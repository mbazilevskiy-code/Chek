"""Разовый анонс пользователям: «что нового».

Мульти-тенант: каждому пишет тот бот, в котором он зарегистрирован. Клиентам
тренера — только с их согласием и нейтральным тоном, без упоминания платформы:
для клиента это ассистент его тренера, а не «Чек».

Запускается руками ОДИН раз после выката. Никакой авторассылки на рестарте —
бот про этот модуль не знает.

    ./.venv/bin/python announce.py --dry-run "текст"     # посчитать, кому уйдёт
    ./.venv/bin/python announce.py "текст"               # разослать
    ./.venv/bin/python announce.py --owner-text "..." "текст для клиентов"
"""
import argparse
import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

import config
import db

log = logging.getLogger("announce")

# Telegram разрешает ~30 сообщений в секунду; держимся заметно ниже.
PAUSE_SEC = 0.05


def _owner_ids() -> set[int]:
    """Кому писать из личного бота: только владелец, чужих там нет."""
    ids = set(config.ALLOWED_USER_IDS or ())
    saved = db.get_setting("owner_id")
    if saved and str(saved).isdigit():
        ids.add(int(saved))
    return ids


def plan(text: str, owner_text: str | None = None) -> list[dict]:
    """Кому и от чьего имени писать. Клиенты тренера — только с consent=1."""
    coaches = {c["id"]: c for c in db.list_coaches()}
    owners = _owner_ids()
    targets = []
    for uid in db.all_user_ids():
        user = db.get_user(uid) or {}
        coach_id = user.get("coach_id")
        if coach_id:
            coach = coaches.get(coach_id)
            if not coach or not coach.get("bot_token"):
                continue
            if not user.get("consent"):
                continue          # без согласия не пишем
            targets.append({"uid": uid, "token": coach["bot_token"], "text": text,
                            "via": "@" + (coach.get("bot_username") or str(coach_id))})
        elif uid in owners and config.TELEGRAM_BOT_TOKEN:
            targets.append({"uid": uid, "token": config.TELEGRAM_BOT_TOKEN,
                            "text": owner_text or text, "via": "личный бот"})
    return targets


async def run(text: str, *, owner_text: str | None = None, dry_run: bool = False,
              sender=None, pause: float = PAUSE_SEC) -> dict:
    """Рассылает анонс. sender подменяется в тестах, чтобы не ходить в Telegram."""
    targets = plan(text, owner_text)
    stats = {"planned": len(targets), "sent": 0, "blocked": 0, "failed": 0}
    if dry_run or not targets:
        return stats

    own_bots: dict[str, Bot] = {}
    if sender is None:
        default = DefaultBotProperties(parse_mode=ParseMode.HTML)

        async def sender(token: str, uid: int, body: str) -> None:
            if token not in own_bots:
                own_bots[token] = Bot(token=token, default=default)
            await own_bots[token].send_message(uid, body)

    try:
        for t in targets:
            for attempt in (1, 2):
                try:
                    await sender(t["token"], t["uid"], t["text"])
                    stats["sent"] += 1
                    log.info("отправлено uid=%s (%s)", t["uid"], t["via"])
                    break
                except TelegramRetryAfter as e:
                    # Упёрлись в лимит: ждём столько, сколько просит Telegram.
                    if attempt == 2:
                        stats["failed"] += 1
                        log.warning("uid=%s: лимит не разошёлся", t["uid"])
                        break
                    await asyncio.sleep(getattr(e, "retry_after", 1) or 1)
                except TelegramForbiddenError:
                    stats["blocked"] += 1
                    log.info("пропуск uid=%s: бот заблокирован", t["uid"])
                    break
                except Exception as e:  # noqa: BLE001
                    stats["failed"] += 1
                    log.warning("uid=%s: не доставлено — %s", t["uid"], e)
                    break
            await asyncio.sleep(pause)
    finally:
        for b in own_bots.values():
            await b.session.close()
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Разовый анонс пользователям")
    ap.add_argument("text", help="текст для клиентов (нейтральный, без упоминания платформы)")
    ap.add_argument("--owner-text", default=None, help="отдельный текст для личного бота")
    ap.add_argument("--dry-run", action="store_true", help="только посчитать получателей")
    args = ap.parse_args()

    db.init_db()
    stats = asyncio.run(run(args.text, owner_text=args.owner_text, dry_run=args.dry_run))
    if args.dry_run:
        for t in plan(args.text, args.owner_text):
            print(f"  uid={t['uid']} через {t['via']}")
    print(f"получателей: {stats['planned']} · отправлено: {stats['sent']} · "
          f"заблокировали: {stats['blocked']} · ошибок: {stats['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
