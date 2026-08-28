"""Сборка данных для кабинетов по контракту вёрстки.

Форма ответа задана объектом MOCK внутри client.html / coach.html — он же
служит документацией. Пустые разделы не отдаём вовсе: страница сама показывает
«Данных нет». Все человекочитаемые строки (значения анализов, «вчера») готовим
здесь, на сервере, — вёрстка их только выводит.
"""
from datetime import datetime

import db

WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
FLAG_LEVEL = {"green": "ok", "yellow": "warn", "red": "crit"}
SRC_NAME = {"oura": "Oura", "whoop": "WHOOP"}


def num(value) -> str:
    """Число по-русски: 2,1 вместо 2.1, без хвостовых нулей."""
    if value is None:
        return ""
    return f"{float(value):g}".replace(".", ",")


def when_word(date_str: str) -> str:
    """«сегодня» / «вчера» / «26.08» — как ждёт вёрстка."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return date_str or ""
    today = datetime.now().date()
    if d == today:
        return "сегодня"
    if (today - d).days == 1:
        return "вчера"
    return d.strftime("%d.%m")


def _latest_value(rows: dict, key: str):
    """Последнее непустое значение метрики в окне.

    Строка за сегодня часто заводится раньше, чем приходят данные, поэтому
    брать просто максимальную дату нельзя — получим пустые метрики.
    """
    for day in sorted(rows, reverse=True):
        value = (rows[day] or {}).get(key)
        if value is not None:
            return value
    return None


def _gadget_summary(rows: dict, connected: bool, ready_key: str) -> dict:
    """Сводка гаджета для колец.

    Вёрстка форматирует числа без проверки на null, поэтому «подключён»
    выставляем только когда есть чем заполнить: готовность и сон.
    """
    if not connected:
        return {"connected": False}
    readiness = _latest_value(rows, ready_key)
    sleep_h = _latest_value(rows, "sleep_h")
    if readiness is None or sleep_h is None:
        return {"connected": False}
    return {"connected": True, "readiness": readiness, "sleep_h": sleep_h,
            "hrv": _latest_value(rows, "hrv") or 0,
            "rhr": _latest_value(rows, "resting_hr") or 0}


def gadget_blocks(uid: int, dates: list[str]):
    """oura / whoop и главный гаджет: подключённый, а из двух — с более свежими данными."""
    oura_rows = db.oura_range(uid, dates)
    whoop_rows = db.whoop_range(uid, dates)
    oura_on, whoop_on = db.oura_connected(uid), db.whoop_connected(uid)

    oura = _gadget_summary(oura_rows, oura_on, "readiness")
    whoop = _gadget_summary(whoop_rows, whoop_on, "recovery")
    oura_on, whoop_on = oura["connected"], whoop["connected"]

    if oura_on and whoop_on:
        newest_o = max(oura_rows) if oura_rows else ""
        newest_w = max(whoop_rows) if whoop_rows else ""
        primary = "whoop" if newest_w > newest_o else "oura"
    elif whoop_on:
        primary = "whoop"
    else:
        primary = "oura"
    return {"primary": primary, "oura": oura, "whoop": whoop}, oura_rows, whoop_rows


def weight_by_date(uid: int, dates: list[str]) -> dict:
    """Вес на каждый день окна: последний известный на эту дату."""
    history = sorted(db.weight_history(uid, 400), key=lambda w: w["date"])
    out, last, idx = {}, None, 0
    for day in dates:
        while idx < len(history) and history[idx]["date"] <= day:
            last = history[idx]["kg"]
            idx += 1
        out[day] = last
    return out


def labs_block(uid: int) -> dict | None:
    dates = db.lab_dates(uid)
    if not dates:
        return None
    markers = []
    for m in db.latest_markers(uid):
        value = m["value_text"] or num(m["value"])
        if m["unit"]:
            value = f"{value} {m['unit']}".strip()
        ref = ""
        if m["ref_low"] is not None or m["ref_high"] is not None:
            ref = f"{num(m['ref_low'])}–{num(m['ref_high'])}"
        trend = ""
        earlier = [h for h in db.marker_history(uid, m["name"])
                   if h["date"] < m["date"] and h["value"] is not None]
        if earlier and m["value"] is not None:
            prev = earlier[-1]["value"]
            arrow = "↑" if m["value"] > prev else ("↓" if m["value"] < prev else "=")
            trend = f"{arrow} было {num(prev)}"
        markers.append({"name": m["name"], "value": value, "ref": ref,
                        "flag": m["flag"] or "норма", "trend": trend})
    if not markers:
        return None
    markers.sort(key=lambda r: 0 if r["flag"] in ("низко", "высоко") else 1)
    return {"last_date": when_word(dates[0]), "total": len(markers), "markers": markers}


def workouts_block(uid: int, dates: list[str]) -> dict | None:
    rows = db.workouts_detailed(uid, dates)
    if not rows:
        return None
    done = [w for w in rows if w["status"] == "done"]
    block = {"month_done": len(done)}
    if done:
        w = done[0]
        block["last"] = {
            "when": when_word(w["date"]),
            "dur": w["duration_min"],
            "kcal": w["kcal_burned"],
            "src": SRC_NAME.get(w["kcal_source"] or "", "оценка"),
            "desc": w["description"] or "",
        }
    return block


def payload(uid: int, days: int = 30) -> dict:
    """Единый ответ для личной страницы клиента и карточки в кабинете тренера."""
    import web_dashboard as wd

    s = wd.build_summary(days, uid=uid)
    if not s.get("ok"):
        return {"ok": False, "reason": s.get("reason")}

    user = db.get_user(uid) or {}
    coach = db.coach_by_id(user["coach_id"]) if user.get("coach_id") else None
    dates_asc = [row["date"] for row in s["series"]]

    gadgets, oura_rows, whoop_rows = gadget_blocks(uid, dates_asc)
    weights = weight_by_date(uid, dates_asc)
    primary_is_oura = gadgets["primary"] == "oura"

    series = []
    for row in s["series"]:
        day = row["date"]
        o = oura_rows.get(day) or {}
        w = whoop_rows.get(day) or {}
        sleep = o.get("sleep_h") if primary_is_oura else w.get("sleep_h")
        ready = o.get("readiness") if primary_is_oura else w.get("recovery")
        series.append({
            "date": day,
            "label": WEEKDAY_RU[datetime.strptime(day, "%Y-%m-%d").weekday()],
            "kcal": row["kcal"],
            "water": row["water"],
            "chek": row["chek"],
            "workout": {"done": "done", "skipped": "skip"}.get(row["workout"], "none"),
            "sleep_h": sleep if sleep is not None else (w.get("sleep_h") or o.get("sleep_h")),
            "readiness": ready if ready is not None else (w.get("recovery") or o.get("readiness")),
            "weight": weights.get(day),
        })

    today, targets = s["today"], s["targets"]
    out = {
        "ok": True,
        "me": {"name": user.get("name") or "",
               "brand": (coach or {}).get("brand") or "",
               "coach": (coach or {}).get("name") or ""},
        "today": {"kcal": today["kcal"], "protein": today["protein"], "fat": today["fat"],
                  "carbs": today["carbs"], "water": today["water"]},
        "targets": {"kcal": targets["kcal"], "protein": targets["protein"],
                    "fat": targets["fat"], "carbs": targets["carbs"],
                    "water": targets["water"]},
        "stats": {"avg_chek": s["stats"]["avg_chek"]},
        "series": series,
        "gadgets": gadgets,
    }

    detail = wd.client_detail(uid, days)

    if detail.get("wellbeing"):
        wb = detail["wellbeing"]
        out["wellbeing"] = {"avg": wb["avg"],
                            "latest": {"note": (wb.get("latest") or {}).get("note") or ""}}

    if detail.get("supplements"):
        week = dates_asc[-7:]
        taken = db.supplement_taken_dates(uid, week)
        out["supplements"] = [
            {"name": x["name"], "timing": x["timing"] or "",
             "plan": x["plan_days_per_week"],
             "dots": [1 if day in set(taken.get(x["id"], [])) else 0 for day in week]}
            for x in detail["supplements"]["list"]
        ]

    rem = reminders_block(uid)
    if rem:
        out["reminders"] = rem

    labs = labs_block(uid)
    if labs:
        out["labs"] = labs

    workouts = workouts_block(uid, dates_asc)
    if workouts:
        out["workouts"] = workouts

    history = db.weight_history(uid, 400)
    if history:
        current = history[0]["kg"]
        older = [w for w in history if w["date"] <= dates_asc[0]]
        base = older[-1]["kg"] if older else history[-1]["kg"]
        out["weight"] = {"current": round(current, 1), "delta30": round(current - base, 1)}
    elif user.get("weight_kg"):
        out["weight"] = {"current": round(float(user["weight_kg"]), 1), "delta30": 0}

    return out


def flags(uid: int, days: int = 7) -> list[dict]:
    """Светофор в уровнях, которые понимает вёрстка: ok / warn / crit."""
    import web_dashboard as wd

    return [{"level": FLAG_LEVEL.get(f["level"], "ok"), "text": f["text"]}
            for f in wd.client_overview(uid, days)["flags"]]


def coach_client_payload(uid: int, days: int = 30) -> dict:
    """Карточка клиента в кабинете тренера: тот же контракт плюс тренерские поля."""
    out = payload(uid, days)
    if not out.get("ok"):
        return out
    user = db.get_user(uid) or {}
    out["name"] = user.get("name") or str(uid)
    out["meta"] = {"sex": user.get("sex"), "age": user.get("age"), "goal": user.get("goal")}
    out["flags"] = flags(uid)
    notes = db.trainer_notes_range(uid, [row["date"] for row in out["series"]])
    out["notes"] = [{"date": when_word(n["date"]), "text": n["text"]} for n in notes[:10]]
    return out


DOW_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def reminder_when(rem: dict) -> str:
    """Расписание напоминания человеческой строкой для кабинета."""
    days = "ежедневно"
    mask = rem.get("dow_mask")
    if mask is not None:
        picked = [DOW_SHORT[i] for i in range(7) if (int(mask) >> i) & 1]
        if picked and len(picked) < 7:
            days = "·".join(picked)
    if rem.get("time"):
        return f"{days} в {rem['time']}" if days == "ежедневно" else f"{days} в {rem['time']}"
    every = rem.get("every_min")
    if every:
        hours = every / 60
        step = f"каждые {int(hours)} ч" if hours >= 1 and float(hours).is_integer() \
            else f"каждые {every} мин"
        window = ""
        if rem.get("win_from") and rem.get("win_to"):
            window = f", {rem['win_from']}–{rem['win_to']}"
        return step + window
    return days


def reminders_block(uid: int) -> list[dict]:
    return [{"kind": r["kind"], "text": r["text"], "when": reminder_when(r)}
            for r in db.list_reminders(uid)]
