"""Разговорный ассистент тренера: один чат вместо команд.

Клиент пишет, говорит или шлёт фото — ассистент сам понимает, записывает,
правит по просьбе и отвечает фактами. Интерпретации и советы не даёт: это
работа тренера, ассистент только собирает данные и подсвечивает важное.

Слой данных не меняется — инструменты здесь тонкие обёртки над db/analyzer.
"""
import json
import logging
import secrets
from datetime import datetime, timedelta

import analyzer
import config
import db
import nutrition
import oura as oura_mod
import whoop as whoop_mod
import web_dashboard

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5          # столько раз подряд агент может дёрнуть инструменты
MAX_TOKENS = 600
HISTORY_TURNS = 15


# ---------------------------------------------------------------- персона

PERSONA = """Ты — ассистент тренера {coach}. Работаешь в его сервисе «{brand}» и общаешься \
с его клиентом в Telegram.

КАК ГОВОРИШЬ
Тепло, по-русски, на «ты», коротко и по-человечески — как живой помощник, а не программа. \
Одна-две фразы обычно достаточно. Без канцелярита, без «запрос обработан», без «неизвестный \
формат», без «это не команда». Если человек просто болтает, здоровается или благодарит — \
поддержи разговор, ничего не записывая.

ЧТО ДЕЛАЕШЬ
Записываешь то, что человек рассказывает о себе: еду, воду, тренировки, самочувствие, БАДы, \
вес. Записывай ТОЛЬКО то, что он явно сказал. Не выдумывай числа и не додумывай. Если не \
понял — задай один короткий уточняющий вопрос. Каждую сделанную запись подтверждай одной \
фразой, чтобы человек видел, что ты понял правильно.
Если просит исправить или удалить — правь через edit_last / delete_last, не создавай дубль.

ФАКТЫ — ДА, СОВЕТЫ — НЕТ
На вопросы о его собственных данных отвечай фактами: сколько съел, сколько выпил, как спал, \
что записано за неделю. Для этого есть get_summary.
Но советы, разборы, «что мне есть», «как тренироваться», «что значат мои анализы», оценки \
питания — не твоя работа. Это работа тренера {coach}. Мягко переведи на него: скажи, что \
это лучше обсудить с {coach}, и вызови flag_for_trainer, чтобы он увидел вопрос. Не \
отказывай сухо — переведи тепло.
Ты не ставишь диагнозов и не назначаешь лечение или добавки. Никогда.

НИКАКИХ КОМАНД И РАЗДЕЛОВ
Ты — живой собеседник, а не меню. Никогда не отправляй человека «в раздел», не проси \
«нажать кнопку», не называй команды со слэшем и не упоминай слова «команда», «раздел», \
«меню», «профиль», «анкета». Всё, что нужно, спрашивай и делай сам прямо в разговоре. \
Если человеку что-то нужно — сделай это инструментом, а не объясняй, куда ему пойти.

ПРОФИЛЬ — ПО ХОДУ РАЗГОВОРА
Пользоваться тобой можно сразу, даже когда о человеке ничего не известно. Недостающее \
(пол, возраст, рост, вес, цель) добирай постепенно, в естественные моменты и по одному: \
например, записав первую еду — «кстати, чтобы посчитать твою норму, сколько ты весишь?». \
Не вываливай список вопросов и не настаивай: не ответил — спокойно спроси в другой раз. \
Каждое названное значение сохраняй через update_profile. Когда данных хватит, нормы \
посчитаются сами — скажи об этом по-человечески, без таблиц.

ГАДЖЕТЫ
Если к слову зайдёт разговор про носимые устройства — спроси, есть ли у человека кольцо Oura или браслет WHOOP, и предложи подключить: инструменты get_oura_link и get_whoop_link дадут ссылку. Если подключение недоступно, скажи об этом честно и не обещай.

ЖИВАЯ РЕЧЬ
Не повторяй одну и ту же фразу-шаблон. Подтверждай по-разному и коротко: «ага, записал», \
«понял тебя», «есть», «отметил». Формулировки меняй, подстраивайся под тон человека.

ЧЕСТНОСТЬ
Если спросят, бот ли ты, — ответь честно: ты программа-ассистент, которую настроил тренер \
{coach}. Не притворяйся человеком.
Слово «Чек» и название платформы клиенту не произноси: для него ты ассистент {coach}.

Сегодня {today}."""


