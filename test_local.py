"""Локальные тесты «Чека»: быстро, без сети и без обращений к ИИ.

Запуск:  python test_local.py

Всё, что ходит наружу (Claude, OpenRouter, Oura), замокано.
База — временная, боевой food_diary.db не трогается.
"""
import asyncio
import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
from datetime import datetime, timedelta

# Временная база — ДО импорта config/db: db.py читает DB_PATH при импорте.
_TMPDIR = tempfile.mkdtemp(prefix="chek_tests_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
# Фиксируем окружение, чтобы личный .env не влиял на результат тестов.
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp          # noqa: E402
import analyzer         # noqa: E402
import config           # noqa: E402
import db               # noqa: E402
import nutrition        # noqa: E402
import web_dashboard    # noqa: E402

# ---------------------------------------------------------------- каркас

_checks = 0
_fails: list[str] = []


def ok(cond, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _fails.append(label)
        print(f"    x {label}")


def eq(got, exp, label: str) -> None:
    ok(got == exp, f"{label}: получено {got!r}, ожидалось {exp!r}")


def near(got, exp, label: str, tol: float = 0.01) -> None:
    ok(got is not None and abs(got - exp) <= tol,
       f"{label}: получено {got!r}, ожидалось ~{exp!r}")


def raises(exc, fn, label: str) -> None:
    try:
        fn()
    except exc:
        ok(True, label)
        return
    except Exception as e:  # noqa: BLE001
        ok(False, f"{label}: другое исключение {type(e).__name__}")
        return
    ok(False, f"{label}: исключение не возникло")


TODAY = datetime.now().strftime("%Y-%m-%d")


def day(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


UID = 1001          # владелец
COACH_UID = 2002    # тренер (человек)
CLIENT_UID = 3003   # клиент тренера

# ---------------------------------------------------------------- db: пользователи


def test_users():
    db.ensure_user(UID, "Владелец")
    u = db.get_user(UID)
    ok(u is not None, "пользователь создан")
    eq(u["name"], "Владелец", "имя пользователя")
    eq(u["coach_id"], None, "личный пользователь без тренера")

    db.update_user(UID, sex="Мужчина", age=30, height_cm=180, weight_kg=80,
                   kcal_target=2760, protein_target=130, fat_target=90, carb_target=355)
    u = db.get_user(UID)
    eq(u["weight_kg"], 80, "вес сохранён")
    eq(u["kcal_target"], 2760, "цель по калориям сохранена")

    # Повторный ensure_user обновляет имя, но НЕ перепривязывает к тренеру.
    db.ensure_user(UID, "Владелец 2", coach_id=777)
    u = db.get_user(UID)
    eq(u["name"], "Владелец 2", "имя обновляется")
    eq(u["coach_id"], None, "существующий пользователь не перепривязывается к тренеру")

    ok(UID in db.all_user_ids(), "пользователь в общем списке")

    db.set_setting("проба", "значение")
    eq(db.get_setting("проба"), "значение", "настройка читается")
    db.set_setting("проба", "новое")
    eq(db.get_setting("проба"), "новое", "настройка перезаписывается")
    eq(db.get_setting("нет-такой"), None, "неизвестная настройка — None")


# ---------------------------------------------------------------- db: еда


def test_meals():
    db.add_meal(UID, TODAY, "09:00", "photo", "Овсянка с ягодами",
                300, 400, 12, 10, 60, 9, "цельная еда")
    db.add_meal(UID, TODAY, "14:00", "text", "Гречка с курицей",
                400, 600, 45, 15, 70, 8, "хорошо")
    db.add_meal(UID, day(1), "13:00", "text", "Паста", 350, 700, 20, 20, 100, 4, "так себе")

    meals = db.meals_for_date(UID, TODAY)
    eq(len(meals), 2, "приёмов пищи за сегодня")
    eq(meals[0]["dish"], "Овсянка с ягодами", "порядок приёмов пищи")

    totals = db.totals_by_date(UID, [TODAY, day(1)])
    eq(totals[TODAY]["n"], 2, "число приёмов за сегодня")
    near(totals[TODAY]["kcal"], 1000, "сумма калорий за сегодня")
    near(totals[TODAY]["protein"], 57, "сумма белка за сегодня")
    near(totals[TODAY]["chek"], 8.5, "средний балл Чека за сегодня")
    near(totals[day(1)]["kcal"], 700, "сумма калорий за вчера")

    t = nutrition.day_totals(meals)
    eq(t["n"], 2, "day_totals: число приёмов")
    near(t["kcal"], 1000, "day_totals: калории")
    near(t["chek"], 8.5, "day_totals: средний Чек")
    eq(nutrition.day_totals([])["chek"], None, "day_totals: пустой день — Чек None")

    eq(len(db.meals_for_dates(UID, [TODAY, day(1)])), 3, "приёмы за две даты")
    eq(db.totals_by_date(UID, []), {}, "totals_by_date: пустой список дат")

    dropped = db.delete_last_meal(UID, TODAY)
    eq(dropped["dish"], "Гречка с курицей", "удалён последний приём")
    eq(len(db.meals_for_date(UID, TODAY)), 1, "после удаления остался один")
    # возвращаем, чтобы дальше данные были осмысленные
    db.add_meal(UID, TODAY, "14:00", "text", "Гречка с курицей",
                400, 600, 45, 15, 70, 8, "хорошо")
    eq(db.delete_last_meal(UID, day(5)), None, "удаление из пустого дня — None")


# ---------------------------------------------------------------- db: вода


def test_water():
    db.add_water(UID, TODAY, "09:00", 300)
    db.add_water(UID, TODAY, "12:00", 500)
    db.add_water(UID, day(1), "10:00", 700)

    eq(db.water_total(UID, TODAY), 800, "вода за сегодня")
    eq(db.water_total(UID, day(3)), 0, "вода за день без записей")

    by_date = db.water_by_date(UID, [TODAY, day(1)])
    eq(by_date[TODAY], 800, "вода по датам: сегодня")
    eq(by_date[day(1)], 700, "вода по датам: вчера")
    eq(db.water_by_date(UID, []), {}, "water_by_date: пустой список дат")

    db.reset_water(UID, day(1))
    eq(db.water_total(UID, day(1)), 0, "сброс воды за день")


# ---------------------------------------------------------------- db: БАДы


def test_supplements():
    sid = db.add_supplement(UID, "Витамин D", "утро")
    sid2 = db.add_supplement(UID, "Магний", "вечер")
    ok(isinstance(sid, int) and sid > 0, "БАД добавлен, вернулся id")

    lst = db.list_supplements(UID)
    eq(len(lst), 2, "активных БАДов")
    eq(lst[0]["name"], "Витамин D", "имя БАДа")
    eq(db.get_supplement(sid)["timing"], "утро", "время приёма БАДа")

    eq(db.toggle_supplement_taken(UID, TODAY, sid), True, "отметка приёма БАДа")
    ok(sid in db.taken_supplements(UID, TODAY), "БАД в списке принятых")
    eq(db.toggle_supplement_taken(UID, TODAY, sid), False, "снятие отметки приёма")
    ok(sid not in db.taken_supplements(UID, TODAY), "БАД убран из принятых")

    db.toggle_supplement_taken(UID, TODAY, sid)  # оставляем принятым
    db.deactivate_supplement(UID, sid2)
    eq(len(db.list_supplements(UID)), 1, "после деактивации остался один активный")
    eq(len(db.list_supplements(UID, only_active=False)), 2,
       "неактивные видны при only_active=False")


# ---------------------------------------------------------------- db: самочувствие


def test_wellbeing():
    db.set_wellbeing(UID, TODAY, energy=4, mood=5, stress=2, libido=3, note="бодрый день")
    w = db.get_wellbeing(UID, TODAY)
    eq(w["energy"], 4, "энергия сохранена")
    eq(w["note"], "бодрый день", "заметка сохранена")

    db.set_wellbeing(UID, TODAY, energy=2)
    w = db.get_wellbeing(UID, TODAY)
    eq(w["energy"], 2, "энергия обновлена")
    eq(w["mood"], 5, "остальные поля не затёрты")

    db.set_wellbeing(UID, TODAY, чужое_поле=1)
    ok("чужое_поле" not in (db.get_wellbeing(UID, TODAY) or {}), "неизвестное поле игнорируется")

    db.set_wellbeing(UID, day(1), energy=5, mood=4)
    rng = db.wellbeing_range(UID, [TODAY, day(1)])
    eq(len(rng), 2, "самочувствие за две даты")
    eq(db.get_wellbeing(UID, day(9)), None, "самочувствие за день без записи — None")


# ---------------------------------------------------------------- db: анализы


def test_labs():
    old = [
        {"name": "Ферритин", "value": 30.0, "unit": "нг/мл",
         "ref_low": 30, "ref_high": 400, "flag": "норма"},
        {"name": "Витамин D", "value": 18.0, "unit": "нг/мл",
         "ref_low": 30, "ref_high": 100, "flag": "низко"},
    ]
    new = [
        {"name": "Ферритин", "value": 55.0, "unit": "нг/мл",
         "ref_low": 30, "ref_high": 400, "flag": "норма"},
        {"name": "Витамин D", "value": 42.0, "unit": "нг/мл",
         "ref_low": 30, "ref_high": 100, "flag": "норма"},
        {"name": "ТТГ", "value": None, "value_text": "не определялся", "unit": "",
         "ref_low": None, "ref_high": None, "flag": "норма"},
    ]
    rid = db.add_lab_result(UID, day(60), "Биохимия", old)
    ok(isinstance(rid, int) and rid > 0, "бланк анализов сохранён")
    db.add_lab_result(UID, TODAY, "Биохимия", new)

    dates = db.lab_dates(UID)
    eq(dates[0], TODAY, "даты анализов — свежие первыми")
    eq(len(dates), 2, "две даты анализов")

    markers = db.markers_for_date(UID, TODAY)
    eq(len(markers), 3, "маркеров в последнем бланке")

    hist = db.marker_history(UID, "Витамин D")
    eq(len(hist), 2, "история маркера — две точки")
    eq(hist[0]["date"], day(60), "история отсортирована по возрастанию даты")
    near(hist[0]["value"], 18.0, "старое значение маркера")
    near(hist[1]["value"], 42.0, "новое значение маркера")

    latest = {m["name"]: m for m in db.latest_markers(UID)}
    eq(len(latest), 3, "последних маркеров по именам")
    near(latest["Витамин D"]["value"], 42.0, "последнее значение витамина D")
    eq(latest["ТТГ"]["value_text"], "не определялся", "текстовое значение маркера")


# ---------------------------------------------------------------- db: тренеры и клиенты


def test_coaches():
    coach = db.add_coach("123456789:AA-test-token", "Иван Тренеров", "Сила и Форма", "cab-key-1")
    ok(coach["id"] > 0, "тренер добавлен")
    eq(coach["bot_id"], 123456789, "bot_id извлечён из токена")
    eq(coach["brand"], "Сила и Форма", "бренд тренера")

    cid = coach["id"]
    eq(db.coach_by_cabinet_token("cab-key-1")["id"], cid, "тренер по ключу кабинета")
    eq(db.coach_by_cabinet_token("нет-такого"), None, "неизвестный ключ кабинета — None")
    eq(db.coach_by_cabinet_token(""), None, "пустой ключ кабинета — None")
    eq(db.coach_by_id(cid)["name"], "Иван Тренеров", "тренер по id")
    ok(any(c["id"] == cid for c in db.list_coaches()), "тренер в общем списке")

    db.set_coach_owner(cid, COACH_UID)
    eq(db.coach_by_id(cid)["coach_user_id"], COACH_UID, "владелец бота тренера привязан")
    db.update_coach(cid, brand="Сила и Форма PRO")
    eq(db.coach_by_id(cid)["brand"], "Сила и Форма PRO", "бренд обновлён")

    # Клиент появляется в кабинете только после согласия.
    db.ensure_user(CLIENT_UID, "Клиент Клиентов", coach_id=cid)
    eq(db.get_user(CLIENT_UID)["coach_id"], cid, "клиент привязан к тренеру")
    eq(len(db.clients_of_coach(cid)), 0, "без согласия клиент в кабинете не виден")

    db.update_user(CLIENT_UID, consent=1)
    clients = db.clients_of_coach(cid)
    eq(len(clients), 1, "после согласия клиент виден в кабинете")
    eq(clients[0]["user_id"], CLIENT_UID, "тот самый клиент")
    eq(len(db.clients_of_coach(99999)), 0, "у чужого тренера клиентов нет")
    return cid


# ---------------------------------------------------------------- db: Oura


def test_oura_db():
    db.save_oura_tokens(UID, "access-1", "refresh-1", 1800000000.0)
    tok = db.get_oura_tokens(UID)
    eq(tok["access_token"], "access-1", "access-токен сохранён")
    eq(tok["refresh_token"], "refresh-1", "refresh-токен сохранён")
    eq(db.oura_connected(UID), True, "кольцо подключено")
    ok(UID in db.oura_users(), "пользователь в списке подключивших кольцо")

    db.save_oura_tokens(UID, "access-2", "refresh-2", 1900000000.0)
    eq(db.get_oura_tokens(UID)["access_token"], "access-2", "токены перезаписываются")

    db.upsert_oura_daily(UID, TODAY, readiness=82, sleep_score=77, sleep_h=7.4,
                         hrv=55, resting_hr=52)
    db.upsert_oura_daily(UID, day(1), readiness=70, sleep_h=6.1)
    db.upsert_oura_daily(UID, TODAY, readiness=88, unknown_field=1)

    rng = db.oura_range(UID, [TODAY, day(1)])
    eq(len(rng), 2, "Oura: два дня в выборке")
    eq(rng[TODAY]["readiness"], 88, "Oura: readiness обновился")
    near(rng[TODAY]["sleep_h"], 7.4, "Oura: sleep_h не затёрт при частичном обновлении")
    ok("unknown_field" not in rng[TODAY], "Oura: неизвестное поле не сохраняется")
    ok("user_id" not in rng[TODAY], "Oura: user_id не торчит наружу")
    eq(db.oura_range(UID, []), {}, "Oura: пустой список дат")

    latest = db.oura_latest(UID)
    eq(latest["date"], TODAY, "Oura: последний день — сегодня")
    eq(db.oura_latest(999999), None, "Oura: у чужого пользователя данных нет")

    db.delete_oura_tokens(UID)
    eq(db.oura_connected(UID), False, "после отключения кольцо не подключено")
    # возвращаем для остальных тестов
    db.save_oura_tokens(UID, "access-2", "refresh-2", 1900000000.0)


# ---------------------------------------------------------------- nutrition


def test_nutrition():
    t = nutrition.calc_targets("Мужчина", 30, 180, 80,
                               "Средняя (3–5 тренировок в неделю)", "Поддерживать")
    eq(t["bmr"], 1780, "BMR по Миффлину — Сан-Жеору")
    eq(t["tdee"], 2759, "TDEE при средней активности")
    eq(t["kcal_target"], 2760, "калории при цели «Поддерживать»")
    eq(t["protein_target"], 130, "белок 1.6 г/кг при поддержании")

    loss = nutrition.calc_targets("Мужчина", 30, 180, 80,
                                  "Средняя (3–5 тренировок в неделю)", "Похудеть")
    gain = nutrition.calc_targets("Мужчина", 30, 180, 80,
                                  "Средняя (3–5 тренировок в неделю)", "Набрать")
    ok(loss["kcal_target"] < t["kcal_target"] < gain["kcal_target"],
       "похудение < поддержание < набор")
    ok(loss["protein_target"] > t["protein_target"], "на дефиците белка больше (1.8 г/кг)")

    fem = nutrition.calc_targets("Женщина", 30, 180, 80,
                                 "Средняя (3–5 тренировок в неделю)", "Поддерживать")
    ok(fem["bmr"] < t["bmr"], "у женщины BMR ниже при прочих равных")

    unknown = nutrition.calc_targets("Мужчина", 30, 180, 80, "Неизвестная", "Непонятная")
    eq(unknown["tdee"], int(1780 * 1.375), "неизвестная активность — коэффициент по умолчанию")

    eq(nutrition.water_target_ml(80), 2650, "норма воды при 80 кг")
    eq(nutrition.water_target_ml(60), 2000, "норма воды при 60 кг")
    eq(nutrition.water_target_ml(None), 2000, "норма воды без веса")
    eq(nutrition.water_target_ml(0), 2000, "норма воды при нулевом весе")

    ok("образцовый" in nutrition.chek_day_verdict(9), "вердикт Чека: отличный день")
    ok(isinstance(nutrition.chek_day_verdict(2), str), "вердикт Чека: плохой день")


# ---------------------------------------------------------------- config


def test_resolve_provider():
    saved = (config.ANTHROPIC_API_KEY, config.OPENROUTER_API_KEY, config._FORCED_PROVIDER)
    try:
        config._FORCED_PROVIDER = ""
        config.ANTHROPIC_API_KEY, config.OPENROUTER_API_KEY = "sk-ant-x", ""
        eq(config.resolve_provider(), "anthropic", "только ключ Claude — провайдер anthropic")

        config.ANTHROPIC_API_KEY, config.OPENROUTER_API_KEY = "", "sk-or-x"
        eq(config.resolve_provider(), "openrouter", "только ключ OpenRouter — провайдер openrouter")

        config.ANTHROPIC_API_KEY, config.OPENROUTER_API_KEY = "sk-ant-x", "sk-or-x"
        eq(config.resolve_provider(), "anthropic", "оба ключа — приоритет у Claude")

        config.ANTHROPIC_API_KEY, config.OPENROUTER_API_KEY = "", ""
        eq(config.resolve_provider(), "demo", "без ключей — демо-режим")

        config.ANTHROPIC_API_KEY = "sk-ant-x"
        config._FORCED_PROVIDER = "openrouter"
        eq(config.resolve_provider(), "openrouter", "PROVIDER перебивает наличие ключей")
        config._FORCED_PROVIDER = "чепуха"
        eq(config.resolve_provider(), "anthropic", "некорректный PROVIDER игнорируется")
    finally:
        (config.ANTHROPIC_API_KEY, config.OPENROUTER_API_KEY,
         config._FORCED_PROVIDER) = saved

    eq(config._clean("  ВСТАВЬ_СЮДА_ТОКЕН  "), "", "заглушка ВСТАВЬ_… считается пустой")
    eq(config._clean('  "ключ"  '), "ключ", "кавычки и пробелы срезаются")
    eq(config._clean(None), "", "None — пустая строка")


def test_public_url():
    # Режим 1: публичный адрес задан.
    os.environ["PUBLIC_BASE_URL"] = "https://chek.example.com/"
    importlib.reload(config)
    eq(config.PUBLIC_BASE_URL, "https://chek.example.com", "слэш справа срезается")
    eq(config.public_url("/coach?key=abc"), "https://chek.example.com/coach?key=abc",
       "ссылка на кабинет тренера")
    eq(config.public_url("/?key=xyz"), "https://chek.example.com/?key=xyz",
       "ссылка на дашборд")
    eq(config.public_url("coach"), "https://chek.example.com/coach",
       "путь без ведущего слэша")
    eq(config.public_url(), "https://chek.example.com", "пустой путь — сам адрес")
    eq(config.public_url("/coach", host="АДРЕС-СЕРВЕРА"), "https://chek.example.com/coach",
       "host игнорируется, когда задан публичный адрес")

    # Режим 2: публичный адрес не задан — прежнее поведение.
    os.environ["PUBLIC_BASE_URL"] = ""
    importlib.reload(config)
    eq(config.PUBLIC_BASE_URL, "", "пустой публичный адрес")
    port = config.DASHBOARD_PORT
    eq(config.public_url("/coach?key=abc", host="АДРЕС-СЕРВЕРА"),
       f"http://АДРЕС-СЕРВЕРА:{port}/coach?key=abc", "запасной вариант: кабинет как раньше")
    eq(config.public_url("/?key=xyz", host="<адрес-этого-сервера>"),
       f"http://<адрес-этого-сервера>:{port}/?key=xyz", "запасной вариант: дашборд как раньше")
    eq(config.public_url("/coach?key=КЛЮЧ"), f"http://<адрес-сервера>:{port}/coach?key=КЛЮЧ",
       "запасной вариант: host по умолчанию")

    os.environ["PUBLIC_BASE_URL"] = "   "
    importlib.reload(config)
    eq(config.PUBLIC_BASE_URL, "", "строка из пробелов — адрес не задан")

    os.environ["PUBLIC_BASE_URL"] = ""
    importlib.reload(config)


# ---------------------------------------------------------------- web_dashboard


def seed_client(coach_id: int) -> None:
    """Данные клиента, чтобы в кабинете были заполнены все блоки."""
    db.update_user(CLIENT_UID, sex="Женщина", age=28, height_cm=168, weight_kg=60,
                   goal="Похудеть", kcal_target=1800, protein_target=110,
                   fat_target=60, carb_target=180)
    db.add_meal(CLIENT_UID, TODAY, "09:00", "photo", "Омлет с овощами",
                250, 350, 20, 22, 8, 9, "цельная еда")
    db.add_meal(CLIENT_UID, day(1), "13:00", "text", "Салат с рыбой",
                300, 420, 30, 20, 15, 8, "хорошо")
    db.add_water(CLIENT_UID, TODAY, "10:00", 1200)
    db.set_habit(CLIENT_UID, TODAY, "workingin", 1)
    db.add_workout_log(CLIENT_UID, TODAY, "18:00", "done", "турник")
    db.set_wellbeing(CLIENT_UID, TODAY, energy=4, mood=4, stress=2, libido=3)
    db.set_wellbeing(CLIENT_UID, day(1), energy=3, mood=5, stress=3, libido=3)
    sid = db.add_supplement(CLIENT_UID, "Омега-3", "утро")
    db.add_supplement(CLIENT_UID, "Витамин D", "утро")
    db.toggle_supplement_taken(CLIENT_UID, TODAY, sid)
    db.add_lab_result(CLIENT_UID, day(40), "Биохимия", [
        {"name": "Ферритин", "value": 15.0, "unit": "нг/мл",
         "ref_low": 30, "ref_high": 400, "flag": "низко"},
    ])
    db.add_lab_result(CLIENT_UID, TODAY, "Биохимия", [
        {"name": "Ферритин", "value": 45.0, "unit": "нг/мл",
         "ref_low": 30, "ref_high": 400, "flag": "норма"},
    ])
    db.upsert_oura_daily(CLIENT_UID, TODAY, readiness=75, sleep_score=80, sleep_h=7.2, hrv=48)
    db.upsert_oura_daily(CLIENT_UID, day(1), readiness=68, sleep_h=6.5)


def test_client_detail():
    d = web_dashboard.client_detail(CLIENT_UID, days=7)
    eq(d["ok"], True, "срез клиента собран")
    eq(len(d["series"]), 7, "в срезе 7 дней")
    eq(d["series"][-1]["date"], TODAY, "последний день ряда — сегодня")
    eq(d["today"]["meals"], 1, "приёмов пищи сегодня")
    eq(d["today"]["water"], 1200, "вода сегодня")
    eq(d["today"]["wi"], True, "Working In отмечен")
    eq(d["today"]["workout"], "done", "тренировка засчитана")
    eq(d["targets"]["kcal"], 1800, "цель по калориям клиента")
    eq(d["goal"], "Похудеть", "цель клиента")

    ok(d["wellbeing"] is not None, "блок самочувствия заполнен")
    eq(d["wellbeing"]["days"], 2, "дней с самочувствием")
    near(d["wellbeing"]["avg"]["energy"], 3.5, "среднее по энергии")

    ok(d["supplements"] is not None, "блок БАДов заполнен")
    eq(d["supplements"]["total"], 2, "всего БАДов")
    eq(d["supplements"]["taken"], 1, "принято БАДов сегодня")

    ok(d["labs"] is not None, "блок анализов заполнен")
    eq(d["labs"]["last_date"], TODAY, "дата последних анализов")
    near(d["labs"]["markers"][0]["value"], 45.0, "значение последнего маркера")
    near(d["labs"]["markers"][0]["prev"], 15.0, "предыдущее значение маркера (динамика)")
    eq(d["labs"]["abnormal"], 0, "отклонений в последнем бланке нет")

    ok(d["oura"] is not None, "блок Oura заполнен")
    eq(d["oura"]["connected"], True, "Oura отмечена как подключённая")
    eq(len(d["oura"]["series"]), 7, "ряд Oura на 7 дней")
    eq(d["oura"]["latest"]["readiness"], 75, "последняя готовность по Oura")

    txt = web_dashboard.week_data_text(CLIENT_UID)
    ok(isinstance(txt, str) and txt.strip(), "текст недели непустой")
    ok("1800" in txt, "в тексте недели есть цель по калориям")
    ok("Похудеть" in txt, "в тексте недели есть цель клиента")
    ok("Омлет с овощами" in txt, "в тексте недели перечислена еда")


async def _coach_http(cabinet_token: str):
    config.DASHBOARD_TOKEN = "owner-test-key"
    port = free_port()
    runner = await web_dashboard.start_dashboard(port, host="127.0.0.1")
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/coach") as r:
                eq(r.status, 401, "кабинет без ключа — 401")
                body = await r.text()
                ok("ключ" in body.lower() or "кабинет" in body.lower(),
                   "в ответе 401 объяснение про ключ")
            async with s.get(f"{base}/coach?key=нет-такого") as r:
                eq(r.status, 401, "кабинет с неверным ключом — 401")
            async with s.get(f"{base}/coach?key={cabinet_token}") as r:
                eq(r.status, 200, "кабинет с ключом — 200")
                ok((await r.text()).strip().startswith("<"), "кабинет отдал HTML")
            async with s.get(f"{base}/coach/api/clients?key={cabinet_token}") as r:
                eq(r.status, 200, "список клиентов с ключом — 200")
                data = await r.json()
                ok(any(c["uid"] == CLIENT_UID for c in data["clients"]),
                   "клиент есть в списке кабинета")
                ok(data["clients"][0]["flags"], "у клиента проставлен флаг светофора")
            async with s.get(f"{base}/coach/api/client?key={cabinet_token}&uid={CLIENT_UID}") as r:
                eq(r.status, 200, "карточка клиента — 200")
            async with s.get(f"{base}/coach/api/client?key={cabinet_token}&uid=999999") as r:
                ok(r.status in (400, 403, 404), "чужой клиент не отдаётся")
            async with s.get(f"{base}/") as r:
                eq(r.status, 401, "дашборд владельца без ключа — 401")
            async with s.get(f"{base}/?key=owner-test-key") as r:
                eq(r.status, 200, "дашборд владельца с ключом — 200")
    finally:
        await runner.cleanup()


def test_coach_http(cabinet_token: str):
    asyncio.run(_coach_http(cabinet_token))


# ---------------------------------------------------------------- analyzer (всё замокано)


MEAL_PAYLOAD = {
    "is_food": True,
    "dish": "Гречка с курицей",
    "total_grams": "400",
    "total_kcal": "620.5",
    "total_protein_g": 45,
    "total_fat_g": 15,
    "total_carbs_g": 70,
    "chek_score": 99,            # заведомо вне диапазона — должно ужаться до 10
    "chek_verdict": "цельная еда",
    "confidence": "непонятная",  # некорректное — должно стать «средняя»
    "items": "не список",        # некорректное — должно стать []
}


class _FakeBlock:
    def __init__(self, data):
        self.type = "tool_use"
        self.name = "report_meal"
        self.input = data


class _FakeResp:
    def __init__(self, data):
        self.content = [_FakeBlock(data)]


class _FakeMessages:
    def __init__(self, outer):
        self._outer = outer

    async def create(self, **kwargs):
        self._outer.calls += 1
        self._outer.last_kwargs = kwargs
        return _FakeResp(self._outer.payload)


class _FakeAnthropic:
    """Подменяет anthropic.AsyncAnthropic — наружу ничего не уходит."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last_kwargs = None
        self.messages = _FakeMessages(self)


def test_extract_json():
    eq(analyzer._extract_json('{"a": 1}'), {"a": 1}, "чистый JSON")
    eq(analyzer._extract_json('```json\n{"a": 2}\n```'), {"a": 2}, "JSON в блоке ```json")
    eq(analyzer._extract_json('```\n{"a": 3}\n```'), {"a": 3}, "JSON в безымянном блоке")
    eq(analyzer._extract_json('Вот ответ: {"a": 4} — готово'), {"a": 4},
       "JSON среди лишнего текста")
    eq(analyzer._extract_json('{"a": {"b": 5}} хвост'), {"a": {"b": 5}}, "вложенный JSON")
    eq(analyzer._extract_json('{"s": "фигурная } внутри строки", "a": 6}'),
       {"s": "фигурная } внутри строки", "a": 6}, "скобка внутри строки не сбивает разбор")
    raises(ValueError, lambda: analyzer._extract_json("совсем не json"), "мусор — ValueError")
    raises(ValueError, lambda: analyzer._extract_json(""), "пустая строка — ValueError")


def test_normalize():
    n = analyzer._normalize(MEAL_PAYLOAD)
    eq(n["chek_score"], 10, "балл Чека ужат до 10")
    eq(analyzer._normalize({"chek_score": -5})["chek_score"], 1, "балл Чека поднят до 1")
    eq(n["total_kcal"], 620.5, "калории приведены к float")
    eq(n["total_grams"], 400.0, "граммы приведены к float")
    eq(n["confidence"], "средняя", "некорректная уверенность заменена")
    eq(n["items"], [], "items приведён к списку")
    eq(analyzer._normalize({})["dish"], "Приём пищи", "название по умолчанию")
    eq(analyzer._normalize({"total_kcal": "чепуха"})["total_kcal"], 0.0,
       "нечисловые калории — 0.0")


async def _meal_anthropic():
    fake = _FakeAnthropic(MEAL_PAYLOAD)
    saved_client, saved_provider = analyzer._client, config.ACTIVE_PROVIDER
    analyzer._client = lambda: fake
    config.ACTIVE_PROVIDER = "anthropic"
    try:
        data = await analyzer.analyze_meal(image_bytes=b"fake photo bytes",
                                           media_type="image/jpeg", caption="обед")
        eq(data["dish"], "Гречка с курицей", "Claude: блюдо разобрано")
        eq(data["total_kcal"], 620.5, "Claude: калории разобраны")
        eq(data["chek_score"], 10, "Claude: балл нормализован")
        eq(fake.calls, 1, "Claude: ровно один вызов модели")
        ok("обед" in fake.last_kwargs["messages"][0]["content"][-1]["text"],
           "Claude: подпись пользователя попала в запрос")
        eq(fake.last_kwargs["messages"][0]["content"][0]["type"], "image",
           "Claude: фото передано как изображение")
    finally:
        analyzer._client, config.ACTIVE_PROVIDER = saved_client, saved_provider


async def _meal_openrouter():
    calls = []

    async def fake_or_chat(session, body):
        calls.append(body)
        return "```json\n" + json.dumps(MEAL_PAYLOAD, ensure_ascii=False) + "\n```"

    saved = (analyzer._or_chat, config.ACTIVE_PROVIDER,
             config.OPENROUTER_MODEL, config.OPENROUTER_API_KEY)
    analyzer._or_chat = fake_or_chat
    config.ACTIVE_PROVIDER = "openrouter"
    config.OPENROUTER_MODEL = "stub/model"   # не "auto": иначе полез бы за списком моделей
    config.OPENROUTER_API_KEY = "sk-or-test"
    try:
        data = await analyzer.analyze_meal(text="гречка с курицей")
        eq(data["dish"], "Гречка с курицей", "OpenRouter: блюдо разобрано из ```-блока")
        eq(data["chek_score"], 10, "OpenRouter: балл нормализован")
        eq(len(calls), 1, "OpenRouter: ровно один запрос")
        eq(calls[0]["model"], "stub/model", "OpenRouter: модель взята из конфига")
        ok("гречка с курицей" in json.dumps(calls[0], ensure_ascii=False),
           "OpenRouter: текст пользователя попал в запрос")
    finally:
        (analyzer._or_chat, config.ACTIVE_PROVIDER,
         config.OPENROUTER_MODEL, config.OPENROUTER_API_KEY) = saved


async def _meal_openrouter_retry():
    """Первый ответ — мусор, второй — валидный JSON: должна сработать повторная попытка."""
    answers = ["Конечно! Вот разбор блюда без всякого JSON.",
               json.dumps(MEAL_PAYLOAD, ensure_ascii=False)]
    calls = []

    async def fake_or_chat(session, body):
        calls.append(body)
        return answers[len(calls) - 1]

    saved = (analyzer._or_chat, config.ACTIVE_PROVIDER,
             config.OPENROUTER_MODEL, config.OPENROUTER_API_KEY)
    analyzer._or_chat = fake_or_chat
    config.ACTIVE_PROVIDER = "openrouter"
    config.OPENROUTER_MODEL = "stub/model"
    config.OPENROUTER_API_KEY = "sk-or-test"
    try:
        data = await analyzer.analyze_meal(text="что-то съедобное")
        eq(len(calls), 2, "OpenRouter: понадобилась повторная строгая попытка")
        eq(data["dish"], "Гречка с курицей", "OpenRouter: со второй попытки разобрано")
    finally:
        (analyzer._or_chat, config.ACTIVE_PROVIDER,
         config.OPENROUTER_MODEL, config.OPENROUTER_API_KEY) = saved


async def _meal_demo():
    saved = config.ACTIVE_PROVIDER
    config.ACTIVE_PROVIDER = "demo"
    try:
        try:
            await analyzer.analyze_meal(text="что угодно")
            ok(False, "демо-режим: ожидалось исключение DemoModeError")
        except analyzer.DemoModeError:
            ok(True, "демо-режим: analyze_meal бросает DemoModeError")
    finally:
        config.ACTIVE_PROVIDER = saved


def test_analyzer_meals():
    asyncio.run(_meal_anthropic())
    asyncio.run(_meal_openrouter())
    asyncio.run(_meal_openrouter_retry())
    asyncio.run(_meal_demo())


# ---------------------------------------------------------------- запуск


def main() -> int:
    print(f"Временная база: {os.environ['DB_PATH']}")
    db.init_db()

    print("- db: пользователи и настройки")
    test_users()
    print("- db: еда и суммы за день")
    test_meals()
    print("- db: вода")
    test_water()
    print("- db: БАДы")
    test_supplements()
    print("- db: самочувствие")
    test_wellbeing()
    print("- db: анализы")
    test_labs()
    print("- db: тренеры и клиенты")
    coach_id = test_coaches()
    print("- db: Oura")
    test_oura_db()
    print("- nutrition: нормы КБЖУ и воды")
    test_nutrition()
    print("- config: выбор провайдера")
    test_resolve_provider()
    print("- config: публичные ссылки")
    test_public_url()

    seed_client(coach_id)
    print("- кабинет: срез клиента и текст недели")
    test_client_detail()
    print("- кабинет: HTTP на localhost")
    test_coach_http("cab-key-1")

    print("- analyzer: извлечение JSON")
    test_extract_json()
    print("- analyzer: нормализация")
    test_normalize()
    print("- analyzer: разбор еды (моки)")
    test_analyzer_meals()

    if _fails:
        print(f"\nПРОВАЛЕНО: {len(_fails)} из {_checks}")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print(f"\nВсе проверки пройдены: {_checks}")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMPDIR, ignore_errors=True)
    sys.exit(code)
