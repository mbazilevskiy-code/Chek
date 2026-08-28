"""Интеграция с браслетом WHOOP (Developer API v2, OAuth2).

Поток такой же, как у Oura: клиент говорит ассистенту «подключи вуп» → ссылка
авторизации → разрешает на сайте WHOOP → редирект на /whoop/callback?code=…&state=…
→ меняем код на токены → ежедневно тянем восстановление, сон, strain и тренировки.

v1 закрыт, работаем только с v2. Токен живёт около часа, поэтому обязателен
scope offline и обновление по refresh_token.
"""
import json
import time
from datetime import datetime, timedelta

import aiohttp

import config
import db

AUTHORIZE_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API = "https://api.prod.whoop.com/developer/v2"
SCOPES = ("offline read:recovery read:sleep read:cycles read:workout "
          "read:profile read:body_measurement")

KJ_PER_KCAL = 4.184


class WhoopError(Exception):
    pass


def authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    q = urlencode({
        "response_type": "code",
        "client_id": config.WHOOP_CLIENT_ID,
        "redirect_uri": config.WHOOP_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{q}"


async def _post_token(data: dict) -> dict:
    data = dict(data, client_id=config.WHOOP_CLIENT_ID,
                client_secret=config.WHOOP_CLIENT_SECRET)
    async with aiohttp.ClientSession() as s:
        async with s.post(TOKEN_URL, data=data) as r:
            payload = await r.json()
            if r.status != 200 or "access_token" not in payload:
                raise WhoopError(str(payload)[:300])
            return payload


async def exchange_code(code: str) -> dict:
    return await _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.WHOOP_REDIRECT_URI,
    })


def _store_tokens(uid: int, tok: dict) -> None:
    expires_at = time.time() + int(tok.get("expires_in", 3600)) - 60
    db.save_whoop_tokens(uid, tok["access_token"], tok.get("refresh_token", ""), expires_at)


async def complete_auth(uid: int, code: str) -> None:
    _store_tokens(uid, await exchange_code(code))


async def _valid_token(uid: int) -> str:
    t = db.get_whoop_tokens(uid)
    if not t:
        raise WhoopError("браслет не подключён")
    if t["expires_at"] and t["expires_at"] > time.time():
        return t["access_token"]
    tok = await _post_token({"grant_type": "refresh_token",
                             "refresh_token": t["refresh_token"],
                             "scope": "offline"})
    _store_tokens(uid, tok)
    return tok["access_token"]