def _persona(coach: dict | None) -> str:
    name = (coach or {}).get("name") or "твоего тренера"
    brand = (coach or {}).get("brand") or "тренировочный дневник"
    return PERSONA.format(coach=name, brand=brand,
                          today=datetime.now().strftime("%Y-%m-%d, %A"))


# ---------------------------------------------------------------- рабочий контекст

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


def context_block(uid: int) -> str:
    """Свежий срез по клиенту — чтобы агент не переспрашивал очевидное."""
    user = db.get_user(uid) or {}
    date = _today()
    lines = ["ЧТО Я ЗНАЮ О КЛИЕНТЕ (для тебя, не пересказывай целиком):"]

    prof = []
    for key, label in (("sex", "пол"), ("age", "возраст"), ("height_cm", "рост"),
                       ("weight_kg", "вес"), ("goal", "цель")):
        if user.get(key):
            prof.append(f"{label} {user[key]}")
    lines.append("Профиль: " + (", ".join(prof) if prof else "не заполнен"))
    if user.get("kcal_target"):
        lines.append(f"Нормы: {user['kcal_target']} ккал, Б{user.get('protein_target')}/"
                     f"Ж{user.get('fat_target')}/У{user.get('carb_target')}, "
                     f"вода {user.get('water_target_ml') or nutrition.water_target_ml(user.get('weight_kg'))} мл")

    meals = db.meals_for_date(uid, date)
    if meals:
        t = nutrition.day_totals(meals)
        lines.append(f"Сегодня еда: {t['n']} записей, {round(t['kcal'])} ккал "
                     f"(Б{round(t['protein'])}/Ж{round(t['fat'])}/У{round(t['carbs'])}). "
                     + "; ".join(f"{m['time']} {m['dish']}" for m in meals[-5:]))
    else:
        lines.append("Сегодня еда: записей нет")

    water = db.water_total(uid, date)
    lines.append(f"Сегодня вода: {water} мл")

    wo = db.workout_for_date(uid, date)
    if wo:
        bits = [wo["status"]]
        if wo["duration_min"]:
            bits.append(f"{wo['duration_min']} мин")
        if wo["description"]:
            bits.append(wo["description"])
        lines.append("Сегодня тренировка: " + ", ".join(str(b) for b in bits))
    else:
        lines.append("Сегодня тренировка: не записана")

    wb = db.get_wellbeing(uid, date)
    if wb:
        bits = [f"{k} {wb[k]}" for k in ("energy", "mood", "stress", "sleep_h") if wb.get(k)]
        lines.append("Сегодня самочувствие: " + (", ".join(bits) or "есть заметка"))

    supps = db.list_supplements(uid)
    if supps:
        taken = db.taken_supplements(uid, date)
        lines.append("БАДы: " + ", ".join(
            f"{s['name']}{' ✓' if s['id'] in taken else ''}" for s in supps))

    weights = db.weight_history(uid, 2)
    if weights:
        lines.append("Вес: " + ", ".join(f"{w['kg']:g} ({w['date']})" for w in weights))

    lines.append("Кольцо Oura: " + ("подключено" if db.oura_connected(uid) else "не подключено"))
    lines.append("Браслет WHOOP: " + ("подключён" if db.whoop_connected(uid) else "не подключён"))
    wh = db.whoop_latest(uid)
    if wh:
        bits = [f"{k} {wh[k]}" for k in ("recovery", "strain", "sleep_h", "hrv")
                if wh.get(k) is not None]
        if bits:
            lines.append("WHOOP, последние данные: " + ", ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------- расход тренировки

async def workout_kcal(uid: int, date: str, hhmm: str, minutes: int) -> tuple[int, str]:
    """Расход: число из кольца, если тренировка в нём есть, иначе оценка по MET."""
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
    if db.whoop_connected(uid):
        stored = whoop_mod.match_workout(db.whoop_workouts_for_date(uid, date), hhmm, minutes)
        if stored and stored.get("calories"):
            return int(round(float(stored["calories"]))), "whoop"
    user = db.get_user(uid) or {}
    return nutrition.workout_kcal_estimate(user.get("weight_kg"), minutes), "estimate"


# ---------------------------------------------------------------- инструменты

TOOLS = [
    {"name": "save_meal",
     "description": "Записать приём пищи. description — что человек съел, его словами.",
     "input_schema": {"type": "object",
                      "properties": {"description": {"type": "string"}},
                      "required": ["description"]}},
    {"name": "save_water",
     "description": "Записать выпитую воду в миллилитрах. Стакан ~250, кружка ~300, бутылка ~500.",
     "input_schema": {"type": "object", "properties": {"ml": {"type": "integer"}},
                      "required": ["ml"]}},
    {"name": "save_weight",
     "description": "Записать вес человека в килограммах.",
     "input_schema": {"type": "object", "properties": {"kg": {"type": "number"}},
                      "required": ["kg"]}},
    {"name": "save_wellbeing",
     "description": ("Записать самочувствие. Оценки по шкале 1-10, только те, о которых сказали. "
                     "sleep_h — сколько часов спал."),
     "input_schema": {"type": "object", "properties": {
         "energy": {"type": "integer"}, "mood": {"type": "integer"},
         "stress": {"type": "integer"}, "sleep_h": {"type": "number"},
         "note": {"type": "string"}}}},
    {"name": "save_workout",
     "description": ("Записать сделанную тренировку. time — ЧЧ:ММ, если названо, иначе не "
                     "указывай. duration_min — минуты. description — что делал."),
     "input_schema": {"type": "object", "properties": {
         "time": {"type": "string"}, "duration_min": {"type": "integer"},
         "description": {"type": "string"}}}},
    {"name": "add_supplement",
     "description": "Добавить БАД или витамин в список того, что человек принимает.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "timing": {"type": "string"}},
         "required": ["name"]}},
    {"name": "log_supplement_taken",
     "description": "Отметить, что человек принял добавку сегодня.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "remove_supplement",
     "description": "Убрать БАД из списка.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "edit_last",
     "description": ("Исправить последнюю запись за сегодня выбранного типа. "
                     "entry_type: meal, water, workout, wellbeing, weight. "
                     "В fields — только те поля, которые надо поменять."),
     "input_schema": {"type": "object", "properties": {
         "entry_type": {"type": "string",
                        "enum": ["meal", "water", "workout", "wellbeing", "weight"]},
         "fields": {"type": "object", "properties": {
             "description": {"type": "string"}, "ml": {"type": "integer"},
             "time": {"type": "string"}, "duration_min": {"type": "integer"},
             "energy": {"type": "integer"}, "mood": {"type": "integer"},
             "stress": {"type": "integer"}, "sleep_h": {"type": "number"},
             "kg": {"type": "number"}}}},
         "required": ["entry_type", "fields"]}},
    {"name": "delete_last",
     "description": "Удалить последнюю запись за сегодня выбранного типа.",
     "input_schema": {"type": "object", "properties": {
         "entry_type": {"type": "string",
                        "enum": ["meal", "water", "workout", "wellbeing", "weight"]}},
         "required": ["entry_type"]}},
    {"name": "get_summary",
     "description": "Факты по данным клиента: period = today или week.",
     "input_schema": {"type": "object", "properties": {
         "period": {"type": "string", "enum": ["today", "week"]}},
         "required": ["period"]}},
    {"name": "update_profile",
     "description": ("Обновить профиль: пол (Мужчина/Женщина), возраст, рост в см, вес в кг, "
                     "активность, цель (Похудеть/Поддерживать/Набрать). Нормы пересчитаются."),
     "input_schema": {"type": "object", "properties": {
         "sex": {"type": "string"}, "age": {"type": "integer"},
         "height_cm": {"type": "number"}, "weight_kg": {"type": "number"},
         "activity": {"type": "string"}, "goal": {"type": "string"}}}},
    {"name": "get_oura_link",
     "description": "Ссылка для подключения кольца Oura.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_schedule",
     "description": ("Расписание тренировок человека: в какие дни и во сколько. "
                     "Отвечай на вопросы вроде «когда у меня тренировка»."),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_supplements_today",
     "description": ("Список добавок человека с временем приёма и отметками, что уже "
                     "принято сегодня."),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_my_dashboard_link",
     "description": ("Ссылка на личную страничку клиента с его прогрессом: графики, "
                     "самочувствие, анализы, разбор недели. Давай, когда человек хочет "
                     "посмотреть свои цифры, прогресс или графики."),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_whoop_link",
     "description": ("Ссылка для подключения браслета WHOOP. Давай, когда человек говорит, "
                     "что у него WHOOP, или просит подключить браслет."),
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "flag_for_trainer",
     "description": ("Подсветить тренеру вопрос, жалобу или тревогу клиента. Вызывай, когда "
                     "человек просит совет, спрашивает про питание, тренировки или анализы."),
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                      "required": ["text"]}},
]


