"""«Чек» — Telegram-боты (личный + боты тренеров) в одном процессе.

Личный бот владельца + white-label боты тренеров: клиенты присылают еду/воду/отметки,
тренер видит всё в веб-кабинете. Еда: КБЖУ + оценка по Полу Чеку.
"""
import asyncio
import html
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timedelta

import anthropic
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import analyzer
import config
import db
import nutrition
import oura as oura_mod
import web_dashboard
from analyzer import DemoModeError, OpenRouterError
from config import ALLOWED_USER_IDS, TELEGRAM_BOT_TOKEN

log = logging.getLogger("chek")
router = Router()

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_FILE = 4_500_000

DAY_TOKENS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WATER_RE = re.compile(r"^вода\s*\+?\s*(\d{2,4})\s*(?:мл)?\s*$", re.IGNORECASE)
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")

# Заполняются при старте: bot_id -> coach dict / Bot instance.
COACH_BY_BOT: dict[int, dict] = {}
BOTS: dict[int, Bot] = {}
MAIN_BOT_ID: int | None = None


# ---------------------------------------------------------------- контекст бота

def coach_of(message_bot_id: int) -> dict | None:
    """Тренер, которому принадлежит бот; None для личного бота."""
    return COACH_BY_BOT.get(message_bot_id)


def is_coach_himself(uid: int, coach: dict | None) -> bool:
    return bool(coach and coach.get("coach_user_id") == uid)


def is_client_bot(message_bot_id: int) -> bool:
    """Бот тренера. По умолчанию здесь бот только собирает данные: советы, оценки
    и разборы получает тренер (бриф и кабинет). В личном боте ограничения нет."""
    return coach_of(message_bot_id) is not None


def ai_tips_on(message_bot_id: int) -> bool:
    """Тренер включил своему боту ИИ-подсказки клиенту (coaches.ai_tips)."""
    coach = coach_of(message_bot_id)
    return bool(coach and coach.get("ai_tips"))


def show_advice(message_bot_id: int) -> bool:
    """Показывать ли клиенту оценки и советы ИИ.

    Личный бот — всегда да. Бот тренера — только если тренер включил ai_tips;
    по умолчанию выключено, см. журнал решений в CLAUDE.md.
    """
    return (not is_client_bot(message_bot_id)) or ai_tips_on(message_bot_id)


def _ensure(message: Message) -> None:
    """Создаёт пользователя; клиентов привязывает к тренеру."""
    coach = coach_of(message.bot.id)
    cid = None
    if coach and not is_coach_himself(message.from_user.id, coach):
        cid = coach["id"]
    db.ensure_user(message.from_user.id, message.from_user.first_name, coach_id=cid)


CONSENT_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Да, согласен", callback_data="consent:yes"),
    InlineKeyboardButton(text="❌ Нет", callback_data="consent:no"),
]])


def consent_text(coach: dict) -> str:
    name = html.escape(coach.get("name") or "твой тренер")
    return (
        f"Чтобы продолжить, одно важное согласие: тренер <b>{name}</b> будет видеть "
        "твои записи в этом боте — еду, воду, тренировки и отметки самочувствия — "
        "чтобы вести тебя точнее.\n\nСогласен?"
    )


# ---------------------------------------------------------------- доступ

def _is_allowed_personal(uid: int, allow_lock: bool = False) -> bool:
    if ALLOWED_USER_IDS:
        return uid in ALLOWED_USER_IDS
    owner = db.get_setting("owner_id")
    if owner is None:
        if allow_lock:
            db.set_setting("owner_id", str(uid))
            return True
        return False
    return owner == str(uid)


def _is_owner(uid: int) -> bool:
    owner = db.get_setting("owner_id")
    if owner:
        return owner == str(uid)
    return uid in ALLOWED_USER_IDS if ALLOWED_USER_IDS else False


async def access_filter(message: Message) -> bool:
    uid = message.from_user.id
    coach = coach_of(message.bot.id)

    if coach is None:  # личный бот владельца
        if _is_allowed_personal(uid, allow_lock=True):
            return True
        await message.answer(
            "Это личный бот 🙂 Доступ ограничен.\n"
            f"Твой Telegram ID: <code>{uid}</code> — владелец может добавить его "
            "в ALLOWED_USER_IDS в файле .env."
        )
        return False

    # Бот тренера: первый написавший становится тренером-владельцем.
    if coach.get("coach_user_id") is None:
        db.set_coach_owner(coach["id"], uid)
        coach["coach_user_id"] = uid
        return True
    if is_coach_himself(uid, coach):
        return True
    # Сначала привязываем к тренеру (человек мог завестись раньше в другом боте),
    # и только потом смотрим согласие: иначе старое согласие пропускало бы его
    # дальше, а в кабинете тренера он так и не появлялся.
    _ensure(message)
    # Клиент: без согласия — только /start (там покажем кнопки согласия).
    user = db.get_user(uid)
    if user and user.get("consent"):
        return True
    if (message.text or "").startswith("/start"):
        return True
    await message.answer(consent_text(coach), reply_markup=CONSENT_KB)
    return False


async def cb_access_filter(cb: CallbackQuery) -> bool:
    uid = cb.from_user.id
    coach = coach_of(cb.message.bot.id) if cb.message else None
    if coach is None:
        if _is_allowed_personal(uid, allow_lock=True):
            return True
        await cb.answer("Это личный бот — доступ ограничен.", show_alert=True)
        return False
    if is_coach_himself(uid, coach) or cb.data.startswith("consent:"):
        return True
    user = db.get_user(uid)
    if user and user.get("consent"):
        return True
    await cb.answer("Сначала нужно согласие — нажми /start", show_alert=True)
    return False


router.message.filter(access_filter)
router.callback_query.filter(cb_access_filter)


# ---------------------------------------------------------------- утилиты

def now_date_time() -> tuple[str, str]:
    n = datetime.now()
    return n.strftime("%Y-%m-%d"), n.strftime("%H:%M")


def last_dates(n: int) -> list[str]:
    today = datetime.now()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def _i(x) -> int:
    try:
        return int(round(float(x or 0)))
    except (TypeError, ValueError):
        return 0


def chek_emoji(score: float) -> str:
    if score >= 8.5:
        return "🌿"
    if score >= 7:
        return "✅"
    if score >= 5:
        return "⚖️"
    if score >= 3:
        return "⚠️"
    return "🚫"


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


WATER_KB = ikb([
    [("+250 мл", "water:add:250"), ("+500 мл", "water:add:500")],
    [("↺ Сбросить сегодня", "water:reset")],
])
WORKOUT_KB = ikb([[("✅ Сделал", "workout:done"), ("⏭ Не тренировался", "workout:skip")]])
WORKOUT_NUDGE_KB = ikb([[("🏋️ Записать тренировку", "workout:log")]])
# Кольцо уже записало тренировку — предлагаем готовые цифры.
OURA_TRAIN_KB = ikb([[("✅ Записать её", "workout:auto")],
                     [("✍️ Ввести вручную", "workout:manual"),
                      ("⏭ Не тренировался", "workout:skip")]])
WI_KB = ikb([[("🧘 Working In сделал", "habit:wi")]])