async def _get(session: aiohttp.ClientSession, token: str, path: str,
               params: dict) -> list[dict]:
    """Читает коллекцию v2 со всеми страницами (nextToken)."""
    out, cursor, pages = [], None, 0
    while pages < 10:
        pages += 1
        q = dict(params)
        if cursor:
            q["nextToken"] = cursor
        async with session.get(f"{API}/{path}", params=q,
                               headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status == 401:
                raise WhoopError("токен отклонён (401)")
            payload = await r.json()
        if not isinstance(payload, dict):
            break
        if "records" in payload:
            out.extend(payload.get("records") or [])
            cursor = payload.get("next_token") or payload.get("nextToken")
            if not cursor:
                break
        else:
            out.append(payload)          # одиночный объект, например body measurement
            break
    return out


async def _safe(session, token: str, path: str, params: dict,
                required: bool = False) -> list[dict]:
    """Один недоступный источник не должен ронять весь забор."""
    try:
        return await _get(session, token, path, params)
    except WhoopError:
        if required:
            raise
        return []
    except Exception:  # noqa: BLE001
        return []


def _day(value: str | None) -> str | None:
    """Дата из ISO-времени WHOOP."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _hours(millis) -> float | None:
    return round(millis / 3_600_000, 1) if millis else None


def _kcal(kilojoule) -> int | None:
    return int(round(kilojoule / KJ_PER_KCAL)) if kilojoule else None


def _minutes_of_day(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.hour * 60 + dt.minute


def workout_time(rec: dict) -> str | None:
    m = _minutes_of_day((rec or {}).get("start"))
    return f"{m // 60:02d}:{m % 60:02d}" if m is not None else None


def workout_duration_min(rec: dict) -> int | None:
    try:
        started = datetime.fromisoformat((rec or {}).get("start", "").replace("Z", "+00:00"))
        ended = datetime.fromisoformat((rec or {}).get("end", "").replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    minutes = int(round((ended - started).total_seconds() / 60))
    return minutes if 1 <= minutes <= 600 else None


def match_workout(workouts: list[dict], hhmm: str, duration_min: int) -> dict | None:
    """Тренировка WHOOP, максимально пересекающаяся по времени с названной."""
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


async def fetch_and_store(uid: int, days: int = 14) -> int:
    """Тянет данные браслета и складывает в whoop_daily / whoop_workouts."""
    token = await _valid_token(uid)
    end = datetime.now()
    start = end - timedelta(days=days)
    params = {"start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
              "limit": 25}
    by_date: dict[str, dict] = {}

    def slot(day: str) -> dict:
        return by_date.setdefault(day, {})

    async with aiohttp.ClientSession() as s:
        # Восстановление: главный показатель WHOOP
        for rec in await _safe(s, token, "recovery", params, required=True):
            day = _day(rec.get("created_at") or rec.get("updated_at"))
            score = rec.get("score") or {}
            if not day or not score:
                continue
            it = slot(day)
            if score.get("recovery_score") is not None:
                it["recovery"] = round(float(score["recovery_score"]))
            if score.get("hrv_rmssd_milli") is not None:
                it["hrv"] = round(float(score["hrv_rmssd_milli"]), 1)
            if score.get("resting_heart_rate") is not None:
                it["resting_hr"] = round(float(score["resting_heart_rate"]), 1)
            if score.get("spo2_percentage") is not None:
                it["spo2"] = round(float(score["spo2_percentage"]), 1)
            if score.get("skin_temp_celsius") is not None:
                it["skin_temp"] = round(float(score["skin_temp_celsius"]), 1)

        # Сон: длительность, фазы, эффективность, дыхание
        for rec in await _safe(s, token, "activity/sleep", params):
            day = _day(rec.get("start"))
            score = rec.get("score") or {}
            if not day or not score:
                continue
            it = slot(day)
            stages = score.get("stage_summary") or {}
            deep = _hours(stages.get("total_slow_wave_sleep_time_milli"))
            rem = _hours(stages.get("total_rem_sleep_time_milli"))
            light = _hours(stages.get("total_light_sleep_time_milli"))
            awake = _hours(stages.get("total_awake_time_milli"))
            in_bed = _hours(stages.get("total_in_bed_time_milli"))
            for key, value in (("deep_h", deep), ("rem_h", rem), ("light_h", light),
                               ("awake_h", awake)):
                if value is not None:
                    it[key] = value
            asleep = sum(x for x in (deep, rem, light) if x)
            if asleep:
                it["sleep_h"] = round(asleep, 1)
            elif in_bed:
                it["sleep_h"] = in_bed
            if score.get("sleep_performance_percentage") is not None:
                it["sleep_perf"] = round(float(score["sleep_performance_percentage"]))
            if score.get("respiratory_rate") is not None:
                it["breath_avg"] = round(float(score["respiratory_rate"]), 1)

        # Цикл: дневной strain и расход
        for rec in await _safe(s, token, "cycle", params):
            day = _day(rec.get("start"))
            score = rec.get("score") or {}
            if not day or not score:
                continue
            it = slot(day)
            if score.get("strain") is not None:
                it["strain"] = round(float(score["strain"]), 1)
            kcal = _kcal(score.get("kilojoule"))
            if kcal:
                it["day_kcal"] = kcal
            if score.get("average_heart_rate") is not None:
                it["avg_hr"] = round(float(score["average_heart_rate"]))
            if score.get("max_heart_rate") is not None:
                it["max_hr"] = round(float(score["max_heart_rate"]))

        # Тренировки — в отдельную таблицу, дедуп по id
        for rec in await _safe(s, token, "activity/workout", params):
            day = _day(rec.get("start"))
            if not rec.get("id") or not day:
                continue
            score = rec.get("score") or {}
            db.upsert_whoop_workout(uid, {
                "whoop_id": rec["id"], "day": day,
                "start": rec.get("start"), "end": rec.get("end"),
                "sport": rec.get("sport_name") or rec.get("sport_id"),
                "strain": round(float(score["strain"]), 1) if score.get("strain") else None,
                "calories": _kcal(score.get("kilojoule")),
                "distance": score.get("distance_meter"),
                "avg_hr": round(float(score["average_heart_rate"]))
                if score.get("average_heart_rate") else None,
            })

        # Телосложение — разово, кладём в extra_json свежего дня
        body = await _safe(s, token, "user/measurement/body", {})
        if body and by_date:
            latest = max(by_date)
            keep = {k: body[0].get(k) for k in
                    ("height_meter", "weight_kilogram", "max_heart_rate")
                    if body[0].get(k) is not None}
            if keep:
                slot(latest)["extra_json"] = json.dumps({"body": keep}, ensure_ascii=False)

    for day, fields in by_date.items():
        db.upsert_whoop_daily(uid, day, **fields)
    return len(by_date)