async def _tool_save_meal(uid: int, args: dict) -> dict:
    text = (args.get("description") or "").strip()
    if not text:
        return {"ok": False, "error": "не понял, что именно съел"}
    data = await analyzer.analyze_meal(text=text)
    date, time_ = _today(), _now_hhmm()
    db.add_meal(uid, date, time_, "text",
                dish=str(data.get("dish") or "Приём пищи"),
                grams=float(data.get("total_grams") or 0),
                kcal=float(data.get("total_kcal") or 0),
                protein=float(data.get("total_protein_g") or 0),
                fat=float(data.get("total_fat_g") or 0),
                carbs=float(data.get("total_carbs_g") or 0),
                chek_score=int(data.get("chek_score") or 5),
                chek_verdict=str(data.get("chek_verdict") or ""), raw=data)
    totals = nutrition.day_totals(db.meals_for_date(uid, date))
    return {"ok": True, "dish": data.get("dish"), "kcal": round(float(data.get("total_kcal") or 0)),
            "protein": round(float(data.get("total_protein_g") or 0)),
            "fat": round(float(data.get("total_fat_g") or 0)),
            "carbs": round(float(data.get("total_carbs_g") or 0)),
            "chek_score": data.get("chek_score"),
            "day_kcal": round(totals["kcal"])}


