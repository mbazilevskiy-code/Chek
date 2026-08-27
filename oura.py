"""Интеграция с кольцом Oura (API v2, OAuth2).

Поток: /oura в боте → ссылка авторизации → пользователь разрешает на сайте Oura →
Oura редиректит на /oura/callback?code=...&state=... → меняем код на токены →
ежедневно тянем сон/готовность/HRV/активность.
"""
import json
import time
from datetime import datetime, timedelta

import aiohttp

import config
import db

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API = "https://api.ouraring.com/v2/usercollection"
SCOPES = "email personal daily heartrate"


class OuraError(Exception):
    pass


def authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    q = urlencode({
        "response_type": "code",
        "client_id": config.OURA_CLIENT_ID,
        "redirect_uri": config.OURA_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{q}"


async def _post_token(data: dict) -> dict:
    data = dict(data, client_id=config.OURA_CLIENT_ID, client_secret=config.OURA_CLIENT_SECRET)
    async with aiohttp.ClientSession() as s:
        async with s.post(TOKEN_URL, data=data) as r:
            payload = await r.json()
            if r.status != 200 or "access_token" not in payload:
                raise OuraError(str(payload)[:300])
            return payload


async def exchange_code(code: str) -> dict:
    return await _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.OURA_REDIRECT_URI,
    })


def _store_tokens(uid: int, tok: dict) -> None:
    expires_at = time.time() + int(tok.get("expires_in", 86400)) - 60
    db.save_oura_tokens(uid, tok["access_token"], tok.get("refresh_token", ""), expires_at)


async def complete_auth(uid: int, code: str) -> None:
    _store_tokens(uid, await exchange_code(code))