def evening_kb(uid: int, date: str) -> InlineKeyboardMarkup | None:
    rows = []
    if not db.get_habit(uid, date, "workingin"):
        rows.append([InlineKeyboardButton(text="🧘 Working In сделал", callback_data="habit:wi")])
    wb = db.get_wellbeing(uid, date) or {}
    if not wb.get("energy"):
        rows.append([InlineKeyboardButton(text="🙂 Отметить самочувствие", callback_data="feel:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def water_target_for(user: dict | None) -> int:
    if user and user.get("water_target_ml"):
        return int(user["water_target_ml"])
    return nutrition.water_target_ml(user.get("weight_kg") if user else None)


def water_text(uid: int, date: str, user: dict | None) -> str:
    total = db.water_total(uid, date)
    target = water_target_for(user)
    pct = min(int(total * 100 / target), 999) if target else 0
    filled = min(round(pct / 10), 10)
    bar = "▰" * filled + "▱" * (10 - filled)
    tail = " ✅ Норма!" if total >= target else ""
    hint = ("" if user and user.get("weight_kg")
            else "\n<i>Норма точнее после анкеты /profile (по Чеку ~0,033 л на кг веса).</i>")
    return (f"💧 <b>Вода сегодня: {total} / {target} мл</b>{tail}\n"
            f"{bar} {pct}%\n"
            f"<i>Добавить: кнопками ниже или сообщением «вода 300».</i>{hint}")


def day_extras_lines(uid: int, date: str, user: dict | None) -> list[str]:
    lines = []
    total = db.water_total(uid, date)
    target = water_target_for(user)
    lines.append(f"💧 Вода: {total} / {target} мл" + (" ✅" if total >= target else ""))
    wi = db.get_habit(uid, date, "workingin")
    lines.append("🧘 Working In: " + ("✅" if wi else "—"))
    dow = datetime.strptime(date, "%Y-%m-%d").weekday()
    plan_today = any(p["dow"] == dow for p in db.get_workout_plan(uid))
    wo = db.workout_for_date(uid, date)
    if wo:
        lines.append("🏋️ Тренировка: " + ("✅ сделана" if wo["status"] == "done" else "⏭ пропущена"))
    elif plan_today:
        lines.append("🏋️ Тренировка: сегодня по плану — записать: /train")
    else:
        lines.append("🏋️ Тренировка: не записана (записать — /train)")
    supps = db.list_supplements(uid)
    if supps:
        taken = db.taken_supplements(uid, date)
        lines.append(f"💊 БАДы: {len(taken)}/{len(supps)}" + (" ✅" if len(taken) == len(supps) else ""))
    wb = db.get_wellbeing(uid, date)
    if wb and (wb.get("energy") or wb.get("mood")):
        bits = []
        if wb.get("energy"):
            bits.append(f"энергия {wb['energy']}")
        if wb.get("mood"):
            bits.append(f"настроение {wb['mood']}")
        if wb.get("stress"):
            bits.append(f"стресс {wb['stress']}")
        lines.append("🙂 Самочувствие: " + ", ".join(bits))
    ol = oura_line(uid)
    if ol:
        lines.append(ol)
    return lines


def build_day_overview(uid: int, date: str, advice: bool = False) -> str:
    user = db.get_user(uid)
    meals = db.meals_for_date(uid, date)
    lines = []
    if meals:
        t = nutrition.day_totals(meals)
        food = (f"🍽 Еда ({t['n']} зап.): <b>{_i(t['kcal'])} ккал</b> · Б {_i(t['protein'])} · "
                f"Ж {_i(t['fat'])} · У {_i(t['carbs'])}")
        if user and user.get("kcal_target"):
            rest = user["kcal_target"] - t["kcal"]
            food += (f"\n   Осталось: {_i(rest)} ккал" if rest >= 0
                     else f"\n   Перебор: {_i(-rest)} ккал 😬")
        lines.append(food)
        if t["chek"]:
            verdict = f" — {nutrition.chek_day_verdict(t['chek'])}" if advice else ""
            lines.append(f"{chek_emoji(t['chek'])} Еда по Чеку: {t['chek']:.1f}/10{verdict}")
    else:
        lines.append("🍽 Еда: записей нет")
    lines.extend(day_extras_lines(uid, date, user))
    return "\n".join(lines)


# ---------------------------------------------------------------- ответы на еду

def day_summary_line(user: dict | None, totals: dict) -> str:
    k, p, f, c = _i(totals["kcal"]), _i(totals["protein"]), _i(totals["fat"]), _i(totals["carbs"])
    if user and user.get("kcal_target"):
        return (
            f"📊 <b>Сегодня</b> ({totals['n']} зап.): {k} / {user['kcal_target']} ккал · "
            f"Б {p}/{user['protein_target']} · Ж {f}/{user['fat_target']} · "
            f"У {c}/{user['carb_target']}"
        )
    return (
        f"📊 <b>Сегодня</b> ({totals['n']} зап.): {k} ккал · Б {p} · Ж {f} · У {c}\n"
        "<i>Рассчитать твою норму → /profile</i>"
    )


def fmt_meal_reply(data: dict, meals: list[dict], user: dict | None,
                   advice: bool = False) -> str:
    dish = html.escape(str(data.get("dish") or "Приём пищи"))
    conf = html.escape(str(data.get("confidence") or "средняя"))
    grams = _i(data.get("total_grams"))
    kcal = _i(data.get("total_kcal"))
    p, f, c = _i(data.get("total_protein_g")), _i(data.get("total_fat_g")), _i(data.get("total_carbs_g"))
    score = int(data.get("chek_score") or 5)

    lines = [f"🍽 <b>{dish}</b>"]
    if grams:
        lines.append(f"⚖️ ~{grams} г · уверенность: {conf}")
    lines.append(f"🔥 <b>{kcal} ккал</b> · Б {p} · Ж {f} · У {c}")

    items = data.get("items") or []
    if isinstance(items, list) and len(items) >= 2:
        for it in items[:8]:
            try:
                lines.append(
                    f"  – {html.escape(str(it['name']))} ~{_i(it['grams'])} г · {_i(it['kcal'])} ккал"
                )
            except (KeyError, TypeError):
                continue

    if data.get("assumptions"):
        lines.append(f"<i>💭 {html.escape(str(data['assumptions']))}</i>")

    # Без advice клиенту идёт только число: вердикт и совет ИИ лежат в raw_json
    # и достаются тренеру в кабинете и брифе.
    lines.append("")
    if advice:
        verdict = html.escape(str(data.get("chek_verdict") or ""))
        lines.append(f"{chek_emoji(score)} <b>По Чеку: {score}/10</b>"
                     + (f" — {verdict}" if verdict else ""))
        if data.get("chek_tip"):
            lines.append(f"💡 {html.escape(str(data['chek_tip']))}")
    else:
        lines.append(f"{chek_emoji(score)} <b>По Чеку: {score}/10</b>")

    totals = nutrition.day_totals(meals)
    lines.append("")
    lines.append(day_summary_line(user, totals))
    return "\n".join(lines)


DEMO_HOWTO = (
    "Сейчас бот в <b>демо-режиме</b>: ИИ-ключ не задан, поэтому анализ еды выключен.\n\n"
    "Включить бесплатный анализ (карта не нужна):\n"
    "1. Зайди на openrouter.ai и зарегистрируйся.\n"
    "2. Раздел Keys → Create Key → скопируй ключ (sk-or-...).\n"
    "3. Вставь его в файл .env в строку OPENROUTER_API_KEY= и перезапусти бота.\n\n"
    "Есть ключ Anthropic? Тогда вставь его в ANTHROPIC_API_KEY — будет точнее. "
    "Подробности в README."
)


def _error_reply(e: Exception) -> str:
    if isinstance(e, OpenRouterError):
        if e.status == 401:
            return ("⚠️ OpenRouter не принял ключ. Проверь OPENROUTER_API_KEY в файле .env "
                    "(без пробелов и кавычек) и перезапусти бота.")
        if e.status == 402:
            return ("💳 Эта модель OpenRouter платная. Поставь в .env OPENROUTER_MODEL=auto — "
                    "бот сам подберёт бесплатную, и перезапусти его.")
        if e.status == 429:
            return ("⏳ Дневной лимит бесплатных запросов OpenRouter пока исчерпан. "
                    "Попробуй позже (лимит обновляется раз в сутки).")
        if e.status in (400, 404):
            return ("⚠️ Модель OpenRouter недоступна. Поставь в .env OPENROUTER_MODEL=auto "
                    "и перезапусти бота.\n"
                    f"<i>Детали: {html.escape(e.message[:150])}</i>")
        return f"⚠️ Ошибка OpenRouter: <code>{html.escape(str(e)[:200])}</code>"
    if isinstance(e, anthropic.AuthenticationError):
        return ("⚠️ Anthropic не принял API-ключ. Проверь ANTHROPIC_API_KEY в файле .env "
                "(без пробелов и кавычек) и перезапусти бота.")
    if isinstance(e, anthropic.PermissionDeniedError):
        return ("⚠️ Anthropic отклонил запрос (доступ запрещён). Чаще всего это блокировка "
                "по региону сети — включи VPN, работающий для всего компьютера, и перезапусти бота.")
    if isinstance(e, anthropic.RateLimitError):
        return "⏳ Слишком много запросов подряд — подожди минуту и попробуй ещё раз."
    if isinstance(e, anthropic.APIConnectionError):
        return "🌐 Нет связи с Anthropic API — проверь интернет (или VPN) и попробуй ещё раз."
    if isinstance(e, anthropic.APIStatusError):
        msg = str(e)
        if "credit" in msg.lower() or "billing" in msg.lower():
            return ("💳 Похоже, на аккаунте Anthropic закончились средства — пополни баланс "
                    "на console.anthropic.com (Billing).")
        return f"⚠️ Ошибка Anthropic API: <code>{html.escape(msg[:200])}</code>"
    return f"⚠️ Что-то пошло не так: <code>{html.escape(str(e)[:200])}</code>"


async def analyze_and_reply(
    message: Message,
    *,
    image_bytes: bytes | None = None,
    media_type: str = "image/jpeg",
    caption: str | None = None,
    text: str | None = None,
    source: str,
) -> None:
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    note = await message.answer("🔍 Смотрю, что у тебя на тарелке…" if image_bytes
                                else "🔍 Считаю по описанию…")
    try:
        data = await analyzer.analyze_meal(
            image_bytes=image_bytes, media_type=media_type, caption=caption, text=text
        )
    except DemoModeError:
        got = "📸 Фото получил" if image_bytes else "✍️ Описание получил"
        await note.edit_text(f"{got}, связь с Telegram работает ✅\n\n{DEMO_HOWTO}")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка анализа")
        await note.edit_text(_error_reply(e))
        return

    if not data.get("is_food"):
        await note.edit_text(
            "Хм, еды тут не вижу 🤔\n"
            "Пришли фото блюда (можно с подписью-уточнением) или опиши его текстом — "
            "посчитаю КБЖУ и оценю по Чеку."
        )
        return

    uid = message.from_user.id
    date, time_ = now_date_time()
    _ensure(message)
    db.add_meal(
        uid, date, time_, source,
        dish=str(data.get("dish") or "Приём пищи"),
        grams=float(data.get("total_grams") or 0),
        kcal=float(data.get("total_kcal") or 0),
        protein=float(data.get("total_protein_g") or 0),
        fat=float(data.get("total_fat_g") or 0),
        carbs=float(data.get("total_carbs_g") or 0),
        chek_score=int(data.get("chek_score") or 5),
        chek_verdict=str(data.get("chek_verdict") or ""),
        raw=data,
    )
    meals = db.meals_for_date(uid, date)
    user = db.get_user(uid)
    await note.edit_text(fmt_meal_reply(data, meals, user, show_advice(message.bot.id)))


# ---------------------------------------------------------------- команды

HELP_TEXT = (
    "Вот всё, что я умею:\n\n"
    "🍽 <b>Еда</b> — пришли фото или напиши текстом («2 яйца, тост с маслом, кофе»): "
    "посчитаю КБЖУ и запишу в дневник. Подпись к фото уточняет расчёт.\n"
    "💧 <b>Вода</b> — напиши «вода 300» или открой /water\n"
    "🏋️ <b>Тренировки</b> — /train записать сделанную (время, длительность, что делал), "
    "/plan расписание-напоминания (<code>/plan пн,ср,пт 18:00</code>)\n"
    "💊 <b>БАДы</b> — /bad: что принимаешь, план приёма и отметки\n"
    "😌 <b>Самочувствие</b> — /feel: энергия, стресс, настроение\n"
    "🩸 <b>Анализы</b> — пришли фото или PDF бланка, /labs покажет показатели и динамику\n"
    "💍 <b>Кольцо Oura</b> — /oura: подтянуть сон, готовность и HRV\n"
    "🧘 <b>Working In</b> — отметить: /habits\n\n"
    "📊 /today — весь день · /week — неделя\n"
    "🎯 /profile — норма КБЖУ и воды · /targets — поправить вручную\n"
    "⏰ /reminders — напоминания и вечерняя сводка\n"
    "🗑 /undo — удалить последнюю запись еды\n"
    "❓ /help — это меню\n\n"
    "<i>Расчёт еды по фото приблизительный (±20–30%). Чем точнее подпись, "
    "тем точнее расчёт.</i>"
)

# Клиенту тренера сразу проговариваем роль бота, чтобы он не ждал от него разборов.
CLIENT_NOTE = "\n\n<i>Я собираю твои данные для тренера — разбор и советы даёт он.</i>"


def coach_greeting(coach: dict, user_name: str) -> str:
    cname = html.escape(coach.get("name") or "тренер")
    brand = html.escape(coach.get("brand") or "Чек")
    return (
        f"Привет, {html.escape(user_name)}! 👋\n"
        f"Я — <b>{brand}</b>, ассистент тренера <b>{cname}</b>.\n\n"
        "Помогаю вести дневник между вашими встречами. Вот что я умею:\n\n"
        "🍽 <b>Еда</b> — фото или текстом, посчитаю КБЖУ\n"
        "💧 <b>Вода</b> — напиши сколько, или /water\n"
        "🏋️ <b>Тренировки</b> — /train записать сделанную, /plan расписание\n"
        "💊 <b>БАДы</b> — /bad: что принимаешь и отметки приёма\n"
        "😌 <b>Самочувствие</b> — /feel: энергия, сон, стресс, настроение\n"
        "🩸 <b>Анализы</b> — пришли фото или PDF бланка (/labs)\n"
        "💍 <b>Кольцо Oura</b> — /oura, подтянуть сон и готовность\n"
        "❓ /help — всегда покажет это меню\n\n"
        f"Я собираю твои данные для тренера — разбор и советы даёт {cname} 🙌\n"
    )


# /cancel — раньше любых обработчиков состояний: aiogram отдаёт сообщение
# первому подошедшему, а хендлеры шагов диалогов ловят вообще всё подряд.
@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять 🙂")
        return
    await state.clear()
    await message.answer("Ок, отменил.", reply_markup=ReplyKeyboardRemove())


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    uid = message.from_user.id
    coach = coach_of(message.bot.id)
    _ensure(message)
    name = message.from_user.first_name or "друг"

    if coach and is_coach_himself(uid, coach):
        username = coach.get("bot_username") or ""
        cabinet_url = config.public_url(
            f"/coach?key={coach.get('cabinet_token')}", host="АДРЕС-СЕРВЕРА")
        await message.answer(
            f"👋 Это панель твоего ассистента <b>{html.escape(coach.get('brand') or '')}</b>.\n\n"
            f"🔗 <b>Подключение клиентов:</b> просто отправь им ссылку "
            f"https://t.me/{username} — бот поздоровается от твоего имени и попросит "
            "согласие на доступ к данным.\n\n"
            f"🖥 <b>Твой кабинет</b> (светофор по клиентам, AI-брифы):\n"
            f"{cabinet_url}\n"
            "(адрес сервера тот же, что в ссылке дашборда владельца; сохрани в закладки)\n\n"
            "📋 /clients — краткий список клиентов прямо здесь.\n"
            "🧠 /aitips — давать ли клиентам ИИ-разборы и советы (по умолчанию нет: "
            "разбор — твоя работа).\n"
            "Кстати, ты можешь пользоваться ботом и как клиент — просто присылай еду."
        )
        return

    if coach:
        user = db.get_user(uid)
        text = coach_greeting(coach, name)
        if not (user and user.get("consent")):
            await message.answer(text + "\n" + consent_text(coach), reply_markup=CONSENT_KB)
        else:
            await message.answer(text + "\n" + HELP_TEXT)
        return

    # Личный бот
    user = db.get_user(uid)
    tail = ("\n\nНачнём с настройки твоей нормы? Жми /profile 🙂"
            if not (user and user.get("kcal_target")) else "")
    if config.ACTIVE_PROVIDER == "demo":
        tail += "\n\n⚠️ " + DEMO_HOWTO
    await message.answer(
        f"Привет, {html.escape(name)}! 👋 Я — твой дневник питания и Чек-коуч.\n\n"
        + HELP_TEXT + tail
    )


@router.callback_query(F.data == "consent:yes")
async def cb_consent_yes(cb: CallbackQuery) -> None:
    coach = coach_of(cb.message.bot.id)
    cid = coach["id"] if coach else None
    db.ensure_user(cb.from_user.id, cb.from_user.first_name, coach_id=cid)
    db.update_user(cb.from_user.id, consent=1)
    await cb.answer("Спасибо! ✅")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer(
        "Отлично, поехали! 🚀\n\n"
        "Для начала рассчитаем твою норму калорий и воды — жми /profile "
        "(6 коротких вопросов).\n\n"
        "А дальше веди всё, что важно: 🍽 еда фото или текстом · 💧 вода · "
        "🏋️ тренировки /train · 💊 БАДы /bad · 😌 самочувствие /feel · "
        "🩸 анализы /labs · 💍 кольцо /oura\n"
        "❓ /help — полное меню." + CLIENT_NOTE
    )


@router.callback_query(F.data == "consent:no")
async def cb_consent_no(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer(
        "Понимаю. Без согласия я работать не смогу — в этом боте тренер видит записи, "
        "чтобы помогать тебе.\nПередумаешь — просто нажми /start."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    note = CLIENT_NOTE if is_client_bot(message.bot.id) else ""
    await message.answer(HELP_TEXT + note)


@router.message(Command("clients"))
async def cmd_clients(message: Message) -> None:
    coach = coach_of(message.bot.id)
    if not coach or not is_coach_himself(message.from_user.id, coach):
        await message.answer("Эта команда — для тренера в его боте 🙂")
        return
    clients = db.clients_of_coach(coach["id"])
    if not clients:
        await message.answer(
            "Клиентов пока нет. Отправь им ссылку на этого бота — "
            f"https://t.me/{coach.get('bot_username') or ''}"
        )
        return
    days = last_dates(7)
    lines = [f"👥 <b>Клиенты ({len(clients)})</b>"]
    for u in clients:
        totals = db.totals_by_date(u["user_id"], days)
        active = len(totals)
        today_k = _i((totals.get(days[0]) or {}).get("kcal"))
        lines.append(f"• {html.escape(u.get('name') or str(u['user_id']))} — "
                     f"актив {active}/7 дн., сегодня {today_k} ккал")
    lines.append("\nПодробно — в веб-кабинете (ссылка в /start).")
    await message.answer("\n".join(lines))


@router.message(Command("aitips"))
async def cmd_aitips(message: Message) -> None:
    """Тренер сам решает, советует ли его бот клиентам. По умолчанию — нет."""
    coach = coach_of(message.bot.id)
    if not coach or not is_coach_himself(message.from_user.id, coach):
        await message.answer("Эта команда — для тренера, владельца этого бота 🙂")
        return
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("on", "вкл", "1"):
        db.update_coach(coach["id"], ai_tips=1)
        coach["ai_tips"] = 1
        await message.answer(
            "🧠 ИИ-подсказки клиентам <b>включены</b>.\n"
            "Бот снова даёт разбор еды и анализов, советы по БАДам и технике, "
            "а на /today — бриф недели тем же движком, что и твой бриф в кабинете."
        )
    elif arg in ("off", "выкл", "0"):
        db.update_coach(coach["id"], ai_tips=0)
        coach["ai_tips"] = 0
        await message.answer(
            "🔕 ИИ-подсказки клиентам <b>выключены</b>.\n"
            "Бот собирает данные и подтверждает их, разбор и советы даёшь ты."
        )
    else:
        state = "включены" if coach.get("ai_tips") else "выключены"
        await message.answer(
            f"🧠 ИИ-подсказки клиентам сейчас <b>{state}</b>.\n"
            "Переключить: <code>/aitips on</code> · <code>/aitips off</code>"
        )


class NewCoach(StatesGroup):
    token = State()
    name = State()
    brand = State()


@router.message(Command("newcoach"))
async def cmd_newcoach(message: Message, state: FSMContext) -> None:
    if coach_of(message.bot.id) is not None or not _is_owner(message.from_user.id):
        await message.answer("Эта команда доступна только владельцу в личном боте 🙂")
        return
    await state.set_state(NewCoach.token)
    await message.answer(
        "Подключаем нового тренера! 🤝\n\n"
        "1️⃣ Попроси тренера (или сделай сам) создать бота у @BotFather:\n"
        "/newbot → имя (например «Ассистент Анны») → юзернейм (…_bot).\n\n"
        "Пришли сюда <b>токен</b> этого бота (вида 123456:ABC…).\n(отмена — /cancel)"
    )


@router.message(StateFilter(NewCoach.token, NewCoach.name, NewCoach.brand),
                F.text.startswith("/"))
async def nc_ignore_commands(message: Message) -> None:
    """Команды внутри диалога не трактуем как ввод. /cancel перехвачен выше."""
    await message.answer(
        "Идёт подключение тренера — другие команды пока подождут. "
        "Заверши текущий шаг или выйди: /cancel"
    )


@router.message(NewCoach.token)
async def nc_token(message: Message, state: FSMContext) -> None:
    token = (message.text or "").strip()
    if not TOKEN_RE.match(token):
        await message.answer("Не похоже на токен 🤔 Нужен вид 123456789:AAE… Пришли ещё раз.")
        return
    await state.update_data(token=token)
    await state.set_state(NewCoach.name)
    await message.answer("2️⃣ Как зовут тренера? (так его будут видеть клиенты, например «Анна»)")


@router.message(NewCoach.name)
async def nc_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=(message.text or "").strip()[:60])
    await state.set_state(NewCoach.brand)
    await message.answer("3️⃣ Название сервиса/бота для клиентов? (например «Анна Фит»)")


@router.message(NewCoach.brand)
async def nc_brand(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    brand = (message.text or "").strip()[:60]
    cabinet_token = secrets.token_hex(8)
    try:
        coach = db.add_coach(data["token"], data["name"], brand, cabinet_token)
    except Exception:  # noqa: BLE001
        await message.answer("Такой токен уже добавлен 🤔 Проверь и попробуй /newcoach заново.")
        return
    cabinet_url = config.public_url(f"/coach?key={cabinet_token}", host="АДРЕС-СЕРВЕРА")
    addr_hint = "" if config.PUBLIC_BASE_URL else " (вместо АДРЕС-СЕРВЕРА — IP сервера)"
    await message.answer(
        f"✅ Тренер <b>{html.escape(data['name'])}</b> ({html.escape(brand)}) добавлен!\n\n"
        f"🔑 Ключ кабинета: <code>{cabinet_token}</code>\n"
        f"🖥 Кабинет: {cabinet_url}\n\n"
        "Сейчас перезапущусь (5 сек) и подхвачу нового бота. Дальше:\n"
        "1. Тренер пишет своему боту /start — бот привяжется к нему.\n"
        "2. Тренер шлёт клиентам ссылку на бота.\n"
        f"3. Кабинет открывается по ссылке выше{addr_hint}."
    )
    log.info("Новый тренер добавлен, перезапуск процесса…")
    asyncio.get_running_loop().call_later(2.0, os._exit, 0)


@router.message(Command("today", "day"))
async def cmd_today(message: Message) -> None:
    uid = message.from_user.id
    date, _ = now_date_time()
    meals = db.meals_for_date(uid, date)
    lines = [f"📅 <b>Сегодня, {datetime.now().strftime('%d.%m')}</b>", ""]
    if meals:
        for m in meals:
            lines.append(
                f"{m['time']} · {html.escape(m['dish'])} — {_i(m['kcal'])} ккал · Чек {m['chek_score']}"
            )
        lines.append("")
    advice = show_advice(message.bot.id)
    lines.append(build_day_overview(uid, date, advice))
    await message.answer("\n".join(lines))

    # Если тренер включил подсказки — тем же движком, что и бриф тренеру.
    if advice and ai_tips_on(message.bot.id):
        note = await message.answer("🧠 Собираю бриф по твоей неделе…")
        try:
            user = db.get_user(uid) or {}
            text = await analyzer.generate_brief(
                user.get("name") or "клиент",
                web_dashboard.week_data_text(uid),
                coach_of(message.bot.id),
            )
        except DemoModeError:
            await note.edit_text(DEMO_HOWTO)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка брифа для клиента")
            await note.edit_text(_error_reply(e))
            return
        await note.edit_text("🧠 <b>Бриф недели</b>\n\n" + html.escape(text.strip())[:3800])


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    uid = message.from_user.id
    days = last_dates(7)
    totals = db.totals_by_date(uid, days)
    user = db.get_user(uid)
    target_w = water_target_for(user)
    water = db.water_by_date(uid, days)
    wi_days = db.habit_dates(uid, "workingin", days)
    workouts = db.workouts_by_date(uid, days)

    lines = ["📆 <b>Последние 7 дней</b>"]
    kcals, cheks = [], []
    for d in days:
        label = datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m")
        t = totals.get(d)
        marks = []
        if water.get(d, 0) >= target_w:
            marks.append("💧")
        if d in wi_days:
            marks.append("🧘")
        if workouts.get(d) == "done":
            marks.append("🏋️")
        suffix = (" " + "".join(marks)) if marks else ""
        if t:
            lines.append(f"{label}: {_i(t['kcal'])} ккал · Б {_i(t['protein'])} "
                         f"Ж {_i(t['fat'])} У {_i(t['carbs'])} · Чек {t['chek']:.1f}{suffix}")
            kcals.append(t["kcal"])
            cheks.append(t["chek"])
        else:
            lines.append(f"{label}: —{suffix}")
    lines.append("")
    if kcals:
        lines.append(f"Среднее по дням с едой: <b>{_i(sum(kcals) / len(kcals))} ккал</b> · "
                     f"Чек {sum(cheks) / len(cheks):.1f}/10")
    done_n = sum(1 for s in workouts.values() if s == "done")
    water_n = sum(1 for d in days if water.get(d, 0) >= target_w)
    lines.append(f"💧 Вода в норме: {water_n}/7 · 🧘 Working In: {len(wi_days)}/7 · "
                 f"🏋️ Тренировок: {done_n}")
    await message.answer("\n".join(lines))


@router.message(Command("undo"))
async def cmd_undo(message: Message) -> None:
    date, _ = now_date_time()
    deleted = db.delete_last_meal(message.from_user.id, date)
    if not deleted:
        await message.answer("Сегодня удалять нечего — записей еды нет.")
        return
    meals = db.meals_for_date(message.from_user.id, date)
    t = nutrition.day_totals(meals)
    await message.answer(
        f"🗑 Удалил: {html.escape(deleted['dish'])} ({_i(deleted['kcal'])} ккал).\n"
        f"Осталось за сегодня: {_i(t['kcal'])} ккал."
    )


@router.message(Command("targets"))
async def cmd_targets(message: Message) -> None:
    uid = message.from_user.id
    parts = (message.text or "").split()[1:]
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        k, p, f, c = (int(x) for x in parts)
        _ensure(message)
        db.update_user(uid, kcal_target=k, protein_target=p, fat_target=f, carb_target=c)
        await message.answer(f"✅ Норма обновлена: {k} ккал · Б {p} · Ж {f} · У {c}")
        return
    user = db.get_user(uid)
    if user and user.get("kcal_target"):
        await message.answer(
            f"Твоя норма: <b>{user['kcal_target']} ккал</b> · Б {user['protein_target']} · "
            f"Ж {user['fat_target']} · У {user['carb_target']}\n"
            f"💧 Вода: {water_target_for(user)} мл\n\n"
            "Изменить КБЖУ вручную: <code>/targets 2000 150 70 200</code>\n"
            "Изменить воду: <code>/water цель 2500</code>\n"
            "Пересчитать по анкете: /profile"
        )
    else:
        await message.answer(
            "Норма пока не задана.\nРассчитать по анкете: /profile\n"
            "Или задать вручную: <code>/targets 2000 150 70 200</code> "
            "(ккал, белки, жиры, углеводы)"
        )


# ---------------------------------------------------------------- вода

@router.message(Command("water"))
async def cmd_water(message: Message) -> None:
    uid = message.from_user.id
    parts = (message.text or "").split()[1:]
    date, time_ = now_date_time()
    _ensure(message)
    if len(parts) == 2 and parts[0].lower() in ("цель", "target") and parts[1].isdigit():
        db.update_user(uid, water_target_ml=int(parts[1]))
        await message.answer(f"✅ Цель по воде: {int(parts[1])} мл в день")
        return
    if len(parts) == 1 and parts[0].isdigit():
        db.add_water(uid, date, time_, int(parts[0]))
    await message.answer(water_text(uid, date, db.get_user(uid)), reply_markup=WATER_KB)


@router.callback_query(F.data.startswith("water:"))
async def cb_water(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    date, time_ = now_date_time()
    action = cb.data.split(":")
    if action[1] == "add":
        ml = int(action[2])
        db.add_water(uid, date, time_, ml)
        await cb.answer(f"+{ml} мл 💧")
    elif action[1] == "reset":
        db.reset_water(uid, date)
        await cb.answer("Сбросил на сегодня")
    try:
        await cb.message.edit_text(water_text(uid, date, db.get_user(uid)), reply_markup=WATER_KB)
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------- привычки

def habits_text(uid: int, date: str) -> str:
    user = db.get_user(uid)
    dates30 = last_dates(30)
    wi_streak = db.habit_streak(uid, "workingin", dates30)
    wi_week = len(db.habit_dates(uid, "workingin", last_dates(7)))
    wi_today = db.get_habit(uid, date, "workingin")
    lines = ["🧘 <b>Привычки по Чеку</b>", ""]
    total = db.water_total(uid, date)
    target = water_target_for(user)
    lines.append(f"💧 Вода: {total} / {target} мл" + (" ✅" if total >= target else ""))
    lines.append("🧘 Working In сегодня: " + ("✅" if wi_today else "ещё нет"))
    lines.append(f"   Серия: {wi_streak} дн. подряд · за неделю: {wi_week}/7")
    lines.append("")
    lines.append("<i>Working In по Чеку — «зарядка наоборот»: дыхание животом, зоновые "
                 "упражнения, спокойная прогулка. 10–15 минут в день.</i>")
    return "\n".join(lines)


@router.message(Command("habits"))
async def cmd_habits(message: Message) -> None:
    uid = message.from_user.id
    date, _ = now_date_time()
    _ensure(message)
    kb = None if db.get_habit(uid, date, "workingin") else WI_KB
    await message.answer(habits_text(uid, date), reply_markup=kb)


@router.callback_query(F.data == "habit:wi")
async def cb_habit_wi(cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    date, _ = now_date_time()
    db.set_habit(uid, date, "workingin", 1)
    await cb.answer("Working In засчитан 🧘")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer("🧘 Working In за сегодня — ✅. Пол Чек одобряет!")


# ---------------------------------------------------------------- тренировки

class LogWorkout(StatesGroup):
    when = State()
    duration = State()
    description = State()


TRAIN_ASK = "🏋️ <b>Записать тренировку?</b>"


def _parse_hhmm(text: str | None) -> str | None:
    """«18:30», «18.30», «1830» → 18:30. «сейчас» или пусто — текущее время."""
    t = (text or "").strip().lower()
    if t in ("", "сейчас", "now"):
        return now_date_time()[1]
    m = re.fullmatch(r"(\d{1,2})[:.\s-]?(\d{2})", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if h <= 23 and mi <= 59 else None


def _parse_duration(text: str | None) -> int | None:
    """«40», «40 мин», «1 час», «1.5 часа», «1 ч 20» → минуты."""
    t = (text or "").strip().lower().replace(",", ".")
    if not t:
        return None
    hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:ч|час)", t)
    mins = re.search(r"(\d+)\s*(?:м|мин)", t)
    total = 0.0
    if hours:
        total += float(hours.group(1)) * 60
        if not mins:                       # «1 ч 20» — хвостовое число это минуты
            rest = re.search(r"(\d+)", t[hours.end():])
            if rest:
                total += int(rest.group(1))
    if mins:
        total += int(mins.group(1))
    if not hours and not mins:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)", t)
        if not m:
            return None
        total = float(m.group(1))
    total = int(round(total))
    return total if 1 <= total <= 600 else None


def _oura_candidate(uid: int, date: str) -> dict | None:
    """Тренировка кольца за сегодня, которую ещё не записали в дневник."""
    if not db.oura_connected(uid) or db.workout_for_date(uid, date):
        return None
    for w in db.oura_workouts_for_date(uid, date):
        minutes = oura_mod.workout_duration_min(w)
        hhmm = oura_mod.workout_time(w)
        if minutes and hhmm:
            return {"time": hhmm, "minutes": minutes,
                    "calories": w.get("calories"), "activity": w.get("activity")}
    return None


@router.message(Command("train"))
async def cmd_train(message: Message, state: FSMContext) -> None:
    """Тренировку не придумываем, а фиксируем: её задаёт тренер, не бот."""
    uid = message.from_user.id
    date, _ = now_date_time()
    _ensure(message)
    await state.clear()

    cand = _oura_candidate(uid, date)
    if cand:
        await state.set_data({"date": date, "time": cand["time"],
                              "duration": cand["minutes"],
                              "kcal": cand["calories"], "kcal_source": "oura"})
        bits = [f"в {cand['time']}", f"{cand['minutes']} мин"]
        if cand["calories"]:
            bits.append(f"{cand['calories']} ккал")
        await message.answer(
            "💍 Кольцо записало тренировку: " + ", ".join(bits) + ".\nЗаписать её?",
            reply_markup=OURA_TRAIN_KB,
        )
        return
    await message.answer(TRAIN_ASK, reply_markup=WORKOUT_KB)


@router.callback_query(F.data.startswith("workout:"))
async def cb_workout(cb: CallbackQuery, state: FSMContext) -> None:
    uid = cb.from_user.id
    date, time_ = now_date_time()
    action = cb.data.split(":")[1]

    if action == "log":                    # кнопка из напоминания
        await cb.answer()
        await cb.message.answer(TRAIN_ASK, reply_markup=WORKOUT_KB)
        return

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    if action == "skip":
        db.add_workout_log(uid, date, time_, "skipped", note="train")
        await cb.answer("Записал")
        await cb.message.answer("⏭ Отметил: сегодня не тренировался 🙂")
        return

    if action == "auto":                   # берём готовые цифры с кольца
        data = await state.get_data()
        if not data.get("time"):
            await cb.answer("Данные потерялись — начни заново: /train", show_alert=True)
            return
        await cb.answer()
        await state.set_state(LogWorkout.description)
        await cb.message.answer("✍️ Опиши тренировку: что делал, подходы, ощущения.\n"
                                "Поставь «-», если без описания.")
        return

    if action == "manual":                 # цифры кольца не подошли — вводим руками
        await cb.answer()
        await state.clear()
        await cb.message.answer(TRAIN_ASK, reply_markup=WORKOUT_KB)
        return

    await cb.answer()
    await state.set_state(LogWorkout.when)
    await state.update_data(date=date)
    await cb.message.answer(
        "🕒 Во сколько тренировался? Например <code>18:30</code>, "
        "или напиши «сейчас».\n(отмена — /cancel)"
    )


@router.message(StateFilter(LogWorkout.when, LogWorkout.duration, LogWorkout.description),
                F.text.startswith("/"))
async def lw_ignore_commands(message: Message) -> None:
    """Команды внутри записи тренировки не считаем ответом. /cancel перехвачен выше."""
    await message.answer("Записываю тренировку — другие команды пока подождут. "
                         "Ответь на вопрос или выйди: /cancel")


@router.message(LogWorkout.when)
async def lw_when(message: Message, state: FSMContext) -> None:
    hhmm = _parse_hhmm(message.text)
    if hhmm is None:
        await message.answer("Не разобрал время 🤔 Напиши как <code>18:30</code> или «сейчас».")
        return
    await state.update_data(time=hhmm)
    await state.set_state(LogWorkout.duration)
    await message.answer("⏱ Сколько длилась? Например <code>40</code>, «40 мин» или «1 час».")


async def _workout_kcal(uid: int, date: str, hhmm: str, minutes: int) -> tuple[int, str]:
    """Расход за тренировку: число из кольца, если тренировка в нём есть, иначе оценка."""
    if db.oura_connected(uid):
        stored = oura_mod.match_workout(db.oura_workouts_for_date(uid, date), hhmm, minutes)
        if stored and stored.get("calories"):
            return int(round(float(stored["calories"]))), "oura"
        if config.OURA_ENABLED:
            try:
                hit = oura_mod.match_workout(
                    await oura_mod.fetch_workouts(uid, date), hhmm, minutes)
                if hit and hit.get("calories"):
                    return int(round(float(hit["calories"]))), "oura"
            except Exception:  # noqa: BLE001
                log.warning("Oura: тренировки за день не получены", exc_info=True)
    user = db.get_user(uid) or {}
    return nutrition.workout_kcal_estimate(user.get("weight_kg"), minutes), "estimate"


@router.message(LogWorkout.duration)
async def lw_duration(message: Message, state: FSMContext) -> None:
    minutes = _parse_duration(message.text)
    if minutes is None:
        await message.answer("Не разобрал длительность 🤔 Напиши минуты числом "
                             "(<code>40</code>) или «1 час 20 мин».")
        return
    data = await state.get_data()
    kcal, source = await _workout_kcal(message.from_user.id, data["date"], data["time"], minutes)
    await state.update_data(duration=minutes, kcal=kcal, kcal_source=source)
    await state.set_state(LogWorkout.description)
    await message.answer("✍️ Опиши тренировку: что делал, подходы, ощущения.\n"
                         "Поставь «-», если без описания.")


@router.message(LogWorkout.description)
async def lw_description(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    desc = "" if raw in ("-", "—", "нет") else raw[:500]
    data = await state.get_data()
    await state.clear()
    kcal, source = data.get("kcal"), data.get("kcal_source", "")
    db.add_workout_log(message.from_user.id, data["date"], data["time"], "done",
                       note="train", duration_min=data["duration"], description=desc,
                       kcal_burned=kcal, kcal_source=source)
    # «≈» и «(оценка)» — только для прикидки: из кольца число точное.
    burn = ""
    if kcal:
        burn = (f" — ≈ {kcal} ккал (оценка)" if source == "estimate"
                else f" — {kcal} ккал (Oura)")
    tail = f". {html.escape(desc)}" if desc else ""
    await message.answer(
        f"🏋️ Записал тренировку в {data['time']}, {data['duration']} мин{burn}{tail}"
    )


def _parse_plan_args(args: str) -> list[tuple[int, str]] | None:
    parts = args.split()
    if not parts:
        return None
    time_ = "18:00"
    if len(parts) >= 2:
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", parts[1])
        if not m:
            return None
        time_ = f"{int(m.group(1)):02d}:{m.group(2)}"
    days = []
    for token in parts[0].split(","):
        token = token.strip().lower()
        if token not in DAY_TOKENS:
            return None
        days.append(DAY_TOKENS[token])
    return [(d, time_) for d in sorted(set(days))]


def plan_text(uid: int) -> str:
    plan = db.get_workout_plan(uid)
    if not plan:
        return ("🏋️ Расписания пока нет.\n\n"
                "Задать: <code>/plan пн,ср,пт 18:00</code> — дни через запятую и время.\n"
                "Убрать: <code>/plan off</code>\n"
                "Программа в любой день: /train")
    days = " · ".join(f"{DAY_NAMES[p['dow']]} {p['time']}" for p in plan)
    return (f"🏋️ <b>Тренировки (турник/брусья):</b> {days}\n"
            "В эти дни напомню и пришлю кнопки отметки. Программа — /train.\n\n"
            "Изменить: <code>/plan пн,ср,пт 18:00</code> · убрать: <code>/plan off</code>")


@router.message(Command("plan"))
async def cmd_plan(message: Message) -> None:
    uid = message.from_user.id
    _ensure(message)
    args = (message.text or "").split(maxsplit=1)
    args = args[1].strip() if len(args) > 1 else ""
    if not args:
        await message.answer(plan_text(uid))
        return
    if args.lower() in ("off", "выкл", "нет"):
        db.set_workout_plan(uid, [])
        await message.answer("Ок, расписание тренировок убрал. /train работает в любой день.")
        return
    entries = _parse_plan_args(args)
    if entries is None:
        await message.answer(
            "Не понял формат 🤔 Пример: <code>/plan пн,ср,пт 18:00</code>\n"
            "Дни: пн, вт, ср, чт, пт, сб, вс. Время можно не указывать (будет 18:00)."
        )
        return
    db.set_workout_plan(uid, entries)
    await message.answer("✅ " + plan_text(uid))


# ---------------------------------------------------------------- напоминания

@router.message(Command("reminders"))
async def cmd_reminders(message: Message) -> None:
    uid = message.from_user.id
    _ensure(message)
    args = (message.text or "").split()[1:]
    if args:
        a = args[0].lower()
        if a in ("off", "выкл"):
            db.update_user(uid, reminders_on=0)
            await message.answer("🔕 Напоминания выключены. Включить: /reminders on")
            return
        if a in ("on", "вкл"):
            db.update_user(uid, reminders_on=1)
            await message.answer("🔔 Напоминания включены.")
            return
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", a)
        if m:
            t = f"{int(m.group(1)):02d}:{m.group(2)}"
            db.update_user(uid, evening_time=t)
            await message.answer(f"🌙 Вечерняя сводка теперь в {t}.")
            return
    user = db.get_user(uid) or {}
    state = "включены 🔔" if user.get("reminders_on", 1) else "выключены 🔕"
    await message.answer(
        f"Напоминания: <b>{state}</b>\n"
        f"🌙 Вечерняя сводка дня: {user.get('evening_time') or '21:00'}\n"
        f"🏋️ Тренировки: по расписанию /plan\n\n"
        "Команды: <code>/reminders 21:30</code> — время сводки, "
        "<code>/reminders off</code> / <code>on</code>"
    )


def _fire_once(uid: int, date: str, tag: str) -> bool:
    key = f"fired:{uid}:{date}:{tag}"
    if db.get_setting(key):
        return False
    db.set_setting(key, "1")
    return True


def _bot_for_user(user: dict) -> Bot | None:
    """Каким ботом писать пользователю: бот его тренера или личный бот."""
    if user.get("coach_id"):
        coach = db.coach_by_id(user["coach_id"])
        if coach and coach.get("bot_id") in BOTS:
            return BOTS[coach["bot_id"]]
        return None
    return BOTS.get(MAIN_BOT_ID)


async def _oura_daily_fetch(date: str) -> None:
    if not config.OURA_ENABLED or datetime.now().hour < 7:
        return
    for uid in db.oura_users():
        if _fire_once(uid, date, "oura_fetch"):
            try:
                await oura_mod.fetch_and_store(uid)
                log.info("Oura обновлён для %s", uid)
            except Exception:  # noqa: BLE001
                log.exception("Oura daily fetch")


async def reminder_loop() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            now = datetime.now()
            hm = now.strftime("%H:%M")
            date = now.strftime("%Y-%m-%d")
            dow = now.weekday()
            await _oura_daily_fetch(date)
            for uid in db.all_user_ids():
                user = db.get_user(uid)
                if not user or not user.get("reminders_on", 1):
                    continue
                if user.get("coach_id") and not user.get("consent"):
                    continue
                bot = _bot_for_user(user)
                if bot is None:
                    continue
                for p in db.get_workout_plan(uid):
                    if p["dow"] == dow and p["time"] == hm and _fire_once(uid, date, f"wo{p['id']}"):
                        await bot.send_message(
                            uid,
                            "🏋️ По расписанию сегодня тренировка.\n"
                            "Как закончишь — запиши её:",
                            reply_markup=WORKOUT_NUDGE_KB,
                        )
                ev = user.get("evening_time") or "21:00"
                if ev == hm and _fire_once(uid, date, "evening"):
                    await bot.send_message(
                        uid,
                        "🌙 <b>Вечерняя сводка дня</b>\n\n"
                        + build_day_overview(uid, date, show_advice(bot.id)),
                        reply_markup=evening_kb(uid, date),
                    )
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(20)


# ---------------------------------------------------------------- БАДы

TIMINGS = ["утром", "днём", "вечером", "на ночь", "с едой"]

# План приёма: сколько дней в неделю клиент планирует принимать добавку.
PLAN_LABELS = {7: "каждый день", 6: "6 раз в неделю", 5: "5 раз в неделю",
               4: "4 раза в неделю", 3: "3 раза в неделю", 2: "2 раза в неделю",
               1: "1 раз в неделю"}
PLAN_KB = [["каждый день"], ["6 раз в неделю", "5 раз в неделю"],
           ["4 раза в неделю", "3 раза в неделю"], ["2 раза в неделю", "1 раз в неделю"]]


def _parse_plan(text: str | None) -> int | None:
    """«каждый день» → 7, «3 раза в неделю» → 3. None — не разобрали."""
    t = (text or "").strip().lower()
    if t in ("каждый день", "ежедневно", "каждый"):
        return 7
    m = re.search(r"[1-7]", t)
    return int(m.group()) if m else None


class AddSuppl(StatesGroup):
    name = State()
    timing = State()
    plan = State()


def suppl_kb(uid: int, date: str, client: bool = False) -> InlineKeyboardMarkup:
    supps = db.list_supplements(uid)
    taken = db.taken_supplements(uid, date)
    rows = []
    for s in supps:
        mark = "✅" if s["id"] in taken else "⬜"
        label = f"{mark} {s['name']}" + (f" · {s['timing']}" if s["timing"] else "")
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"sup:take:{s['id']}")])
    # «Совместимость» — это ИИ-совет, клиенту тренера его не показываем.
    bottom = [InlineKeyboardButton(text="➕ Добавить", callback_data="sup:add")]
    if not client:
        bottom.append(InlineKeyboardButton(text="🔍 Совместимость", callback_data="sup:check"))
    rows.append(bottom)
    if supps:
        rows.append([InlineKeyboardButton(text="🗑 Убрать из списка", callback_data="sup:manage")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def suppl_text(uid: int, date: str, client: bool = False) -> str:
    supps = db.list_supplements(uid)
    if not supps:
        return ("💊 <b>БАДы и добавки</b>\n\nСписок пуст. Нажми «Добавить» — укажешь название, "
                "когда принимаешь и как часто планируешь.")
    taken = db.taken_supplements(uid, date)
    lines = []
    for s in supps:
        plan = s["plan_days_per_week"] or 7
        parts = [p for p in (s["timing"], PLAN_LABELS.get(plan, f"{plan} раз в неделю")) if p]
        lines.append(f"• {html.escape(s['name'])} — {' · '.join(parts)}")
    # «Совместимость» — ИИ-совет, клиенту тренера кнопку не показываем.
    tail = ("\n\nОтметь принятые кнопками." if client
            else "\n\nОтметь принятые кнопками. Проверить набор — «Совместимость».")
    return f"💊 <b>БАДы сегодня: {len(taken)}/{len(supps)}</b>\n" + "\n".join(lines) + tail


@router.message(Command("supplements", "bad", "supps"))
async def cmd_supplements(message: Message) -> None:
    uid = message.from_user.id
    date, _ = now_date_time()
    _ensure(message)
    client = not show_advice(message.bot.id)
    await message.answer(suppl_text(uid, date, client), reply_markup=suppl_kb(uid, date, client))


@router.callback_query(F.data.startswith("sup:"))
async def cb_suppl(cb: CallbackQuery, state: FSMContext) -> None:
    uid = cb.from_user.id
    date, _ = now_date_time()
    parts = cb.data.split(":")
    action = parts[1]
    client = not show_advice(cb.message.bot.id)

    if action == "take":
        taken = db.toggle_supplement_taken(uid, date, int(parts[2]))
        await cb.answer("Принято ✅" if taken else "Снял отметку")
        try:
            await cb.message.edit_text(suppl_text(uid, date, client), reply_markup=suppl_kb(uid, date, client))
        except TelegramBadRequest:
            pass
    elif action == "add":
        await cb.answer()
        await state.set_state(AddSuppl.name)
        await cb.message.answer("Название добавки? (например «Витамин D 2000 МЕ»)\n(отмена — /cancel)")
    elif action == "check":
        if client:
            await cb.answer("Совместимость смотрит тренер", show_alert=True)
            return
        await cb.answer("Проверяю…")
        await _run_suppl_check(cb.message, uid)
    elif action == "manage":
        supps = db.list_supplements(uid)
        rows = [[InlineKeyboardButton(text=f"🗑 {s['name']}"[:60], callback_data=f"sup:del:{s['id']}")]
                for s in supps]
        rows.append([InlineKeyboardButton(text="← Назад", callback_data="sup:back")])
        await cb.answer()
        try:
            await cb.message.edit_text("Что убрать из списка?",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except TelegramBadRequest:
            pass
    elif action == "del":
        db.deactivate_supplement(uid, int(parts[2]))
        await cb.answer("Убрал")
        try:
            await cb.message.edit_text(suppl_text(uid, date, client), reply_markup=suppl_kb(uid, date, client))
        except TelegramBadRequest:
            pass
    elif action == "back":
        await cb.answer()
        try:
            await cb.message.edit_text(suppl_text(uid, date, client), reply_markup=suppl_kb(uid, date, client))
        except TelegramBadRequest:
            pass


async def _run_suppl_check(message: Message, uid: int) -> None:
    supps = db.list_supplements(uid)
    if not supps:
        await message.answer("Список БАДов пуст — сначала добавь.")
        return
    note = await message.answer("🔍 Проверяю совместимость набора…")
    try:
        text = await analyzer.check_supplements(supps)
    except DemoModeError:
        await note.edit_text(DEMO_HOWTO)
        return
    except Exception as e:  # noqa: BLE001
        await note.edit_text(_error_reply(e))
        return
    await note.edit_text("💊 <b>Твой набор</b>\n\n" + html.escape(text.strip())[:3800])


@router.message(AddSuppl.name)
async def add_suppl_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Напиши название добавки, например «Магний глицинат».")
        return
    await state.update_data(name=name[:80])
    await state.set_state(AddSuppl.timing)
    await message.answer("Когда принимаешь?",
                         reply_markup=_kb([[t] for t in TIMINGS] + [["Не важно"]]))


@router.message(AddSuppl.timing)
async def add_suppl_timing(message: Message, state: FSMContext) -> None:
    timing = (message.text or "").strip().lower()
    if timing not in TIMINGS and timing != "не важно":
        await message.answer("Выбери кнопкой 🙂", reply_markup=_kb([[t] for t in TIMINGS] + [["Не важно"]]))
        return
    await state.update_data(timing="" if timing == "не важно" else timing)
    await state.set_state(AddSuppl.plan)
    await message.answer("Как часто планируешь принимать?", reply_markup=_kb(PLAN_KB))


@router.message(AddSuppl.plan)
async def add_suppl_plan(message: Message, state: FSMContext) -> None:
    plan = _parse_plan(message.text)
    if plan is None:
        await message.answer("Выбери кнопкой 🙂", reply_markup=_kb(PLAN_KB))
        return
    data = await state.get_data()
    await state.clear()
    uid = message.from_user.id
    db.add_supplement(uid, data["name"], data.get("timing", ""), plan)
    await message.answer(
        f"✅ Добавил: <b>{html.escape(data['name'])}</b> — {PLAN_LABELS[plan]}",
        reply_markup=ReplyKeyboardRemove())
    # Проверку совместимости (ИИ-совет) показываем, только если она разрешена.
    if show_advice(message.bot.id):
        await _run_suppl_check(message, uid)


# ---------------------------------------------------------------- самочувствие

class Feel(StatesGroup):
    energy = State()
    mood = State()
    stress = State()
    libido = State()
    note = State()


def scale_kb(prefix: str, skip: bool = False) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=str(n), callback_data=f"{prefix}:{n}") for n in range(1, 6)]
    row2 = [InlineKeyboardButton(text=str(n), callback_data=f"{prefix}:{n}") for n in range(6, 11)]
    rows = [row1, row2]
    if skip:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data=f"{prefix}:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("feel", "wellbeing"))
async def cmd_feel(message: Message, state: FSMContext) -> None:
    _ensure(message)
    await state.set_state(Feel.energy)
    await message.answer(
        "🙂 <b>Как ты сегодня?</b> Оцени по шкале 1–10.\n(отмена — /cancel)\n\n"
        "⚡️ Энергия?", reply_markup=scale_kb("feel_energy"))


async def _feel_next(cb: CallbackQuery, state: FSMContext, field: str, value,
                     next_state, prompt: str, kb) -> None:
    if value is not None:
        await state.update_data(**{field: value})
    await cb.answer()
    await state.set_state(next_state)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await cb.message.answer(prompt, reply_markup=kb)


@router.callback_query(Feel.energy, F.data.startswith("feel_energy:"))
async def feel_energy(cb: CallbackQuery, state: FSMContext) -> None:
    v = int(cb.data.split(":")[1])
    await _feel_next(cb, state, "energy", v, Feel.mood, "🙂 Настроение?", scale_kb("feel_mood"))


@router.callback_query(Feel.mood, F.data.startswith("feel_mood:"))
async def feel_mood(cb: CallbackQuery, state: FSMContext) -> None:
    v = int(cb.data.split(":")[1])
    await _feel_next(cb, state, "mood", v, Feel.stress, "😤 Уровень стресса?", scale_kb("feel_stress"))


@router.callback_query(Feel.stress, F.data.startswith("feel_stress:"))
async def feel_stress(cb: CallbackQuery, state: FSMContext) -> None:
    v = int(cb.data.split(":")[1])
    await _feel_next(cb, state, "stress", v, Feel.libido,
                     "❤️ Либидо? (можно пропустить)", scale_kb("feel_libido", skip=True))


@router.callback_query(Feel.libido, F.data.startswith("feel_libido:"))
async def feel_libido(cb: CallbackQuery, state: FSMContext) -> None:
    raw = cb.data.split(":")[1]
    v = None if raw == "skip" else int(raw)
    await _feel_next(cb, state, "libido", v, Feel.note,
                     "📝 Пара слов о дне? (или нажми /skip)", ReplyKeyboardRemove())


async def _save_feel(uid: int, date: str, state: FSMContext, note: str | None) -> str:
    data = await state.get_data()
    await state.clear()
    db.set_wellbeing(uid, date, energy=data.get("energy"), mood=data.get("mood"),
                     stress=data.get("stress"), libido=data.get("libido"), note=note)
    parts = []
    if data.get("energy"):
        parts.append(f"⚡️ энергия {data['energy']}")
    if data.get("mood"):
        parts.append(f"🙂 настроение {data['mood']}")
    if data.get("stress"):
        parts.append(f"😤 стресс {data['stress']}")
    if data.get("libido"):
        parts.append(f"❤️ либидо {data['libido']}")
    return "✅ Записал: " + ", ".join(parts) + ".\nСпасибо, это поможет видеть связи с едой и сном 🙌"


@router.message(Feel.note, Command("skip"))
async def feel_note_skip(message: Message, state: FSMContext) -> None:
    date, _ = now_date_time()
    txt = await _save_feel(message.from_user.id, date, state, None)
    await message.answer(txt)


@router.message(Feel.note)
async def feel_note(message: Message, state: FSMContext) -> None:
    date, _ = now_date_time()
    note = (message.text or "").strip()[:500]
    txt = await _save_feel(message.from_user.id, date, state, note or None)
    await message.answer(txt)


@router.callback_query(F.data == "feel:start")
async def cb_feel_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(Feel.energy)
    await cb.message.answer("🙂 <b>Как ты сегодня?</b> 1–10.\n\n⚡️ Энергия?",
                            reply_markup=scale_kb("feel_energy"))


# ---------------------------------------------------------------- анализы

class LabUpload(StatesGroup):
    wait = State()


def _flag_icon(flag: str) -> str:
    return {"низко": "🔽", "высоко": "🔼"}.get(flag, "✅")


def labs_overview_text(uid: int, advice: bool = False) -> str:
    dates = db.lab_dates(uid)
    if not dates:
        return ("🧪 <b>Анализы</b>\n\nПока нет загруженных анализов.\n"
                "Пришли <b>PDF или фото бланка</b> любой лаборатории — разберу показатели, "
                "покажу, что вне нормы, и буду следить за динамикой.")
    latest = db.latest_markers(uid)
    abnormal = [m for m in latest if m["flag"] in ("низко", "высоко")]
    lines = [f"🧪 <b>Анализы</b> · загрузок: {len(dates)}, последняя {dates[0]}", ""]
    if abnormal:
        lines.append("<b>Вне нормы (по последним данным):</b>")
        for m in abnormal[:15]:
            val = m["value_text"] or (f"{m['value']:g}" if m["value"] is not None else "?")
            ref = ""
            if m["ref_low"] is not None or m["ref_high"] is not None:
                lo = f"{m['ref_low']:g}" if m["ref_low"] is not None else ""
                hi = f"{m['ref_high']:g}" if m["ref_high"] is not None else ""
                ref = f" (норма {lo}–{hi})"
            lines.append(f"{_flag_icon(m['flag'])} {html.escape(m['name'])}: "
                         f"{val} {html.escape(m['unit'] or '')}{ref}")
    else:
        lines.append("✅ Все показатели последнего бланка в пределах нормы.")
    lines.append("")
    lines.append("Прислать новый бланк — /labupload"
                 + (" · разбор с рекомендациями — /labreport" if advice else ""))
    return "\n".join(lines)


@router.message(Command("labs", "analysis"))
async def cmd_labs(message: Message) -> None:
    _ensure(message)
    await message.answer(labs_overview_text(message.from_user.id, show_advice(message.bot.id)))


@router.message(Command("labupload"))
async def cmd_labupload(message: Message, state: FSMContext) -> None:
    _ensure(message)
    await state.set_state(LabUpload.wait)
    await message.answer("📄 Пришли <b>PDF или фото</b> бланка анализов следующим сообщением.\n"
                         "(отмена — /cancel)")


async def _process_labs(message: Message, data: bytes, media_type: str,
                        state: FSMContext | None) -> None:
    if state is not None:
        await state.clear()
    uid = message.from_user.id
    note = await message.answer("🧪 Разбираю бланк, это займёт полминуты…")
    try:
        parsed = await analyzer.parse_labs(data, media_type)
    except DemoModeError:
        await note.edit_text(DEMO_HOWTO)
        return
    except analyzer.LabProviderError:
        await note.edit_text("PDF-бланки я разбираю только на Claude. Пришли <b>фото</b> "
                             "бланка — его прочитаю на любой модели, либо укажи ключ Anthropic.")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка разбора анализов")
        await note.edit_text(_error_reply(e))
        return

    if not parsed.get("is_lab") or not parsed["markers"]:
        await note.edit_text("Хм, не похоже на бланк анализов 🤔 Пришли фото или PDF, где "
                             "видны показатели с их значениями.")
        return

    date = parsed.get("date") or now_date_time()[0]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        date = now_date_time()[0]
    db.add_lab_result(uid, date, parsed["panel"], parsed["markers"])

    # Подтверждение нейтральное: цифры — да, трактовка — нет. Разбор идёт тренеру.
    n = len(parsed["markers"])
    abn = [m for m in parsed["markers"] if m["flag"] in ("низко", "высоко")]
    tail = ("Записал в дневник." if not is_client_bot(message.bot.id) else "Передал тренеру.")
    await note.edit_text(
        f"✅ Разобрал бланк: <b>{html.escape(parsed['panel'])}</b> от {date} — "
        f"{n} показателей, {len(abn)} вне нормы. {tail} Диагнозы не ставлю.\n\n"
        "Свои цифры и динамику смотри в /labs."
    )


@router.message(LabUpload.wait, F.photo)
async def lab_photo(message: Message, state: FSMContext) -> None:
    buf = await message.bot.download(message.photo[-1])
    await _process_labs(message, buf.read(), "image/jpeg", state)


@router.message(LabUpload.wait, F.document)
async def lab_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    mt = doc.mime_type or ""
    if mt not in IMAGE_TYPES and mt != "application/pdf":
        await message.answer("Нужен PDF или картинка бланка. Пришли ещё раз или /cancel.")
        return
    if (doc.file_size or 0) > 15_000_000:
        await message.answer("Файл великоват. Пришли поменьше (или отдельными страницами).")
        return
    buf = await message.bot.download(doc)
    await _process_labs(message, buf.read(), mt, state)


@router.message(Command("labreport"))
async def cmd_labreport(message: Message) -> None:
    if not show_advice(message.bot.id):
        # Разбор анализов — работа тренера; ИИ по-прежнему готовит его, но в бриф.
        await message.answer(
            "Разбор анализов делает твой тренер — я передал ему показатели и динамику.\n"
            "Свои цифры смотри в /labs."
        )
        return
    uid = message.from_user.id
    dates = db.lab_dates(uid)
    if not dates:
        await message.answer("Пока нет анализов. Пришли бланк — /labupload.")
        return
    note = await message.answer("🔬 Готовлю разбор последнего бланка…")
    markers = db.markers_for_date(uid, dates[0])
    hist_notes = []
    if len(dates) >= 2:
        for m in markers:
            if m["flag"] in ("низко", "высоко") and m["value"] is not None:
                hist = db.marker_history(uid, m["name"])
                prev = [h for h in hist if h["date"] < dates[0] and h["value"] is not None]
                if prev:
                    diff = m["value"] - prev[-1]["value"]
                    arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "=")
                    hist_notes.append(f"{m['name']} {arrow} (было {prev[-1]['value']:g}, стало {m['value']:g})")
    try:
        text = await analyzer.interpret_labs(markers, "; ".join(hist_notes[:8]))
    except DemoModeError:
        await note.edit_text(DEMO_HOWTO)
        return
    except Exception as e:  # noqa: BLE001
        await note.edit_text(_error_reply(e))
        return
    await note.edit_text("🔬 <b>Разбор анализов</b>\n\n" + html.escape(text.strip())[:3800])


# ---------------------------------------------------------------- Oura (кольцо)

def oura_line(uid: int) -> str | None:
    o = db.oura_latest(uid)
    if not o:
        return None
    bits = []
    if o.get("readiness"):
        bits.append(f"готовность {o['readiness']}")
    if o.get("sleep_h"):
        h = int(o["sleep_h"]); m = int(round((o["sleep_h"] - h) * 60))
        bits.append(f"сон {h}:{m:02d}")
    if o.get("hrv"):
        bits.append(f"HRV {int(o['hrv'])}")
    return "💍 Кольцо: " + ", ".join(bits) if bits else None


@router.message(Command("oura", "ring"))
async def cmd_oura(message: Message) -> None:
    uid = message.from_user.id
    _ensure(message)
    if not config.OURA_ENABLED:
        if _is_owner(uid) and coach_of(message.bot.id) is None:
            await message.answer(
                "💍 Интеграция Oura ещё не настроена на сервере.\n\n"
                "Нужно один раз зарегистрировать приложение на "
                "cloud.ouraring.com (раздел для разработчиков), получить Client ID и Secret "
                "и вписать их в .env (OURA_CLIENT_ID, OURA_CLIENT_SECRET, OURA_REDIRECT_URI).\n"
                "Подробности — в README. После этого /oura подключит кольцо."
            )
        else:
            await message.answer("💍 Подключение кольца Oura скоро будет доступно.")
        return

    if db.oura_connected(uid):
        o = db.oura_latest(uid)
        last = ""
        if o:
            line = oura_line(uid)
            last = f"\n\nПоследние данные ({o['date']}):\n{line}" if line else ""
        await message.answer(
            "💍 <b>Кольцо Oura подключено ✅</b>" + last +
            "\n\nОбновить данные — /oura_sync · отключить — /oura_off"
        )
        return

    state = secrets.token_urlsafe(12)
    db.set_setting(f"ourastate:{state}", str(uid))
    url = oura_mod.authorize_url(state)
    await message.answer(
        "💍 <b>Подключим кольцо Oura</b>\n\n"
        "Нажми кнопку ниже, разреши доступ на сайте Oura — и вернись сюда. "
        "Дальше сон, готовность и HRV будут подтягиваться сами.",
        reply_markup=ikb([[("🔗 Подключить Oura", "noop")]]) if False else
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Подключить Oura", url=url)]]),
    )


@router.message(Command("oura_sync"))
async def cmd_oura_sync(message: Message) -> None:
    uid = message.from_user.id
    if not db.oura_connected(uid):
        await message.answer("Кольцо не подключено. Подключить — /oura")
        return
    note = await message.answer("💍 Обновляю данные с кольца…")
    try:
        n = await oura_mod.fetch_and_store(uid)
    except Exception as e:  # noqa: BLE001
        log.exception("Oura sync")
        await note.edit_text(f"⚠️ Не получилось обновить: <code>{html.escape(str(e)[:150])}</code>")
        return
    line = oura_line(uid) or "данных пока нет"
    await note.edit_text(f"✅ Обновил ({n} дн.).\n{line}")


@router.message(Command("oura_off"))
async def cmd_oura_off(message: Message) -> None:
    db.delete_oura_tokens(message.from_user.id)
    await message.answer("💍 Кольцо отключено. Данные в дневнике остаются. Подключить снова — /oura")


# ---------------------------------------------------------------- анкета /profile

class Profile(StatesGroup):
    sex = State()
    age = State()
    height = State()
    weight = State()
    activity = State()
    goal = State()


def _kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


SEX_KB = _kb([["Мужчина", "Женщина"]])
ACT_KB = _kb([[a] for a in nutrition.ACTIVITIES])
GOAL_KB = _kb([list(nutrition.GOALS)])


def _num(text: str, lo: float, hi: float) -> float | None:
    try:
        v = float(text.replace(",", ".").strip())
        return v if lo <= v <= hi else None
    except ValueError:
        return None


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext) -> None:
    await state.set_state(Profile.sex)
    await message.answer(
        "Рассчитаем твою дневную норму КБЖУ и воды. 6 коротких вопросов.\n"
        "(отменить — /cancel)\n\n1️⃣ Пол?", reply_markup=SEX_KB,
    )


@router.message(Profile.sex, F.text.in_(["Мужчина", "Женщина"]))
async def p_sex(message: Message, state: FSMContext) -> None:
    await state.update_data(sex=message.text)
    await state.set_state(Profile.age)
    await message.answer("2️⃣ Возраст (полных лет)?", reply_markup=ReplyKeyboardRemove())


@router.message(Profile.sex)
async def p_sex_bad(message: Message) -> None:
    await message.answer("Выбери кнопкой: Мужчина или Женщина 🙂", reply_markup=SEX_KB)


@router.message(Profile.age)
async def p_age(message: Message, state: FSMContext) -> None:
    v = _num(message.text or "", 10, 100)
    if v is None:
        await message.answer("Напиши возраст числом, например: 34")
        return
    await state.update_data(age=int(v))
    await state.set_state(Profile.height)
    await message.answer("3️⃣ Рост в сантиметрах?")


@router.message(Profile.height)
async def p_height(message: Message, state: FSMContext) -> None:
    v = _num(message.text or "", 120, 230)
    if v is None:
        await message.answer("Напиши рост числом в см, например: 178")
        return
    await state.update_data(height=v)
    await state.set_state(Profile.weight)
    await message.answer("4️⃣ Вес в килограммах?")


@router.message(Profile.weight)
async def p_weight(message: Message, state: FSMContext) -> None:
    v = _num(message.text or "", 30, 300)
    if v is None:
        await message.answer("Напиши вес числом в кг, например: 82 или 82.5")
        return
    await state.update_data(weight=v)
    await state.set_state(Profile.activity)
    await message.answer("5️⃣ Уровень активности?", reply_markup=ACT_KB)


@router.message(Profile.activity, F.text.in_(list(nutrition.ACTIVITIES)))
async def p_activity(message: Message, state: FSMContext) -> None:
    await state.update_data(activity=message.text)
    await state.set_state(Profile.goal)
    await message.answer("6️⃣ Цель?", reply_markup=GOAL_KB)


@router.message(Profile.activity)
async def p_activity_bad(message: Message) -> None:
    await message.answer("Выбери один из вариантов кнопкой 🙂", reply_markup=ACT_KB)


@router.message(Profile.goal, F.text.in_(list(nutrition.GOALS)))
async def p_goal(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    goal = message.text
    t = nutrition.calc_targets(data["sex"], data["age"], data["height"],
                               data["weight"], data["activity"], goal)
    water = nutrition.water_target_ml(data["weight"])
    uid = message.from_user.id
    _ensure(message)
    db.update_user(
        uid, sex=data["sex"], age=data["age"], height_cm=data["height"],
        weight_kg=data["weight"], activity=data["activity"], goal=goal,
        kcal_target=t["kcal_target"], protein_target=t["protein_target"],
        fat_target=t["fat_target"], carb_target=t["carb_target"],
        water_target_ml=water,
    )
    await message.answer(
        f"Готово! 🎯 Твоя дневная норма ({goal.lower()}):\n\n"
        f"🔥 <b>{t['kcal_target']} ккал</b>\n"
        f"🥩 Белки: {t['protein_target']} г\n"
        f"🧈 Жиры: {t['fat_target']} г\n"
        f"🍞 Углеводы: {t['carb_target']} г\n"
        f"💧 Вода (по Чеку): {water} мл\n\n"
        f"<i>Основной обмен ~{t['bmr']} ккал, с активностью ~{t['tdee']} ккал.</i>\n"
        "Подправить: <code>/targets 2000 150 70 200</code>, <code>/water цель 2500</code>\n\n"
        "Теперь присылай фото еды 📸 и задай план тренировок: "
        "<code>/plan пн,ср,пт 18:00</code>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Profile.goal)
async def p_goal_bad(message: Message) -> None:
    await message.answer("Выбери цель кнопкой 🙂", reply_markup=GOAL_KB)


# ---------------------------------------------------------------- еда: фото и текст

@router.message(F.photo)
async def on_photo(message: Message) -> None:
    photo = message.photo[-1]
    buf = await message.bot.download(photo)
    await analyze_and_reply(
        message, image_bytes=buf.read(), media_type="image/jpeg",
        caption=message.caption, source="photo",
    )


@router.message(F.document)
async def on_document(message: Message) -> None:
    doc = message.document
    mt = doc.mime_type or ""
    if mt == "application/pdf":
        # PDF — это не еда: разбираем как бланк анализов.
        if (doc.file_size or 0) > 15_000_000:
            await message.answer("PDF великоват — пришли поменьше или отдельными страницами.")
            return
        buf = await message.bot.download(doc)
        await _process_labs(message, buf.read(), "application/pdf", None)
        return
    if mt not in IMAGE_TYPES:
        await message.answer("Я понимаю фото еды 📸 и бланки анализов (PDF/фото). "
                             "Пришли картинку или опиши еду текстом.")
        return
    if (doc.file_size or 0) > MAX_FILE:
        await message.answer("Файл слишком большой. Пришли это фото обычным способом "
                             "(со сжатием) — так даже лучше.")
        return
    buf = await message.bot.download(doc)
    await analyze_and_reply(
        message, image_bytes=buf.read(), media_type=doc.mime_type,
        caption=message.caption, source="photo",
    )


@router.message(StateFilter(None), F.text)
async def on_text(message: Message) -> None:
    text = (message.text or "").strip()
    low = text.lower()

    m = WATER_RE.match(low)
    if m:
        uid = message.from_user.id
        date, time_ = now_date_time()
        _ensure(message)
        db.add_water(uid, date, time_, int(m.group(1)))
        await message.answer(water_text(uid, date, db.get_user(uid)), reply_markup=WATER_KB)
        return
    if low in ("вода", "💧"):
        uid = message.from_user.id
        date, _ = now_date_time()
        await message.answer(water_text(uid, date, db.get_user(uid)), reply_markup=WATER_KB)
        return

    if text.startswith("/"):
        await message.answer("Не знаю такую команду 🤔 Список команд: /help")
        return
    if len(text) < 3:
        await message.answer("Опиши еду подробнее или пришли фото 📸")
        return
    await analyze_and_reply(message, text=text, source="text")


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Я умею работать с фото еды и текстовыми описаниями 📸\n"
                         "Справка: /help")


# ---------------------------------------------------------------- запуск

def _check_env() -> list[str]:
    problems = []
    if not TELEGRAM_BOT_TOKEN or "ВСТАВЬ" in TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN — токен бота из @BotFather")
    return problems


CLIENT_COMMANDS = [
    BotCommand(command="today", description="Весь день: еда, вода, тренировка"),
    BotCommand(command="week", description="Последние 7 дней"),
    BotCommand(command="train", description="Записать тренировку"),
    BotCommand(command="plan", description="Расписание тренировок"),
    BotCommand(command="water", description="Вода за сегодня"),
    BotCommand(command="supplements", description="БАДы и добавки"),
    BotCommand(command="feel", description="Отметить самочувствие"),
    BotCommand(command="labs", description="Анализы: показатели и динамика"),
    BotCommand(command="oura", description="Кольцо Oura: сон и готовность"),
    BotCommand(command="habits", description="Привычки: Working In"),
    BotCommand(command="profile", description="Рассчитать нормы"),
    BotCommand(command="reminders", description="Напоминания и сводка"),
    BotCommand(command="undo", description="Удалить последнюю запись еды"),
    BotCommand(command="help", description="Справка"),
]


async def main() -> None:
    global MAIN_BOT_ID
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    problems = _check_env()
    if problems:
        print("\n⛔ Сначала заполни файл .env — не хватает:")
        for p in problems:
            print(f"   • {p}")
        print("\nОткрой .env в блокноте, вставь значения и запусти бота ещё раз.\n")
        sys.exit(1)

    db.init_db()
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    main_bot = Bot(token=TELEGRAM_BOT_TOKEN, default=default)
    MAIN_BOT_ID = main_bot.id
    BOTS[main_bot.id] = main_bot
    bots = [main_bot]

    try:
        me = await main_bot.get_me()
    except TelegramUnauthorizedError:
        print("\n⛔ Telegram не принял токен бота. Проверь TELEGRAM_BOT_TOKEN в .env "
              "(токен выдаёт @BotFather) и запусти ещё раз.\n")
        sys.exit(1)

    coach_names = []
    for coach in db.list_coaches():
        try:
            cb = Bot(token=coach["bot_token"], default=default)
            cme = await cb.get_me()
        except TelegramUnauthorizedError:
            print(f"⚠️  Токен бота тренера «{coach.get('name')}» не принят Telegram — пропускаю.")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  Бот тренера «{coach.get('name')}» не запустился: {e}")
            continue
        coach["bot_username"] = cme.username
        db.update_coach(coach["id"], bot_id=cb.id, bot_username=cme.username)
        coach["bot_id"] = cb.id
        COACH_BY_BOT[cb.id] = coach
        BOTS[cb.id] = cb
        bots.append(cb)
        coach_names.append(f"{coach.get('name')} (@{cme.username})")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    for b in bots:
        try:
            await b.set_my_commands(CLIENT_COMMANDS)
        except Exception:  # noqa: BLE001
            pass

    print(f"\n✅ Бот @{me.username} запущен.")
    if coach_names:
        print("🤝 Боты тренеров: " + ", ".join(coach_names))
    provider = config.ACTIVE_PROVIDER
    if provider == "demo":
        print("⚠️  ДЕМО-РЕЖИМ: ИИ-ключ не задан, анализ еды и генерация тренировок выключены.")
        print("    Бесплатный ключ: openrouter.ai → Keys → Create Key,")
        print("    вставь его в .env в строку OPENROUTER_API_KEY= и перезапусти бота.")
    elif provider == "openrouter":
        m = config.OPENROUTER_MODEL
        print(f"ИИ: OpenRouter, модель: {m}"
              + (" (подберу бесплатную автоматически)" if m == "auto" else ""))
    else:
        print(f"ИИ: Claude ({config.MODEL})")

    dash_runner = None
    if config.DASHBOARD_ENABLED:
        try:
            dash_runner = await web_dashboard.start_dashboard(config.DASHBOARD_PORT)
            if config.DASHBOARD_TOKEN:
                dash_url = config.public_url(f"/?key={config.DASHBOARD_TOKEN}",
                                             host="<адрес-этого-сервера>")
                print(f"🖥  Дашборд: {dash_url}")
                print("👥 Кабинеты тренеров: /coach?key=<ключ-кабинета> на том же адресе")
            else:
                print(f"🖥  Дашборд: http://localhost:{config.DASHBOARD_PORT} — открой в браузере.")
        except OSError:
            print(f"⚠️  Порт {config.DASHBOARD_PORT} занят — дашборд не запустился. "
                  "Поменяй DASHBOARD_PORT в .env или закрой другую копию бота.")
    print("Бот работает. Остановить: Ctrl+C\n")

    reminder_task = asyncio.create_task(reminder_loop())
    try:
        await dp.start_polling(*bots)
    finally:
        reminder_task.cancel()
        if dash_runner:
            await dash_runner.cleanup()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