async def _tool_save_water(uid: int, args: dict) -> dict:
    ml = int(args.get("ml") or 0)
    if not 10 <= ml <= 5000:
        return {"ok": False, "error": "странный объём"}
    date = _today()
    db.add_water(uid, date, _now_hhmm(), ml)
    user = db.get_user(uid) or {}
    target = user.get("water_target_ml") or nutrition.water_target_ml(user.get("weight_kg"))
    return {"ok": True, "ml": ml, "day_total": db.water_total(uid, date), "target": target}


async def _tool_save_weight(uid: int, args: dict) -> dict:
    kg = float(args.get("kg") or 0)
    if not 20 <= kg <= 400:
        return {"ok": False, "error": "неправдоподобный вес"}
    prev = (db.get_user(uid) or {}).get("weight_kg")
    db.add_weight(uid, _today(), kg)
    db.update_user(uid, weight_kg=kg)
    return {"ok": True, "kg": kg, "previous": prev}


async def _tool_save_wellbeing(uid: int, args: dict) -> dict:
    fields = {}
    for key in ("energy", "mood", "stress"):
        if args.get(key):
            fields[key] = max(1, min(10, int(args[key])))
    if args.get("sleep_h"):
        fields["sleep_h"] = round(float(args["sleep_h"]), 1)
    if args.get("note"):
        fields["note"] = str(args["note"])[:500]
    if not fields:
        return {"ok": False, "error": "нечего записывать"}
    db.set_wellbeing(uid, _today(), **fields)
    return {"ok": True, **fields}


