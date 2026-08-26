"""Интеграция с кольцом Oura (API v2, OAuth2).

Поток: /oura в боте → ссылка авторизации → пользователь разрешает на сайте Oura →
Oura редиректит на /oura/callback?code=...&state=... → меняем код на токены →
ежедневно тянем сон/готовность/HRV/активность.
"""
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


async def fetch_and_store(uid: int, days: int = 14) -> int:
    """Тянет последние `days` дней и складывает в oura_daily. Возвращает число дней с данными."""
    token = await _valid_token(uid)
    end = datetime.now().date()
    start = end - timedelta(days=days)
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    by_date: dict[str, dict] = {}

    async with aiohttp.ClientSession() as s:
        for rec in await _get(s, token, "daily_readiness", params):
            d = rec.get("day")
            if d:
                by_date.setdefault(d, {})["readiness"] = rec.get("score")
                contrib = rec.get("contributors") or {}
                if contrib.get("resting_heart_rate") is not None:
                    pass  # это баллы, не BPM — пропускаем
        for rec in await _get(s, token, "daily_sleep", params):
            d = rec.get("day")
            if d:
                by_date.setdefault(d, {})["sleep_score"] = rec.get("score")
        for rec in await _get(s, token, "daily_activity", params):
            d = rec.get("day")
            if d:
                by_date.setdefault(d, {})["activity_score"] = rec.get("score")
                if rec.get("steps") is not None:
                    by_date[d]["steps"] = rec.get("steps")
        # подробный сон: длительность, HRV, пульс покоя
        for rec in await _get(s, token, "sleep", params):
            d = rec.get("day")
            if not d:
                continue
            slot = by_date.setdefault(d, {})
            total = rec.get("total_sleep_duration")  # секунды
            if total:
                slot["sleep_h"] = round(total / 3600, 1)
            if rec.get("average_hrv") is not None:
                slot["hrv"] = rec.get("average_hrv")
            if rec.get("average_heart_rate") is not None:
                slot["resting_hr"] = rec.get("lowest_heart_rate") or rec.get("average_heart_rate")

    for d, fields in by_date.items():
        db.upsert_oura_daily(uid, d, **fields)
    return len(by_date)