async def _valid_token(uid: int) -> str:
    t = db.get_oura_tokens(uid)
    if not t:
        raise OuraError("кольцо не подключено")
    if t["expires_at"] and t["expires_at"] > time.time():
        return t["access_token"]
    # обновляем
    tok = await _post_token({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
    _store_tokens(uid, tok)
    return tok["access_token"]


async def _get(session: aiohttp.ClientSession, token: str, path: str, params: dict) -> list[dict]:
    async with session.get(f"{API}/{path}", params=params,
                           headers={"Authorization": f"Bearer {token}"}) as r:
        if r.status == 401:
            raise OuraError("токен отклонён (401)")
        payload = await r.json()
        return payload.get("data", []) if isinstance(payload, dict) else []


async def fetch_workouts(uid: int, date: str) -> list[dict]:
    """Тренировки, записанные кольцом за дату: время начала/конца и расход."""
    token = await _valid_token(uid)
    params = {"start_date": date, "end_date": date}
    async with aiohttp.ClientSession() as s:
        rows = await _get(s, token, "workout", params)
    return [{
        "activity": r.get("activity"),
        "calories": r.get("calories"),
        "start": r.get("start_datetime"),
        "end": r.get("end_datetime"),
        "day": r.get("day"),
    } for r in rows]


def _minutes_of_day(iso: str | None) -> int | None:
    """Час:минута из ISO-времени Oura (оно приходит уже в локальной зоне)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.hour * 60 + dt.minute


def workout_time(rec: dict) -> str | None:
    """Время начала тренировки Oura в виде ЧЧ:ММ."""
    m = _minutes_of_day((rec or {}).get("start"))
    return f"{m // 60:02d}:{m % 60:02d}" if m is not None else None


def workout_duration_min(rec: dict) -> int | None:
    """Длительность тренировки Oura в минутах."""
    try:
        started = datetime.fromisoformat((rec or {}).get("start") or "")
        ended = datetime.fromisoformat((rec or {}).get("end") or "")
    except (TypeError, ValueError):
        return None
    minutes = int(round((ended - started).total_seconds() / 60))
    return minutes if 1 <= minutes <= 600 else None


def match_workout(workouts: list[dict], hhmm: str, duration_min: int) -> dict | None:
    """Тренировка Oura, максимально пересекающаяся по времени с введённой.

    Пользователь называет время на глаз, поэтому берём любое пересечение
    интервалов и выбираем то, где оно больше.
    """
    try:
        h, m = hhmm.split(":")
        user_start = int(h) * 60 + int(m)
    except (AttributeError, ValueError):
        return None
    user_end = user_start + max(1, int(duration_min or 0))

    best, best_overlap = None, 0
    for w in workouts or []:
        start = _minutes_of_day(w.get("start"))
        end = _minutes_of_day(w.get("end"))
        if start is None:
            continue
        if end is None or end <= start:
            end = start + 1
        overlap = min(user_end, end) - max(user_start, start)
        if overlap > best_overlap:
            best, best_overlap = w, overlap
    return best


async def _safe(session, token: str, path: str, params: dict) -> list[dict]:
    """Часть эндпоинтов доступна не во всех тарифах — их просто пропускаем."""
    try:
        return await _get(session, token, path, params)
    except OuraError:
        raise
    except Exception:  # noqa: BLE001
        return []


def _hours(seconds) -> float | None:
    return round(seconds / 3600, 1) if seconds else None


async def fetch_and_store(uid: int, days: int = 14) -> int:
    """Тянет с кольца всё, что отдаёт API, и складывает в oura_daily/oura_workouts.

    Посекундный heartrate намеренно не забираем — он огромный и здесь не нужен.
    Возвращает число дней, по которым что-то пришло.
    """
    token = await _valid_token(uid)
    end = datetime.now().date()
    start = end - timedelta(days=days)
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    by_date: dict[str, dict] = {}

    def slot(day: str) -> dict:
        return by_date.setdefault(day, {})

    async with aiohttp.ClientSession() as s:
        for rec in await _safe(s, token, "daily_readiness", params):
            if rec.get("day"):
                slot(rec["day"])["readiness"] = rec.get("score")

        for rec in await _safe(s, token, "daily_sleep", params):
            if rec.get("day"):
                slot(rec["day"])["sleep_score"] = rec.get("score")

        for rec in await _safe(s, token, "daily_activity", params):
            d = rec.get("day")
            if not d:
                continue
            it = slot(d)
            it["activity_score"] = rec.get("score")
            for key, field in (("steps", "steps"),
                               ("active_kcal", "active_calories"),
                               ("total_kcal", "total_calories"),
                               ("distance_m", "equivalent_walking_distance")):
                if rec.get(field) is not None:
                    it[key] = rec[field]
            mins = sum(rec.get(k) or 0 for k in ("high_activity_minutes",
                                                 "medium_activity_minutes",
                                                 "low_activity_minutes"))
            if mins:
                it["active_min"] = mins

        for rec in await _safe(s, token, "sleep", params):
            d = rec.get("day")
            if not d:
                continue
            it = slot(d)
            for key, field in (("sleep_h", "total_sleep_duration"),
                               ("deep_h", "deep_sleep_duration"),
                               ("rem_h", "rem_sleep_duration"),
                               ("light_h", "light_sleep_duration")):
                h = _hours(rec.get(field))
                if h is not None:
                    it[key] = h
            if rec.get("average_hrv") is not None:
                it["hrv"] = rec["average_hrv"]
            if rec.get("efficiency") is not None:
                it["sleep_efficiency"] = rec["efficiency"]
            if rec.get("average_breath") is not None:
                it["breath_avg"] = round(float(rec["average_breath"]), 1)
            hr = rec.get("lowest_heart_rate") or rec.get("average_heart_rate")
            if hr is not None:
                it["resting_hr"] = hr

        # Ниже — метрики, которых может не быть в тарифе: тогда придёт пусто.
        for rec in await _safe(s, token, "daily_spo2", params):
            d = rec.get("day")
            avg = (rec.get("spo2_percentage") or {}).get("average") if d else None
            if avg is not None:
                slot(d)["spo2_avg"] = round(float(avg), 1)

        for rec in await _safe(s, token, "daily_stress", params):
            d = rec.get("day")
            if not d:
                continue
            it = slot(d)
            if rec.get("stress_high") is not None:
                it["stress_high_min"] = int(rec["stress_high"] // 60)
            if rec.get("day_summary"):
                it["stress_summary"] = rec["day_summary"]

        for rec in await _safe(s, token, "daily_resilience", params):
            if rec.get("day") and rec.get("level"):
                slot(rec["day"])["resilience"] = rec["level"]

        for rec in await _safe(s, token, "daily_cardiovascular_age", params):
            if rec.get("day") and rec.get("vascular_age") is not None:
                slot(rec["day"])["cardio_age"] = rec["vascular_age"]

        for rec in await _safe(s, token, "vO2_max", params):
            if rec.get("day") and rec.get("vo2_max") is not None:
                slot(rec["day"])["vo2_max"] = round(float(rec["vo2_max"]), 1)

        for rec in await _safe(s, token, "sleep_time", params):
            d = rec.get("day")
            if not d:
                continue
            extra = {k: rec.get(k) for k in ("optimal_bedtime", "recommendation", "status")
                     if rec.get(k) is not None}
            if extra:
                slot(d)["extra_json"] = json.dumps({"sleep_time": extra}, ensure_ascii=False)

        # Тренировки — в отдельную таблицу, дедуп по id из Oura.
        for rec in await _safe(s, token, "workout", params):
            if not rec.get("id") or not rec.get("day"):
                continue
            db.upsert_oura_workout(uid, {
                "oura_id": rec["id"], "day": rec["day"],
                "start": rec.get("start_datetime"), "end": rec.get("end_datetime"),
                "activity": rec.get("activity"), "intensity": rec.get("intensity"),
                "calories": rec.get("calories"), "distance": rec.get("distance"),
            })

    for d, fields in by_date.items():
        db.upsert_oura_daily(uid, d, **fields)
    return len(by_date)