async def _tool_save_workout(uid: int, args: dict) -> dict:
    date = _today()
    minutes = int(args.get("duration_min") or 0) or None
    hhmm = (args.get("time") or "").strip() or _now_hhmm()
    kcal, source = (0, "")
    if minutes:
        kcal, source = await workout_kcal(uid, date, hhmm, minutes)
    desc = (args.get("description") or "").strip()[:500]
    db.add_workout_log(uid, date, hhmm, "done", note="agent", duration_min=minutes,
                       description=desc, kcal_burned=kcal or None, kcal_source=source)
    return {"ok": True, "time": hhmm, "duration_min": minutes,
            "description": desc, "kcal": kcal or None, "kcal_source": source}


def _find_supplement(uid: int, name: str):
    name = (name or "").strip().lower()
    for s in db.list_supplements(uid):
        if name and (name in s["name"].lower() or s["name"].lower() in name):
            return s
    return None


async def _tool_add_supplement(uid: int, args: dict) -> dict:
    name = (args.get("name") or "").strip()[:80]
    if not name:
        return {"ok": False, "error": "не понял название"}
    if _find_supplement(uid, name):
        return {"ok": True, "already": True, "name": name}
    db.add_supplement(uid, name, (args.get("timing") or "").strip()[:40])
    return {"ok": True, "name": name}


async def _tool_log_supplement_taken(uid: int, args: dict) -> dict:
    name = (args.get("name") or "").strip()[:80]
    date = _today()
    found = _find_supplement(uid, name)
    if found is None:
        sid = db.add_supplement(uid, name, "")
    else:
        sid = found["id"]
    if sid not in db.taken_supplements(uid, date):
        db.toggle_supplement_taken(uid, date, sid)
    return {"ok": True, "name": name,
            "taken_today": len(db.taken_supplements(uid, date)),
            "total": len(db.list_supplements(uid))}


async def _tool_remove_supplement(uid: int, args: dict) -> dict:
    found = _find_supplement(uid, args.get("name") or "")
    if found is None:
        return {"ok": False, "error": "такого в списке нет"}
    db.deactivate_supplement(uid, found["id"])
    return {"ok": True, "name": found["name"]}


async def _tool_delete_last(uid: int, args: dict) -> dict:
    date = _today()
    kind = args.get("entry_type")
    if kind == "meal":
        gone = db.delete_last_meal(uid, date)
        return {"ok": bool(gone), "removed": (gone or {}).get("dish")}
    if kind == "water":
        ml = db.delete_last_water(uid, date)
        return {"ok": bool(ml), "removed_ml": ml}
    if kind == "workout":
        gone = db.delete_last_workout(uid, date)
        return {"ok": bool(gone)}
    if kind == "wellbeing":
        return {"ok": db.delete_wellbeing(uid, date)}
    if kind == "weight":
        kg = db.delete_last_weight(uid)
        return {"ok": bool(kg), "removed_kg": kg}
    return {"ok": False, "error": "неизвестный тип записи"}


