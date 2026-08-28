"""Локальный веб-дашборд: страница + JSON API поверх базы бота.

Слушает только 127.0.0.1 — доступен только с этого компьютера.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

import config
import db
import nutrition

DASHBOARD_FILE = Path(__file__).parent / "dashboard.html"
COACH_FILE = Path(__file__).parent / "coach.html"
CLIENT_FILE = Path(__file__).parent / "client.html"
PRIVACY_FILE = Path(__file__).parent / "privacy.html"


def _owner_uid() -> int | None:
    if config.ALLOWED_USER_IDS:
        return sorted(config.ALLOWED_USER_IDS)[0]
    owner = db.get_setting("owner_id")
    return int(owner) if owner else None


def build_summary(days: int = 7, uid: int | None = None) -> dict:
    """Собирает все данные дашборда за последние `days` дней."""
    uid = uid or _owner_uid()
    today = datetime.now().strftime("%Y-%m-%d")
    dates_desc = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                  for i in range(days)]
    dates_asc = list(reversed(dates_desc))

    if uid is None:
        return {"ok": False, "reason": "no_user", "days": days}

    user = db.get_user(uid) or {}
    water_target = (user.get("water_target_ml")
                    or nutrition.water_target_ml(user.get("weight_kg")))
    totals = db.totals_by_date(uid, dates_asc)
    water = db.water_by_date(uid, dates_asc)
    wi_days = db.habit_dates(uid, "workingin", dates_asc)
    workouts = db.workouts_by_date(uid, dates_asc)

    series = []
    for d in dates_asc:
        t = totals.get(d) or {}
        series.append({
            "date": d,
            "label": datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m"),
            "kcal": round(t.get("kcal") or 0),
            "protein": round(t.get("protein") or 0),
            "fat": round(t.get("fat") or 0),
            "carbs": round(t.get("carbs") or 0),
            "chek": round(t["chek"], 1) if t.get("chek") else None,
            "meals": t.get("n") or 0,
            "water": water.get(d, 0),
            "workout": workouts.get(d),
            "wi": d in wi_days,
        })

    today_row = next((s for s in series if s["date"] == today), None) or {
        "kcal": 0, "protein": 0, "fat": 0, "carbs": 0,
        "water": 0, "wi": False, "workout": None, "meals": 0, "chek": None,
    }

    cheks = [s["chek"] for s in series if s["chek"]]
    days_with_food = [s for s in series if s["meals"]]

    return {
        "ok": True,
        "generated_at": datetime.now().strftime("%H:%M"),
        "days": days,
        "name": user.get("name") or "",
        "has_profile": bool(user.get("kcal_target")),
        "targets": {
            "kcal": user.get("kcal_target"),
            "protein": user.get("protein_target"),
            "fat": user.get("fat_target"),
            "carbs": user.get("carb_target"),
            "water": water_target,
        },
        "today": today_row,
        "series": series,
        "stats": {
            "avg_kcal": round(sum(s["kcal"] for s in days_with_food) / len(days_with_food))
            if days_with_food else 0,
            "avg_chek": round(sum(cheks) / len(cheks), 1) if cheks else None,
            "water_ok_days": sum(1 for s in series if s["water"] >= water_target),
            "wi_days": len(wi_days),
            "wi_streak": db.habit_streak(uid, "workingin",
                                         [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                                          for i in range(60)]),
            "workouts_done": sum(1 for s in workouts.values() if s == "done"),
        },
        "meals": db.meals_for_dates(uid, dates_asc),
    }


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    """Владелец — по DASHBOARD_TOKEN; кабинет тренера (/coach*) — по ключу кабинета."""
    if request.path.startswith("/oura/") or request.path.startswith("/whoop/"):
        return await handler(request)  # OAuth-callback защищён параметром state
    if request.path == "/privacy":
        return await handler(request)  # публичная страница, ключ не нужен
    if request.path.startswith("/me"):
        supplied = request.query.get("key") or request.cookies.get("chek_me")
        me = db.user_by_cabinet_token(supplied or "")
        if me is None:
            return web.Response(
                status=401,
                text="Личная страничка открывается по персональной ссылке.\n"
                     "Попроси её у своего ассистента в чате — он пришлёт.",
                content_type="text/plain", charset="utf-8",
            )
        request["me"] = me
        resp = await handler(request)
        if request.query.get("key") == me["cabinet_token"]:
            resp.set_cookie("chek_me", me["cabinet_token"],
                            max_age=60 * 60 * 24 * 90, httponly=True, samesite="Lax")
        return resp

    if request.path.startswith("/coach"):
        supplied = request.query.get("key") or request.cookies.get("chek_coach")
        coach = db.coach_by_cabinet_token(supplied or "")
        if coach is None:
            return web.Response(
                status=401,
                text="Кабинет тренера открывается по персональной ссылке вида:\n"
                     "%s\n\n"
                     "Ключ выдаётся при подключении тренера (команда /newcoach у владельца)."
                     % config.public_url("/coach?key=КЛЮЧ_КАБИНЕТА"),
                content_type="text/plain",
                charset="utf-8",
            )
        request["coach"] = coach
        resp = await handler(request)
        if request.query.get("key") == coach["cabinet_token"]:
            resp.set_cookie("chek_coach", coach["cabinet_token"],
                            max_age=60 * 60 * 24 * 90, httponly=True, samesite="Lax")
        return resp

    token = config.DASHBOARD_TOKEN
    if token:
        supplied = request.query.get("key") or request.cookies.get("chek_key")
        if supplied != token:
            return web.Response(
                status=401,
                text="Доступ по ключу. Открой ссылку вида:\n"
                     "%s\n\n"
                     "Ключ печатается при деплое и лежит на сервере в .env "
                     "(строка DASHBOARD_TOKEN)." % config.public_url("/?key=ТВОЙ_КЛЮЧ"),
                content_type="text/plain",
                charset="utf-8",
            )
    resp = await handler(request)
    if token and request.query.get("key") == token:
        resp.set_cookie("chek_key", token, max_age=60 * 60 * 24 * 90,
                        httponly=True, samesite="Lax")
    return resp


async def _index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(DASHBOARD_FILE,
                            headers={"Cache-Control": "no-store"})


async def _api_summary(request: web.Request) -> web.Response:
    try:
        days = max(1, min(int(request.query.get("days", "7")), 90))
    except ValueError:
        days = 7
    return web.json_response(build_summary(days),
                             headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- кабинет тренера

def client_overview(uid: int, days: int = 7) -> dict:
    """Строка светофора по одному клиенту."""
    s = build_summary(days, uid=uid)
    user = db.get_user(uid) or {}
    series = s["series"]
    logged_days = [x for x in series if x["meals"] or x["water"] or x["wi"] or x["workout"]]
    last_activity = logged_days[-1]["date"] if logged_days else None
    days_silent = None
    if last_activity:
        days_silent = (datetime.now() - datetime.strptime(last_activity, "%Y-%m-%d")).days

    flags = []
    if last_activity is None:
        flags.append(("red", "ещё не начал"))
    elif days_silent >= 2:
        flags.append(("red", f"молчит {days_silent} дн."))
    if s["stats"]["avg_chek"] is not None and s["stats"]["avg_chek"] < 5:
        flags.append(("yellow", "качество еды низкое"))
    tgt = s["targets"]["kcal"]
    if tgt and s["stats"]["avg_kcal"] and s["stats"]["avg_kcal"] < 0.7 * tgt:
        flags.append(("yellow", "ест заметно ниже нормы"))
    if not flags:
        flags.append(("green", "всё в порядке"))

    return {
        "uid": uid,
        "name": user.get("name") or str(uid),
        "days_logged": len(logged_days),
        "days": days,
        "last_activity": last_activity,
        "kcal_today": s["today"]["kcal"],
        "kcal_target": tgt,
        "avg_chek": s["stats"]["avg_chek"],
        "water_today": s["today"]["water"],
        "water_target": s["targets"]["water"],
        "workouts_done": s["stats"]["workouts_done"],
        "flags": [{"level": lv, "text": tx} for lv, tx in flags],
    }


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


# Потолок окна и порог, после которого дневные точки схлопываем в недели.
MAX_DAYS = 3650
BUCKET_AFTER_DAYS = 35
# Сколько приёмов пищи отдаём в карточку. Обрезку показываем явно, не молча.
FOOD_LIMIT = 300


def resolve_days(uid: int, raw: str | None) -> int:
    """Сколько дней показывать. «all» — от первой активности клиента."""
    raw = (raw or "7").strip().lower()
    if raw in ("all", "всё", "все"):
        first = db.first_activity_date(uid)
        if not first:
            return 7          # данных нет вообще — показываем привычную неделю
        try:
            start = datetime.strptime(first, "%Y-%m-%d")
        except ValueError:
            return 7
        return max(1, min((datetime.now() - start).days + 1, MAX_DAYS))
    try:
        return max(1, min(int(raw), MAX_DAYS))
    except ValueError:
        return 7


def _bucket_weekly(series: list[dict], keys) -> list[dict]:
    """Схлопывает дневной ряд в средние по ISO-неделям.

    На окне в месяцы и годы дневные точки превращаются в шум — по неделям
    спарклайн снова читается. Плитки при этом считаются по дневным данным.
    """
    buckets: dict[tuple, list[dict]] = {}
    for row in series:
        try:
            dt = datetime.strptime(row["date"], "%Y-%m-%d")
        except (KeyError, TypeError, ValueError):
            continue
        year, week, _ = dt.isocalendar()
        buckets.setdefault((year, week), []).append(row)

    out = []
    for _, rows in sorted(buckets.items()):
        first = min(r["date"] for r in rows)
        item = {
            "date": first,
            "label": datetime.strptime(first, "%Y-%m-%d").strftime("%d.%m"),
            "days": len(rows),
        }
        for k in keys:
            item[k] = _avg([r.get(k) for r in rows])
        out.append(item)
    return out


_SERIES_KEYS = ("kcal", "protein", "fat", "carbs", "chek", "meals", "water")
# Числовые метрики кольца: по ним считаем средние и схлопываем ряды.
_WHOOP_KEYS = ("recovery", "hrv", "resting_hr", "spo2", "skin_temp", "sleep_h",
               "sleep_perf", "deep_h", "rem_h", "light_h", "awake_h", "breath_avg",
               "strain", "day_kcal", "avg_hr", "max_hr")

_OURA_KEYS = ("readiness", "sleep_score", "sleep_h", "hrv", "resting_hr",
              "temp_dev", "activity_score", "steps",
              "sleep_efficiency", "breath_avg", "deep_h", "rem_h", "light_h",
              "spo2_avg", "active_kcal", "total_kcal", "distance_m", "active_min",
              "stress_high_min", "cardio_age", "vo2_max")


def client_detail(uid: int, days: int = 7, labs_days: int | None = None) -> dict:
    """Полный срез клиента для кабинета: еда/вода/тренировки + самочувствие + БАДы + анализы + Oura."""
    s = build_summary(days, uid=uid)
    today = datetime.now().strftime("%Y-%m-%d")
    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    user = db.get_user(uid) or {}

    # самочувствие
    wr = db.wellbeing_range(uid, dates)
    wb_days = [wr[d] for d in dates if d in wr]
    wellbeing = None
    if wb_days:
        latest = wr.get(max(wr))
        wellbeing = {
            "avg": {k: _avg([w.get(k) for w in wb_days])
                    for k in ("energy", "mood", "stress", "libido")},
            "latest": latest,
            "days": len(wb_days),
        }

    # БАДы: план приёма против факта за окно
    supps = db.list_supplements(uid)
    taken_today = db.taken_supplements(uid, today)
    supplements = None
    if supps:
        by_id = db.supplement_taken_dates(uid, dates)
        rows = []
        for x in supps:
            plan = x["plan_days_per_week"] or 7
            tdates = by_id.get(x["id"], [])
            rows.append({
                "id": x["id"],
                "name": x["name"],
                "timing": x["timing"],
                "plan_days_per_week": plan,
                "planned": int(round(plan / 7 * days)),
                "taken": len(tdates),
                "taken_dates": tdates,
                "today_taken": x["id"] in taken_today,
            })
        supplements = {
            "list": rows,
            "taken": len(taken_today), "total": len(supps),
            "window_days": days,
            "planned_total": sum(r["planned"] for r in rows),
            "taken_total": sum(r["taken"] for r in rows),
        }

    # анализы: только бланки, попавшие в окно; тренд — к предыдущему значению
    # (оно может лежать и за пределами окна, так и задумано)
    labs = None
    # Для брифа окно анализов расширяем (labs_days): бланки сдают редко,
    # и на недельном окне ИИ остался бы без них.
    labs_start = (datetime.now() - timedelta(days=(labs_days or days) - 1)).strftime("%Y-%m-%d")
    window_start = min(dates[-1], labs_start)
    all_ldates = db.lab_dates(uid)
    ldates = [d for d in all_ldates if d >= window_start]
    in_window = bool(ldates)
    if not ldates and all_ldates:
        # Анализы сдают раз в несколько месяцев. Прятать последний бланк
        # только потому, что он старше выбранного периода, — значит скрывать
        # от тренера реальные данные. Показываем и помечаем, что он старше.
        ldates = all_ldates[:1]
    if ldates:
        visible_from = window_start if in_window else ldates[0]
        rows = []
        for m in db.latest_markers(uid):
            if m["date"] < visible_from:
                continue
            prev = None
            hist = db.marker_history(uid, m["name"])
            prevvals = [h for h in hist if h["date"] < ldates[0] and h["value"] is not None]
            if prevvals:
                prev = prevvals[-1]["value"]
            rows.append({
                "name": m["name"],
                "value": m["value"], "value_text": m["value_text"], "unit": m["unit"],
                "ref_low": m["ref_low"], "ref_high": m["ref_high"], "flag": m["flag"],
                "prev": prev,
            })
        if rows:
            rows.sort(key=lambda r: 0 if r["flag"] in ("низко", "высоко") else 1)
            labs = {"dates": ldates, "last_date": ldates[0], "markers": rows,
                    "in_window": in_window,
                    "abnormal": sum(1 for r in rows if r["flag"] in ("низко", "высоко"))}

    # Oura за то же окно: ряд по дням + средние за период для плиток
    oura = None
    if hasattr(db, "oura_range"):
        od = db.oura_range(uid, dates)
        if od:
            oura = {
                "series": [{"date": d, **(od.get(d) or {})} for d in reversed(dates)],
                "latest": od.get(max(od)),
                "avg": {k: _avg([v.get(k) for v in od.values()]) for k in _OURA_KEYS},
                "workouts": db.oura_workouts_range(uid, dates),
                "connected": True,
            }

    # Что именно ел клиент: по дням, свежие сверху, с составом приёма.
    rows = db.meals_detailed(uid, dates, FOOD_LIMIT)
    total_meals = db.meals_count(uid, dates)
    by_day: dict[str, list] = {}
    for m in rows:
        raw = {}
        if m.get("raw_json"):
            try:
                raw = json.loads(m["raw_json"])
            except (TypeError, ValueError):
                raw = {}
        items = raw.get("items")
        by_day.setdefault(m["date"], []).append({
            "time": m["time"], "dish": m["dish"], "source": m["source"],
            "grams": m["grams"], "kcal": m["kcal"], "protein": m["protein"],
            "fat": m["fat"], "carbs": m["carbs"],
            "chek_score": m["chek_score"], "verdict": m["chek_verdict"] or "",
            "tip": raw.get("chek_tip") or "",
            "assumptions": raw.get("assumptions") or "",
            "confidence": raw.get("confidence") or "",
            "items": [{"name": i.get("name"), "grams": i.get("grams"), "kcal": i.get("kcal")}
                      for i in (items if isinstance(items, list) else [])
                      if isinstance(i, dict) and i.get("name")],
        })
    food_days = []
    for d in sorted(by_day, reverse=True):
        ms = sorted(by_day[d], key=lambda x: x["time"] or "")
        food_days.append({
            "date": d,
            "label": datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m"),
            "kcal": round(sum(x["kcal"] or 0 for x in ms)),
            "chek": _avg([x["chek_score"] for x in ms]),
            "meals": ms,
        })
    food = {"days": food_days, "shown": len(rows), "total": total_meals,
            "truncated": total_meals > len(rows), "limit": FOOD_LIMIT}
    # Полный плоский список приёмов кабинету не нужен — он дублировал бы food
    # и на окне в год весил бы заметно больше самой карточки.
    s.pop("meals", None)

    # Тренировки: тренеру важен не только факт, но и что именно клиент делал.
    wlog = db.workouts_detailed(uid, dates)
    workouts = None
    if wlog:
        workouts = {
            "list": [{"date": w["date"], "time": w["time"], "status": w["status"],
                      "duration_min": w["duration_min"],
                      "description": w["description"] or "",
                      "kcal_burned": w["kcal_burned"],
                      "kcal_source": w["kcal_source"] or ""} for w in wlog],
            "done": sum(1 for w in wlog if w["status"] == "done"),
            "skipped": sum(1 for w in wlog if w["status"] != "done"),
            "minutes": sum(w["duration_min"] or 0 for w in wlog if w["status"] == "done"),
            # расход держим отдельно от съеденного: смешивать intake и burn нельзя
            "kcal_burned": sum(w["kcal_burned"] or 0 for w in wlog if w["status"] == "done"),
        }

    # WHOOP: тот же принцип, что и Oura — средние за окно плюс тренировки
    whoop = None
    wd = db.whoop_range(uid, dates)
    if wd:
        whoop = {
            "series": [{"date": d, **(wd.get(d) or {})} for d in reversed(dates)],
            "latest": wd.get(max(wd)),
            "avg": {k: _avg([v.get(k) for v in wd.values()]) for k in _WHOOP_KEYS},
            "workouts": db.whoop_workouts_range(uid, dates),
            "connected": True,
        }

    # Последняя запись — по дневному ряду, до схлопывания в недели.
    logged = [x for x in s["series"] if x["meals"] or x["water"] or x["wi"] or x["workout"]]

    bucket = "week" if days > BUCKET_AFTER_DAYS else "day"
    if bucket == "week":
        s["series"] = _bucket_weekly(s["series"], _SERIES_KEYS)
        if oura:
            oura["series"] = _bucket_weekly(oura["series"], _OURA_KEYS)
        if whoop:
            whoop["series"] = _bucket_weekly(whoop["series"], _WHOOP_KEYS)

    s.update({
        "sex": user.get("sex"), "age": user.get("age"), "goal": user.get("goal"),
        "wellbeing": wellbeing, "supplements": supplements, "labs": labs, "oura": oura,
        "whoop": whoop, "food": food, "workouts": workouts,
        "trainer_notes": db.trainer_notes_range(uid, dates), "bucket": bucket,
        "last_activity": logged[-1]["date"] if logged else None,
        "last_activity_label": logged[-1]["label"] if logged else None,
    })
    return s


def _client_uid_or_none(request: web.Request) -> int | None:
    coach = request["coach"]
    try:
        uid = int(request.query.get("uid", ""))
    except ValueError:
        return None
    allowed = {u["user_id"] for u in db.clients_of_coach(coach["id"])}
    return uid if uid in allowed else None


def week_data_text(uid: int) -> str:
    """Компактный текст недели клиента для AI-брифа."""
    s = build_summary(7, uid=uid)
    user = db.get_user(uid) or {}
    lines = []
    tg = s["targets"]
    if tg["kcal"]:
        lines.append(f"Цели: {tg['kcal']} ккал (Б{tg['protein']}/Ж{tg['fat']}/У{tg['carbs']}), "
                     f"вода {tg['water']} мл.")
    else:
        lines.append(f"Цели не заданы. Норма воды {tg['water']} мл.")
    if user.get("goal"):
        lines.append(f"Цель клиента: {user['goal']}.")
    # Что именно клиент делал на тренировке — тренеру это ценнее галочки
    wmap: dict[str, dict] = {}
    for w in db.workouts_detailed(uid, [x["date"] for x in s["series"]]):
        if w["status"] == "done" and w["date"] not in wmap:
            wmap[w["date"]] = w
    meals_by_date: dict[str, list[str]] = {}
    for m in s["meals"]:
        meals_by_date.setdefault(m["date"], []).append(m["dish"])
    for d in s["series"]:
        dishes = ", ".join(meals_by_date.get(d["date"], [])[:4]) or "—"
        parts = [f"{d['label']}: {d['kcal']} ккал (Б{d['protein']}/Ж{d['fat']}/У{d['carbs']})"]
        parts.append(f"Чек {d['chek']}" if d["chek"] else "Чек —")
        parts.append(f"вода {d['water']}")
        if d["workout"] == "done":
            w = wmap.get(d["date"])
            extra = []
            if w and w["duration_min"]:
                extra.append(f"{w['duration_min']} мин")
            if w and w["kcal_burned"]:
                src = "оценка" if w["kcal_source"] == "estimate" else "Oura"
                extra.append(f"расход ~{w['kcal_burned']} ккал ({src})")
            if w and w["description"]:
                extra.append(w["description"][:120])
            parts.append("тренировка ✓" + (f" ({', '.join(extra)})" if extra else ""))
        if d["wi"]:
            parts.append("working in ✓")
        lines.append("; ".join(parts) + f". Ел: {dishes}")
    lines.append(f"Дней с записями: {sum(1 for x in s['series'] if x['meals'])}/7.")

    detail = client_detail(uid, 7, labs_days=MAX_DAYS)
    wb = detail.get("wellbeing")
    if wb:
        a = wb["avg"]
        bits = [f"{k} {a[k]}" for k in ("energy", "mood", "stress", "libido") if a.get(k) is not None]
        lines.append("Самочувствие (ср. за неделю, 1–10): " + ", ".join(bits) + ".")
        if wb["latest"].get("note"):
            lines.append(f"Заметка клиента: «{wb['latest']['note']}».")
    sup = detail.get("supplements")
    if sup:
        names = ", ".join(f"{x['name']}" + (f" ({x['timing']})" if x['timing'] else "")
                          for x in sup["list"])
        lines.append(f"БАДы: {names}. Сегодня принято {sup['taken']}/{sup['total']}.")
    wh = detail.get("whoop")
    if wh and wh.get("avg"):
        a = wh["avg"]
        bits = [f"{label} {a[key]}" for key, label in
                (("recovery", "восстановление"), ("strain", "нагрузка (strain)"),
                 ("sleep_h", "сон, ч"), ("hrv", "HRV"), ("resting_hr", "пульс покоя"))
                if a.get(key) is not None]
        if bits:
            lines.append("WHOOP за неделю (в среднем): " + ", ".join(bits) + ".")
    notes = detail.get("trainer_notes") or []
    if notes:
        lines.append("Ассистент подсветил: "
                     + "; ".join(f"{n['date']} — {n['text'][:160]}" for n in notes[:5]))
    labs = detail.get("labs")
    if labs:
        abn = [m for m in labs["markers"] if m["flag"] in ("низко", "высоко")]
        if abn:
            mk = "; ".join(f"{m['name']} {m['value_text'] or m['value']} {m['unit']} ({m['flag']})"
                           for m in abn[:8])
            lines.append(f"Анализы (последний бланк {labs['last_date']}), вне нормы: {mk}.")
        else:
            lines.append(f"Анализы от {labs['last_date']}: все в норме.")
    return "\n".join(lines)


async def _privacy(request: web.Request) -> web.StreamResponse:
    """Политика конфиденциальности — открыта всем, без ключа."""
    return web.FileResponse(PRIVACY_FILE, headers={"Cache-Control": "public, max-age=3600"})


async def _me_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(CLIENT_FILE, headers={"Cache-Control": "no-store"})


async def _me_api_summary(request: web.Request) -> web.Response:
    """Тот же срез, что видит тренер, но строго по владельцу токена."""
    me = request["me"]
    days = resolve_days(me["user_id"], request.query.get("days"))
    data = client_detail(me["user_id"], days)
    coach = db.coach_by_id(me["coach_id"]) if me.get("coach_id") else None
    data["brand"] = (coach or {}).get("brand") or ""
    data["coach_name"] = (coach or {}).get("name") or ""
    return web.json_response(data)


async def _me_api_brief(request: web.Request) -> web.Response:
    """Разбор недели для самого клиента: без черновиков сообщений."""
    import analyzer

    me = request["me"]
    coach = db.coach_by_id(me["coach_id"]) if me.get("coach_id") else None
    try:
        text = await analyzer.generate_client_brief(
            me.get("name") or "клиент", week_data_text(me["user_id"]), coach)
    except analyzer.DemoModeError:
        return web.json_response({"error": "ИИ-ключ не настроен на сервере"}, status=400)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": f"не получилось: {str(e)[:200]}"}, status=502)
    return web.json_response({"brief": text})


async def _coach_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(COACH_FILE, headers={"Cache-Control": "no-store"})


async def _coach_api_me(request: web.Request) -> web.Response:
    coach = request["coach"]
    return web.json_response({
        "name": coach.get("name"), "brand": coach.get("brand"),
        "bot_username": coach.get("bot_username") or "",
    })


async def _coach_api_clients(request: web.Request) -> web.Response:
    coach = request["coach"]
    rows = [client_overview(u["user_id"]) for u in db.clients_of_coach(coach["id"])]
    order = {"red": 0, "yellow": 1, "green": 2}
    rows.sort(key=lambda r: order.get(r["flags"][0]["level"], 3))
    return web.json_response({"clients": rows, "generated_at": datetime.now().strftime("%H:%M")})


async def _coach_api_client(request: web.Request) -> web.Response:
    uid = _client_uid_or_none(request)
    if uid is None:
        return web.json_response({"error": "клиент не найден"}, status=404)
    days = resolve_days(uid, request.query.get("days"))
    return web.json_response(client_detail(uid, days))


async def _coach_api_brief(request: web.Request) -> web.Response:
    import analyzer
    uid = _client_uid_or_none(request)
    if uid is None:
        return web.json_response({"error": "клиент не найден"}, status=404)
    coach = request["coach"]
    user = db.get_user(uid) or {}
    try:
        text = await analyzer.generate_brief(
            user.get("name") or "клиент", week_data_text(uid), coach
        )
    except analyzer.DemoModeError:
        return web.json_response(
            {"error": "ИИ-ключ не настроен на сервере (демо-режим)"}, status=400)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": f"не получилось: {str(e)[:200]}"}, status=502)
    return web.json_response({"brief": text})


def _oura_page(title: str, body: str) -> web.Response:
    html = (f"<!doctype html><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title>"
            f"<div style='font-family:system-ui,sans-serif;max-width:460px;margin:60px auto;"
            f"padding:0 20px;text-align:center;color:#22271f'>"
            f"<div style='font-size:3rem'>💍</div><h2>{title}</h2><p>{body}</p></div>")
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def _oura_callback(request: web.Request) -> web.Response:
    import oura
    code = request.query.get("code")
    state = request.query.get("state") or ""
    if request.query.get("error"):
        return _oura_page("Подключение отменено",
                          "Доступ к кольцу не выдан. Можно повторить командой /oura в боте.")
    uid_s = db.get_setting(f"ourastate:{state}") if state else None
    if not code or not uid_s:
        return _oura_page("Ссылка устарела",
                          "Открой /oura в боте заново и перейди по свежей ссылке.")
    uid = int(uid_s)
    try:
        await oura.complete_auth(uid, code)
        n = await oura.fetch_and_store(uid)
    except Exception as e:  # noqa: BLE001
        import html as _html
        return _oura_page("Не получилось",
                          "Ошибка при подключении Oura: " + _html.escape(str(e)[:160]))
    db.set_setting(f"ourastate:{state}", "")
    return _oura_page("Кольцо подключено! ✅",
                      f"Загрузил данные за последние дни ({n} дн.). Возвращайся в Telegram — "
                      "теперь сон и готовность будут в сводках и брифах.")


async def _whoop_callback(request: web.Request) -> web.Response:
    import whoop

    code = request.query.get("code")
    state = request.query.get("state") or ""
    if request.query.get("error"):
        return _oura_page("Подключение отменено",
                          "Доступ к браслету не выдан. Скажи ассистенту, если захочешь "
                          "попробовать снова.")
    uid_s = db.get_setting(f"whoopstate:{state}") if state else None
    if not code or not uid_s:
        return _oura_page("Ссылка устарела",
                          "Попроси у ассистента свежую ссылку и перейди по ней.")
    uid = int(uid_s)
    try:
        await whoop.complete_auth(uid, code)
        n = await whoop.fetch_and_store(uid)
    except Exception as e:  # noqa: BLE001
        import html as _html
        return _oura_page("Не получилось",
                          "Ошибка при подключении WHOOP: " + _html.escape(str(e)[:160]))
    db.set_setting(f"whoopstate:{state}", "")
    return _oura_page("Браслет подключён! ✅",
                      f"Загрузил данные за последние дни ({n} дн.). Возвращайся в Telegram — "
                      "теперь восстановление, сон и нагрузка будут в сводках.")


async def start_dashboard(port: int, host: str | None = None) -> web.AppRunner:
    if host is None:
        # С ключом — доступ отовсюду (сервер), без ключа — только этот компьютер.
        host = "0.0.0.0" if config.DASHBOARD_TOKEN else "127.0.0.1"
    app = web.Application(middlewares=[_auth_middleware])
    app.router.add_get("/", _index)
    app.router.add_get("/api/summary", _api_summary)
    app.router.add_get("/privacy", _privacy)
    app.router.add_get("/me", _me_index)
    app.router.add_get("/me/api/summary", _me_api_summary)
    app.router.add_post("/me/api/brief", _me_api_brief)
    app.router.add_get("/coach", _coach_index)
    app.router.add_get("/coach/api/me", _coach_api_me)
    app.router.add_get("/coach/api/clients", _coach_api_clients)
    app.router.add_get("/coach/api/client", _coach_api_client)
    app.router.add_post("/coach/api/brief", _coach_api_brief)
    app.router.add_get("/oura/callback", _oura_callback)
    app.router.add_get("/whoop/callback", _whoop_callback)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