async def _tool_edit_last(uid: int, args: dict) -> dict:
    date = _today()
    kind = args.get("entry_type")
    fields = args.get("fields") or {}

    if kind == "water":
        ml = int(fields.get("ml") or 0)
        if not ml:
            return {"ok": False, "error": "не понял новый объём"}
        db.delete_last_water(uid, date)
        db.add_water(uid, date, _now_hhmm(), ml)
        return {"ok": True, "ml": ml, "day_total": db.water_total(uid, date)}

    if kind == "workout":
        old = db.delete_last_workout(uid, date)
        if not old:
            return {"ok": False, "error": "сегодня тренировок не записано"}
        minutes = int(fields.get("duration_min") or old["duration_min"] or 0) or None
        hhmm = (fields.get("time") or old["time"] or _now_hhmm())
        desc = (fields.get("description") or old["description"] or "")[:500]
        kcal, source = (0, "")
        if minutes:
            kcal, source = await workout_kcal(uid, date, hhmm, minutes)
        db.add_workout_log(uid, date, hhmm, "done", note="agent", duration_min=minutes,
                           description=desc, kcal_burned=kcal or None, kcal_source=source)
        return {"ok": True, "time": hhmm, "duration_min": minutes, "description": desc,
                "kcal": kcal or None, "kcal_source": source}

    if kind == "meal":
        desc = (fields.get("description") or "").strip()
        db.delete_last_meal(uid, date)
        if not desc:
            return {"ok": True, "removed": True}
        return await _tool_save_meal(uid, {"description": desc})

    if kind == "wellbeing":
        return await _tool_save_wellbeing(uid, fields)

    if kind == "weight":
        kg = float(fields.get("kg") or 0)
        if not kg:
            return {"ok": False, "error": "не понял новый вес"}
        db.delete_last_weight(uid)
        return await _tool_save_weight(uid, {"kg": kg})

    return {"ok": False, "error": "неизвестный тип записи"}


async def _tool_get_summary(uid: int, args: dict) -> dict:
    period = args.get("period") or "today"
    date = _today()
    if period == "today":
        meals = db.meals_for_date(uid, date)
        t = nutrition.day_totals(meals)
        user = db.get_user(uid) or {}
        out = {
            "meals": [{"time": m["time"], "dish": m["dish"], "kcal": round(m["kcal"] or 0)}
                      for m in meals],
            "kcal": round(t["kcal"]), "protein": round(t["protein"]),
            "fat": round(t["fat"]), "carbs": round(t["carbs"]),
            "chek": round(t["chek"], 1) if t["chek"] else None,
            "kcal_target": user.get("kcal_target"),
            "water_ml": db.water_total(uid, date),
            "water_target": user.get("water_target_ml")
            or nutrition.water_target_ml(user.get("weight_kg")),
        }
        wo = db.workout_for_date(uid, date)
        if wo:
            out["workout"] = {"status": wo["status"], "duration_min": wo["duration_min"],
                              "kcal_burned": wo["kcal_burned"], "description": wo["description"]}
        wb = db.get_wellbeing(uid, date)
        if wb:
            out["wellbeing"] = {k: wb[k] for k in ("energy", "mood", "stress", "sleep_h")
                                if wb.get(k)}
        return out

    s = web_dashboard.build_summary(7, uid=uid)
    if not s.get("ok"):
        return {"ok": False, "error": "нет данных"}
    return {
        "days": [{"date": d["date"], "kcal": d["kcal"], "water": d["water"],
                  "chek": d["chek"], "workout": d["workout"]} for d in s["series"]],
        "avg_kcal": s["stats"]["avg_kcal"], "avg_chek": s["stats"]["avg_chek"],
        "workouts_done": s["stats"]["workouts_done"],
        "kcal_target": s["targets"]["kcal"], "water_target": s["targets"]["water"],
    }


async def _tool_update_profile(uid: int, args: dict) -> dict:
    fields = {}
    for key in ("sex", "activity", "goal"):
        if args.get(key):
            fields[key] = str(args[key])
    for key in ("age",):
        if args.get(key):
            fields[key] = int(args[key])
    for key in ("height_cm", "weight_kg"):
        if args.get(key):
            fields[key] = float(args[key])
    if not fields:
        return {"ok": False, "error": "нечего обновлять"}
    db.update_user(uid, **fields)
    if args.get("weight_kg"):
        db.add_weight(uid, _today(), float(args["weight_kg"]))

    user = db.get_user(uid) or {}
    if all(user.get(k) for k in ("sex", "age", "height_cm", "weight_kg", "goal")):
        targets = nutrition.calc_targets(user["sex"], user["age"], user["height_cm"],
                                         user["weight_kg"],
                                         user.get("activity") or "Лёгкая (1–3 тренировки в неделю)",
                                         user["goal"])
        db.update_user(uid, kcal_target=targets["kcal_target"],
                       protein_target=targets["protein_target"],
                       fat_target=targets["fat_target"], carb_target=targets["carb_target"],
                       water_target_ml=nutrition.water_target_ml(user["weight_kg"]))
        return {"ok": True, "saved": fields, "targets": targets,
                "water": nutrition.water_target_ml(user["weight_kg"])}
    missing = [k for k in ("sex", "age", "height_cm", "weight_kg", "goal") if not user.get(k)]
    return {"ok": True, "saved": fields, "missing_for_targets": missing}


async def _tool_get_oura_link(uid: int, args: dict) -> dict:
    if not config.OURA_ENABLED:
        return {"ok": False, "error": "кольцо на этом сервере не настроено"}
    state = secrets.token_urlsafe(16)
    db.set_setting(f"ourastate:{state}", str(uid))
    return {"ok": True, "url": oura_mod.authorize_url(state)}


async def _tool_flag_for_trainer(uid: int, args: dict) -> dict:
    text = (args.get("text") or "").strip()
    if not text:
        return {"ok": False}
    db.add_trainer_note(uid, _today(), text)
    return {"ok": True}


_DOW = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


async def _tool_get_schedule(uid: int, args: dict) -> dict:
    plan = db.get_workout_plan(uid)
    if not plan:
        return {"ok": True, "planned": False}
    today = datetime.now()
    items, nearest = [], None
    for p in plan:
        items.append({"day": _DOW[p["dow"]], "time": p["time"]})
        ahead = (p["dow"] - today.weekday()) % 7
        when = (today + timedelta(days=ahead)).replace(
            hour=int(p["time"][:2]), minute=int(p["time"][3:]), second=0, microsecond=0)
        if when < today:
            when += timedelta(days=7)
        if nearest is None or when < nearest:
            nearest = when
    return {"ok": True, "planned": True, "schedule": items,
            "next_date": nearest.strftime("%Y-%m-%d"),
            "next_day": _DOW[nearest.weekday()],
            "next_time": nearest.strftime("%H:%M"),
            "days_ahead": (nearest.date() - today.date()).days}


async def _tool_get_supplements_today(uid: int, args: dict) -> dict:
    date = _today()
    supps = db.list_supplements(uid)
    if not supps:
        return {"ok": True, "any": False}
    taken = db.taken_supplements(uid, date)
    return {"ok": True, "any": True, "items": [
        {"name": s["name"], "timing": s["timing"] or "",
         "plan_days_per_week": s["plan_days_per_week"] or 7,
         "taken_today": s["id"] in taken} for s in supps],
        "taken": len(taken), "total": len(supps)}


async def _tool_get_my_dashboard_link(uid: int, args: dict) -> dict:
    token = db.cabinet_token_for(uid)
    return {"ok": True, "url": config.public_url(f"/me?key={token}")}


async def _tool_get_whoop_link(uid: int, args: dict) -> dict:
    if not config.WHOOP_ENABLED:
        return {"ok": False, "error": "подключение WHOOP временно недоступно"}
    state = secrets.token_urlsafe(16)
    db.set_setting(f"whoopstate:{state}", str(uid))
    return {"ok": True, "url": whoop_mod.authorize_url(state)}


TOOL_IMPL = {
    "get_whoop_link": _tool_get_whoop_link,
    "get_my_dashboard_link": _tool_get_my_dashboard_link,
    "get_schedule": _tool_get_schedule,
    "get_supplements_today": _tool_get_supplements_today,
    "save_meal": _tool_save_meal,
    "save_water": _tool_save_water,
    "save_weight": _tool_save_weight,
    "save_wellbeing": _tool_save_wellbeing,
    "save_workout": _tool_save_workout,
    "add_supplement": _tool_add_supplement,
    "log_supplement_taken": _tool_log_supplement_taken,
    "remove_supplement": _tool_remove_supplement,
    "edit_last": _tool_edit_last,
    "delete_last": _tool_delete_last,
    "get_summary": _tool_get_summary,
    "update_profile": _tool_update_profile,
    "get_oura_link": _tool_get_oura_link,
    "flag_for_trainer": _tool_flag_for_trainer,
}


# ---------------------------------------------------------------- цикл

def available() -> bool:
    """Агент работает только на Claude: tool-use у бесплатных моделей ненадёжен."""
    return config.AGENT_MODE and config.ACTIVE_PROVIDER == "anthropic"


def _history_messages(uid: int) -> list[dict]:
    out = []
    for row in db.chat_tail(uid, HISTORY_TURNS):
        role = "assistant" if row["role"] == "assistant" else "user"
        out.append({"role": role, "content": row["content"][:2000]})
    # Модель не примет историю, начинающуюся с ответа ассистента.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


async def handle_message(uid: int, coach: dict | None, user_text: str,
                         *, system_note: str | None = None) -> tuple[str, str]:
    """Сообщение клиента → (ответ, ссылка-кнопка или пустая строка)."""
    if not available():
        raise analyzer.DemoModeError()

    text = (user_text or "").strip()
    if not text:
        return "", ""

    system = _persona(coach) + "\n\n" + context_block(uid)
    if system_note:
        system += "\n\nТОЛЬКО ЧТО ПРОИЗОШЛО: " + system_note

    messages = _history_messages(uid)
    messages.append({"role": "user", "content": text})

    client = analyzer._client()
    used_tools: list[str] = []
    reply = ""
    link = ""

    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.messages.create(
            model=config.MODEL, max_tokens=MAX_TOKENS, system=system,
            tools=TOOLS, messages=messages,
        )
        calls = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        said = " ".join(getattr(b, "text", "") for b in resp.content
                        if getattr(b, "type", None) == "text").strip()
        if not calls:
            reply = said
            break

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for call in calls:
            used_tools.append(call.name)
            impl = TOOL_IMPL.get(call.name)
            try:
                result = await impl(uid, dict(call.input)) if impl else {"ok": False,
                                                                         "error": "нет такого инструмента"}
            except analyzer.DemoModeError:
                result = {"ok": False, "error": "разбор еды сейчас недоступен"}
            except Exception as e:  # noqa: BLE001
                log.exception("Инструмент %s упал", call.name)
                result = {"ok": False, "error": str(e)[:200]}
            if call.name in ("get_my_dashboard_link", "get_oura_link",
                             "get_whoop_link") and result.get("url"):
                link = result["url"]
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False)})
        messages.append({"role": "user", "content": results})
    else:
        reply = reply or "Записал 🙂"

    if not reply:
        reply = "Записал 🙂"
    log.info("agent uid=%s инструменты=%s", uid, used_tools or "нет")
    return reply.strip(), link


def store_meal(uid: int, data: dict, source: str = "photo") -> dict:
    """Сохраняет уже разобранный приём пищи. Общая точка для фото и текста."""
    date, time_ = _today(), _now_hhmm()
    db.add_meal(uid, date, time_, source,
                dish=str(data.get("dish") or "Приём пищи"),
                grams=float(data.get("total_grams") or 0),
                kcal=float(data.get("total_kcal") or 0),
                protein=float(data.get("total_protein_g") or 0),
                fat=float(data.get("total_fat_g") or 0),
                carbs=float(data.get("total_carbs_g") or 0),
                chek_score=int(data.get("chek_score") or 5),
                chek_verdict=str(data.get("chek_verdict") or ""), raw=data)
    totals = nutrition.day_totals(db.meals_for_date(uid, date))
    return {"dish": data.get("dish"), "kcal": round(float(data.get("total_kcal") or 0)),
            "protein": round(float(data.get("total_protein_g") or 0)),
            "fat": round(float(data.get("total_fat_g") or 0)),
            "carbs": round(float(data.get("total_carbs_g") or 0)),
            "chek_score": data.get("chek_score"), "day_kcal": round(totals["kcal"])}
