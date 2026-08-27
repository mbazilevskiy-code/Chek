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
import oura as oura_mod  # noqa: E402
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

    # Повторный ensure_user обновляет имя; без coach_id привязка не трогается.
    db.ensure_user(UID, "Владелец 2")
    u = db.get_user(UID)
    eq(u["name"], "Владелец 2", "имя обновляется")
    eq(u["coach_id"], None, "без coach_id пользователь остаётся без тренера")

    ok(UID in db.all_user_ids(), "пользователь в общем списке")

    db.set_setting("проба", "значение")
    eq(db.get_setting("проба"), "значение", "настройка читается")
    db.set_setting("проба", "новое")
    eq(db.get_setting("проба"), "новое", "настройка перезаписывается")
    eq(db.get_setting("нет-такой"), None, "неизвестная настройка — None")


# ---------------------------------------------------------------- db: еда


def test_wal_mode():
    """WAL нужен непрерывному бэкапу (litestream) и параллельному чтению."""
    eq(db.journal_mode(), "wal", "база работает в режиме WAL")
    # Данные в WAL-режиме читаются и пишутся как обычно
    db.set_setting("wal-проба", "значение")
    eq(db.get_setting("wal-проба"), "значение", "запись и чтение в WAL работают")
    ok(os.path.exists(os.environ["DB_PATH"]), "файл базы на месте")


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


def test_late_join_coach():
    """Человек мог завестись раньше в другом боте «Чека» — он всё равно должен
    попасть в кабинет тренера, а не остаться молча невидимым."""
    late = 6006
    db.ensure_user(late, "Михаил")            # завёлся в личном боте
    db.update_user(late, consent=1)           # согласие, данное там же
    eq(db.get_user(late)["coach_id"], None, "сначала тренера у него нет")

    coach = db.add_coach("222333444:AA-late", "Николай", "Челлендж", "cab-late")
    cid = coach["id"]
    db.ensure_user(late, "Михаил", coach_id=cid)
    u = db.get_user(late)
    eq(u["coach_id"], cid, "существующий пользователь привязывается к тренеру")
    eq(u["consent"], 0, "старое согласие сбрасывается — бот тренера переспросит")
    eq(len(db.clients_of_coach(cid)), 0, "до нового согласия в кабинете не виден")

    db.update_user(late, consent=1)
    eq([x["user_id"] for x in db.clients_of_coach(cid)], [late],
       "после согласия появляется в кабинете тренера")

    # Уже занятого клиента другой тренер не забирает
    other = db.add_coach("555666777:AA-other", "Другой", "Другой бренд", "cab-other")
    db.ensure_user(late, "Михаил", coach_id=other["id"])
    eq(db.get_user(late)["coach_id"], cid, "чужого клиента второй тренер не уводит")
    eq(db.get_user(late)["consent"], 1, "и согласие у него не сбрасывается")
    eq(len(db.clients_of_coach(other["id"])), 0, "у второго тренера клиентов не появилось")

    # Новый пользователь сразу приходит к тренеру — но согласия ещё не давал
    fresh = 6007
    db.ensure_user(fresh, "Новичок", coach_id=cid)
    f = db.get_user(fresh)
    eq(f["coach_id"], cid, "новый пользователь привязан сразу")
    eq(f["consent"], 0, "и согласия ещё не давал")


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
    near(d["oura"]["avg"]["readiness"], 71.5, "средняя готовность за окно")
    eq(d["oura"]["avg"]["sleep_h"], 6.8, "средний сон за окно (с округлением до 0,1)")

    eq(d["last_activity"], TODAY, "последняя запись клиента — сегодня")
    eq(d["bucket"], "day", "на неделе ряд остаётся дневным")

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
            for period in ("1", "30", "365", "all"):
                url = f"{base}/coach/api/client?key={cabinet_token}&uid={CLIENT_UID}&days={period}"
                async with s.get(url) as r:
                    eq(r.status, 200, f"карточка за период {period} — 200")
                    payload = await r.json()
                    ok(payload.get("ok"), f"payload за период {period} собран")
                    ok(payload.get("days", 0) >= 1, f"период {period}: days в ответе")
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


# ------------------------------------- период просмотра, агрегация и адгеренс БАДов

PLAN_UID = 4004     # отдельный клиент под тесты плана приёма


def test_supplement_plan():
    db.ensure_user(PLAN_UID, "Клиент с планом")
    daily = db.add_supplement(PLAN_UID, "Витамин D", "утром")          # план по умолчанию
    thrice = db.add_supplement(PLAN_UID, "Магний", "вечером", 3)
    eq(db.get_supplement(daily)["plan_days_per_week"], 7, "план по умолчанию — каждый день")
    eq(db.get_supplement(thrice)["plan_days_per_week"], 3, "план «3 раза в неделю» сохранён")

    hi = db.add_supplement(PLAN_UID, "Слишком часто", "", 99)
    lo = db.add_supplement(PLAN_UID, "Слишком редко", "", -3)
    eq(db.get_supplement(hi)["plan_days_per_week"], 7, "план больше 7 ужат до 7")
    eq(db.get_supplement(lo)["plan_days_per_week"], 1, "план меньше 1 поднят до 1")
    db.deactivate_supplement(PLAN_UID, hi)
    db.deactivate_supplement(PLAN_UID, lo)

    week = [day(i) for i in range(7)]
    for i in (0, 1, 2, 4, 6):          # 5 дней из 7 при плане «каждый день»
        db.toggle_supplement_taken(PLAN_UID, day(i), daily)
    for i in (0, 1, 3, 4, 5):          # 5 дней при плане 3 — приём сверх плана
        db.toggle_supplement_taken(PLAN_UID, day(i), thrice)

    counts = db.supplement_taken_days(PLAN_UID, week)
    eq(counts[daily], 5, "supplement_taken_days: 5 дней приёма")
    eq(counts[thrice], 5, "supplement_taken_days: 5 дней у второго БАДа")
    eq(db.supplement_taken_days(PLAN_UID, []), {}, "supplement_taken_days: пустое окно")
    eq(db.supplement_taken_days(PLAN_UID, [day(3)])[thrice], 1, "окно в один день")
    dts = db.supplement_taken_dates(PLAN_UID, week)
    eq(dts[daily][0], day(6), "даты приёма отсортированы по возрастанию")
    eq(len(dts[daily]), 5, "дат приёма ровно столько же, сколько дней")


def test_adherence_payload():
    d = web_dashboard.client_detail(PLAN_UID, days=7)
    rows = {r["name"]: r for r in d["supplements"]["list"]}
    eq(len(rows), 2, "в адгеренсе только активные БАДы")

    eq(rows["Витамин D"]["planned"], 7, "план за неделю при «каждый день» — 7")
    eq(rows["Витамин D"]["taken"], 5, "принято 5 из 7")
    eq(rows["Витамин D"]["plan_days_per_week"], 7, "план отдаётся во фронт")
    eq(len(rows["Витамин D"]["taken_dates"]), 5, "даты приёма в payload — для точек по дням")
    eq(rows["Витамин D"]["today_taken"], True, "отметка за сегодня отдельно")

    eq(rows["Магний"]["planned"], 3, "план за неделю при «3 раза в неделю» — 3")
    eq(rows["Магний"]["taken"], 5, "приём сверх плана не обрезается")
    ok(rows["Магний"]["taken"] > rows["Магний"]["planned"], "5 из 3 отдаётся как есть")

    eq(d["supplements"]["window_days"], 7, "окно в payload БАДов")
    eq(d["supplements"]["planned_total"], 10, "суммарный план за окно")
    eq(d["supplements"]["taken_total"], 10, "суммарно принято за окно")
    eq(d["supplements"]["total"], 2, "всего активных БАДов")

    m = web_dashboard.client_detail(PLAN_UID, days=30)
    rows30 = {r["name"]: r for r in m["supplements"]["list"]}
    eq(rows30["Витамин D"]["planned"], 30, "план за 30 дней при «каждый день» — 30")
    eq(rows30["Магний"]["planned"], 13, "план за 30 дней при 3 раза/нед — 13")
    eq(rows30["Магний"]["taken"], 5, "факт за 30 дней тот же — принимал только на этой неделе")

    one = web_dashboard.client_detail(PLAN_UID, days=1)
    rows1 = {r["name"]: r for r in one["supplements"]["list"]}
    eq(rows1["Витамин D"]["planned"], 1, "план на сегодня при «каждый день» — 1")
    eq(one["supplements"]["window_days"], 1, "окно в один день")


def test_migration_idempotent():
    import sqlite3

    def suppl_columns():
        con = sqlite3.connect(os.environ["DB_PATH"])
        try:
            return [r[1] for r in con.execute("PRAGMA table_info(supplements)")]
        finally:
            con.close()

    before = suppl_columns()
    eq(before.count("plan_days_per_week"), 1, "колонка плана заведена ровно один раз")
    kept = len(db.list_supplements(PLAN_UID))

    db.init_db()
    db.init_db()          # повторная миграция не должна ничего ломать
    after = suppl_columns()
    eq(after.count("plan_days_per_week"), 1, "после повторной миграции колонка не дублируется")
    eq(after, before, "набор колонок supplements не изменился")
    eq(len(db.list_supplements(PLAN_UID)), kept, "данные БАДов на месте")
    eq(db.supplement_taken_days(PLAN_UID, [day(i) for i in range(7)])
       [db.list_supplements(PLAN_UID)[0]["id"]], 5, "отметки приёма пережили миграцию")


def test_resolve_days():
    rd = web_dashboard.resolve_days
    eq(rd(CLIENT_UID, "1"), 1, "период «сегодня» — 1 день")
    eq(rd(CLIENT_UID, "7"), 7, "период «неделя» — 7 дней")
    eq(rd(CLIENT_UID, "30"), 30, "период «месяц» — 30 дней")
    eq(rd(CLIENT_UID, "365"), 365, "период «год» — 365 дней")
    eq(rd(CLIENT_UID, None), 7, "без параметра — неделя")
    eq(rd(CLIENT_UID, "чепуха"), 7, "нечисловой период — неделя")
    eq(rd(CLIENT_UID, "0"), 1, "минимум — один день")
    eq(rd(CLIENT_UID, "99999"), web_dashboard.MAX_DAYS, "потолок окна — 3650 дней")
    eq(web_dashboard.MAX_DAYS, 3650, "потолок поднят с 90 до 3650")

    # «Всё время» — от первой активности клиента
    eq(db.first_activity_date(CLIENT_UID), day(40), "первая активность — самая ранняя дата")
    eq(db.first_activity_date(987654), None, "у неизвестного клиента активности нет")
    eq(rd(CLIENT_UID, "all"), 41, "«всё время» — от первой активности включительно")
    eq(rd(987654, "all"), 7, "нет данных вообще — фолбэк на неделю")


def test_weekly_buckets():
    rows = [{"date": day(i), "label": "", "kcal": 100 + i, "water": 200} for i in range(14)]
    b = web_dashboard._bucket_weekly(rows, ("kcal", "water"))
    ok(2 <= len(b) <= 3, "14 дней схлопываются в 2–3 ISO-недели")
    ok(all(x["kcal"] is not None for x in b), "среднее по неделе посчитано")
    ok(all(x["days"] >= 1 for x in b), "в каждой неделе указано число дней")
    eq(b[0]["date"], min(r["date"] for r in rows[7:]) if len(b) > 1 else b[0]["date"],
       "недели идут по возрастанию даты")
    eq(web_dashboard._bucket_weekly([], ("kcal",)), [], "пустой ряд — пустой результат")

    short = web_dashboard.client_detail(CLIENT_UID, days=30)
    eq(short["bucket"], "day", "30 дней — ещё по дням")
    eq(len(short["series"]), 30, "30 точек")

    long_ = web_dashboard.client_detail(CLIENT_UID, days=90)
    eq(long_["bucket"], "week", "90 дней — схлопнуто в недели")
    ok(12 <= len(long_["series"]) <= 15, "90 дней — примерно 13 недель")
    ok(all("days" in x for x in long_["series"]), "у каждой недели есть число дней")
    ok(long_["stats"]["avg_kcal"] > 0, "плитки по-прежнему считаются по дневным данным")
    if long_["oura"]:
        eq(len(long_["oura"]["series"]), len(long_["series"]), "ряд Oura схлопнут так же")
    eq(long_["last_activity"], TODAY, "последняя запись считается до схлопывания")


def test_food_breakdown():
    db.add_meal(CLIENT_UID, TODAY, "19:30", "photo", "Курица с гречкой и салатом",
                450, 620, 48, 18, 60, 8, "цельная еда, хороший баланс",
                raw={"items": [{"name": "Куриная грудка", "grams": 180, "kcal": 300},
                               {"name": "Гречка", "grams": 150, "kcal": 220},
                               {"name": "Салат", "grams": 120, "kcal": 100}],
                     "chek_tip": "Добавь зелени",
                     "assumptions": "Масло в салате — примерно чайная ложка",
                     "confidence": "высокая"})

    eq(db.meals_count(CLIENT_UID, []), 0, "meals_count: пустое окно")
    eq(db.meals_detailed(CLIENT_UID, [], 10), [], "meals_detailed: пустое окно")
    eq(db.meals_detailed(CLIENT_UID, [TODAY], 1)[0]["date"], TODAY, "meals_detailed: свежие первыми")

    d = web_dashboard.client_detail(CLIENT_UID, days=7)
    f = d["food"]
    ok(f["days"], "дневник еды собран")
    eq(f["days"][0]["date"], TODAY, "свежий день сверху")
    eq(f["total"], 3, "всего приёмов за окно")
    eq(f["truncated"], False, "обрезки нет")

    today_day = f["days"][0]
    eq(len(today_day["meals"]), 2, "два приёма сегодня")
    eq(today_day["meals"][0]["time"], "09:00", "приёмы внутри дня отсортированы по времени")
    eq(today_day["meals"][1]["dish"], "Курица с гречкой и салатом", "название блюда видно")
    eq(today_day["kcal"], 970, "сумма калорий за день")
    near(today_day["chek"], 8.5, "средний Чек за день")

    m = today_day["meals"][1]
    eq(len(m["items"]), 3, "состав приёма разобран из raw_json")
    eq(m["items"][0]["name"], "Куриная грудка", "компонент состава")
    eq(m["items"][0]["kcal"], 300, "калории компонента")
    eq(m["verdict"], "цельная еда, хороший баланс", "вердикт по Чеку в карточке")
    eq(m["tip"], "Добавь зелени", "совет ИИ")
    ok(m["assumptions"], "допущения ИИ отдаются")
    eq(m["confidence"], "высокая", "уверенность ИИ")
    eq(m["source"], "photo", "источник приёма — фото")
    eq(m["protein"], 48, "белок приёма")
    eq(m["chek_score"], 8, "балл Чека по приёму")

    plain = [x for x in today_day["meals"] if x["dish"] == "Омлет с овощами"][0]
    eq(plain["items"], [], "приём без raw_json — пустой состав, без падения")
    eq(plain["tip"], "", "приём без raw_json — без совета")

    ok("meals" not in d, "плоский список приёмов убран из карточки — его заменяет food")

    saved = web_dashboard.FOOD_LIMIT
    web_dashboard.FOOD_LIMIT = 2
    try:
        cut = web_dashboard.client_detail(CLIENT_UID, days=7)["food"]
        eq(cut["shown"], 2, "отдано не больше лимита")
        eq(cut["total"], 3, "полное число приёмов известно")
        eq(cut["truncated"], True, "обрезка помечена явно, а не молча")
    finally:
        web_dashboard.FOOD_LIMIT = saved

    empty = web_dashboard.client_detail(PLAN_UID, days=7)["food"]
    eq(empty["days"], [], "клиент без еды — пустой дневник")
    eq(empty["total"], 0, "и ноль записей")


def test_suppl_plan_in_bot():
    import bot as botmod

    eq(botmod._parse_plan("каждый день"), 7, "«каждый день» → 7")
    eq(botmod._parse_plan("6 раз в неделю"), 6, "«6 раз в неделю» → 6")
    eq(botmod._parse_plan("3 раза в неделю"), 3, "«3 раза в неделю» → 3")
    eq(botmod._parse_plan("1 раз в неделю"), 1, "«1 раз в неделю» → 1")
    eq(botmod._parse_plan("ежедневно"), 7, "синоним «ежедневно» → 7")
    eq(botmod._parse_plan("чепуха"), None, "непонятный ответ — не разобрали")
    eq(botmod._parse_plan(None), None, "пустой ответ — не разобрали")

    names = [h.callback.__name__ for h in botmod.router.message.handlers]
    ok("add_suppl_plan" in names, "шаг плана приёма зарегистрирован в FSM")
    ok(names.index("add_suppl_timing") < names.index("add_suppl_plan"),
       "шаг плана идёт после шага времени приёма")

    txt = botmod.suppl_text(PLAN_UID, TODAY)
    ok("Витамин D" in txt, "в /supplements есть название добавки")
    ok("каждый день" in txt, "в /supplements виден план «каждый день»")
    ok("3 раза в неделю" in txt, "в /supplements виден план «N раз в неделю»")
    ok("утром" in txt, "в /supplements осталось время приёма")


def test_labs_window():
    db.add_lab_result(PLAN_UID, day(50), "Биохимия", [
        {"name": "Ферритин", "value": 60.0, "unit": "нг/мл",
         "ref_low": 30, "ref_high": 400, "flag": "норма"},
    ])
    # Бланк старше окна не прячем: анализы сдают раз в несколько месяцев,
    # иначе блок был бы пустым почти всегда. Показываем и помечаем.
    week = web_dashboard.client_detail(PLAN_UID, days=7)
    ok(week["labs"] is not None, "бланк старше окна всё равно показывается")
    eq(week["labs"]["in_window"], False, "и помечен как старше выбранного периода")
    eq(week["labs"]["last_date"], day(50), "показан последний бланк")
    ok(week["labs"]["markers"], "маркеры старого бланка не потерялись")

    far = web_dashboard.client_detail(PLAN_UID, days=365)
    ok(far["labs"] is not None, "на окне в год бланк виден")
    eq(far["labs"]["in_window"], True, "и попадает в окно")
    eq(far["labs"]["last_date"], day(50), "дата бланка внутри окна")
    eq(len(far["labs"]["dates"]), 1, "в окно попал один бланк")

    # Свежий бланк внутри окна помечается как «в окне»
    eq(web_dashboard.client_detail(CLIENT_UID, days=7)["labs"]["in_window"], True,
       "сегодняшний бланк — внутри недельного окна")

    # А когда анализов нет вообще — честное «Данных нет»
    nolabs = 5005
    db.ensure_user(nolabs, "Без анализов")
    ok(web_dashboard.client_detail(nolabs, days=365)["labs"] is None,
       "нет ни одного бланка — блок пустой")

    # Бриф собирается за неделю, но анализы ему нужны любой давности
    wide = web_dashboard.client_detail(PLAN_UID, days=7, labs_days=web_dashboard.MAX_DAYS)
    ok(wide["labs"] is not None, "labs_days расширяет окно анализов")
    eq(len(wide["series"]), 7, "при этом сам срез остаётся недельным")
    ok("Анализы" in web_dashboard.week_data_text(PLAN_UID),
       "старые анализы не выпадают из текста для AI-брифа")


# ------------- тон общения с клиентом: бот собирает данные, советует тренер

LABS_UID = 7007
VOICE_UID = 7010
FAKE_COACH_BOT = 999001


class _FakeNote:
    def __init__(self):
        self.text = None

    async def edit_text(self, text, **kw):
        self.text = text


class _FakeMessage:
    """Ровно столько от Message, сколько нужно обработчику. Без сети."""

    def __init__(self, uid, bot_id):
        self.from_user = type("U", (), {"id": uid, "first_name": "Тест"})()
        self.bot = type("B", (), {"id": bot_id})()
        self.note = _FakeNote()
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self.note


def _fake_coach_bot(botmod):
    """Регистрируем бота тренера, чтобы is_client_bot() включил клиентский режим."""
    botmod.COACH_BY_BOT[FAKE_COACH_BOT] = {
        "id": 1, "name": "Николай", "brand": "Челлендж",
        "coach_user_id": 555000, "cabinet_token": "cab-x", "bot_username": "coach_bot",
    }


def test_client_meal_reply():
    import bot as botmod

    data = {
        "dish": "Круассан с начинкой", "confidence": "высокая",
        "total_grams": 120, "total_kcal": 420, "total_protein_g": 9,
        "total_fat_g": 24, "total_carbs_g": 42, "chek_score": 3,
        "chek_verdict": "сильно обработанная выпечка из белой муки",
        "chek_tip": "Замени на цельнозерновой вариант с яйцом",
        "items": [],
    }
    reply = botmod.fmt_meal_reply(data, [], None)
    ok("Круассан с начинкой" in reply, "название блюда в ответе клиенту")
    ok("420" in reply, "калории в ответе клиенту")
    ok("Б 9" in reply and "Ж 24" in reply and "У 42" in reply, "БЖУ в ответе клиенту")
    ok("По Чеку: 3/10" in reply, "балл Чека показан числом")
    ok("Замени на цельнозерновой" not in reply, "совета ИИ клиенту нет")
    ok("обработанная выпечка" not in reply, "вердикта ИИ клиенту нет")
    ok("💡" not in reply, "строки с советом нет")

    # Вердикт и совет всё так же запрашиваются у ИИ — они нужны тренеру в кабинете
    ok("chek_verdict" in analyzer.MEAL_SCHEMA["required"],
       "вердикт по-прежнему запрашивается у модели — он уходит тренеру")
    ok("chek_tip" in analyzer.MEAL_SCHEMA["properties"],
       "совет по-прежнему в схеме — он уходит тренеру")


def test_client_day_overview():
    import bot as botmod
    txt = botmod.build_day_overview(CLIENT_UID, TODAY)
    ok("Еда по Чеку" in txt, "в сводке дня есть балл Чека")
    ok("Вода" in txt, "в сводке дня есть вода")
    for phrase in ("Пол бы", "так держать", "образцовый", "Завтра — цельная еда"):
        ok(phrase not in txt, f"в сводке дня нет оценочной фразы «{phrase}»")


async def _labs_confirm(bot_id):
    import bot as botmod

    parsed = {
        "is_lab": True, "panel": "Биохимия", "date": TODAY,
        "markers": [
            {"name": "Ферритин", "value": 15.0, "value_text": None, "unit": "нг/мл",
             "ref_low": 30, "ref_high": 400, "flag": "низко"},
            {"name": "B12", "value": 500.0, "value_text": None, "unit": "пг/мл",
             "ref_low": 200, "ref_high": 900, "flag": "норма"},
        ],
    }

    async def fake_parse(data, media_type):
        return parsed

    saved = botmod.analyzer.parse_labs
    botmod.analyzer.parse_labs = fake_parse
    try:
        m = _FakeMessage(LABS_UID, bot_id)
        await botmod._process_labs(m, b"fake-pdf", "application/pdf", None)
        return m.note.text
    finally:
        botmod.analyzer.parse_labs = saved


def test_client_labs_confirmation():
    import bot as botmod
    _fake_coach_bot(botmod)
    db.ensure_user(LABS_UID, "Клиент анализов")

    txt = asyncio.run(_labs_confirm(FAKE_COACH_BOT))
    ok("Разобрал бланк" in txt, "подтверждение загрузки нейтральное")
    ok("2 показателей" in txt, "сказано, сколько показателей разобрано")
    ok("1 вне нормы" in txt, "сказано, сколько вне нормы")
    ok("Передал тренеру" in txt, "сказано, что данные ушли тренеру")
    ok("Диагнозы не ставлю" in txt, "медицинская граница проговорена")
    ok("/labreport" not in txt, "нет отсылки к разбору с рекомендациями")
    ok("рекоменд" not in txt.lower(), "в подтверждении нет рекомендаций")

    # В личном боте владельца формулировка своя — тренера там нет
    own = asyncio.run(_labs_confirm(123456))
    ok("Записал в дневник" in own, "в личном боте про тренера не пишем")
    ok("Передал тренеру" not in own, "в личном боте нет упоминания тренера")

    overview = botmod.labs_overview_text(LABS_UID)
    ok("/labupload" in overview, "в /labs осталась загрузка нового бланка")
    ok("/labreport" not in overview, "из /labs убран разбор с рекомендациями")
    ok("Ферритин" in overview, "фактические показатели вне нормы клиенту показываем")


def test_onboarding_lists_features():
    import bot as botmod

    greeting = botmod.coach_greeting({"name": "Николай", "brand": "Челлендж"}, "Михаил")
    for kw in ("Еда", "Вода", "Тренировк", "БАД", "Самочувствие", "Анализ", "Oura", "/help"):
        ok(kw in greeting, f"в приветствии клиента есть: {kw}")
    ok("Михаил" in greeting, "клиента зовут по имени")
    ok("Николай" in greeting, "тренер назван по имени")
    ok("разбор и советы даёт" in greeting, "ожидание задано: разбор у тренера")

    help_low = botmod.HELP_TEXT.lower()
    for kw in ("вода", "тренировк", "бад", "самочувств", "анализ", "oura",
               "/help", "/feel", "/oura", "/bad", "/train", "/water"):
        ok(kw in help_low, f"в /help есть: {kw}")
    ok("Я собираю твои данные для тренера" in botmod.CLIENT_NOTE,
       "приписка про роль бота готова для клиента")

    # Согласие по-прежнему не сломано: обработчики на месте
    names = [h.callback.__name__ for h in botmod.router.callback_query.handlers]
    ok("cb_consent_yes" in names, "обработчик согласия на месте")
    ok("cb_consent_no" in names, "обработчик отказа на месте")


WO_UID = 8008


def test_workout_log():
    """Тренировку не генерируем, а фиксируем со слов клиента."""
    import bot as botmod

    eq(botmod._parse_hhmm("18:30"), "18:30", "время «18:30»")
    eq(botmod._parse_hhmm("9.05"), "09:05", "время «9.05» с ведущим нулём")
    eq(botmod._parse_hhmm("1830"), "18:30", "время «1830»")
    ok(botmod._parse_hhmm("сейчас") is not None, "«сейчас» — текущее время")
    ok(botmod._parse_hhmm("") is not None, "пусто — текущее время")
    eq(botmod._parse_hhmm("25:00"), None, "невалидные часы не проходят")
    eq(botmod._parse_hhmm("чепуха"), None, "мусор во времени не проходит")

    eq(botmod._parse_duration("40"), 40, "«40» → 40 мин")
    eq(botmod._parse_duration("40 мин"), 40, "«40 мин» → 40")
    eq(botmod._parse_duration("1 час"), 60, "«1 час» → 60")
    eq(botmod._parse_duration("1.5 часа"), 90, "«1.5 часа» → 90")
    eq(botmod._parse_duration("1 ч 20"), 80, "«1 ч 20» → 80")
    eq(botmod._parse_duration("1 час 20 мин"), 80, "«1 час 20 мин» → 80")
    eq(botmod._parse_duration("чепуха"), None, "мусор в длительности не проходит")
    eq(botmod._parse_duration("0"), None, "ноль минут не принимаем")
    eq(botmod._parse_duration("999"), None, "неправдоподобную длительность не принимаем")

    db.add_workout_log(WO_UID, TODAY, "18:30", "done", note="train", duration_min=40,
                       description="турник: 5х8 подтягиваний, брусья 4х10")
    w = db.workout_for_date(WO_UID, TODAY)
    eq(w["status"], "done", "статус сделанной тренировки")
    eq(w["time"], "18:30", "время сохранено")
    eq(w["duration_min"], 40, "длительность сохранена")
    ok("подтягиваний" in w["description"], "описание сохранено")

    db.add_workout_log(WO_UID, day(1), "20:00", "skipped", note="train")
    prev = db.workouts_by_date(WO_UID, [TODAY, day(1)])
    eq(prev[TODAY], "done", "светофор: тренировка сделана")
    eq(prev[day(1)], "skipped", "светофор: не тренировался")
    eq(db.workout_for_date(WO_UID, day(1))["duration_min"], None,
       "у пропуска длительности нет")

    det = db.workouts_detailed(WO_UID, [TODAY, day(1)])
    eq(len(det), 2, "обе записи в выборке")
    eq(det[0]["date"], TODAY, "свежие первыми")
    eq(db.workouts_detailed(WO_UID, []), [], "пустое окно — пусто")

    names = [h.callback.__name__ for h in botmod.router.message.handlers]
    for n in ("lw_when", "lw_duration", "lw_description", "lw_ignore_commands"):
        ok(n in names, f"шаг записи тренировки зарегистрирован: {n}")
    ok(names.index("cmd_cancel") < names.index("lw_when"), "/cancel раньше шагов записи")
    ok(names.index("lw_ignore_commands") < names.index("lw_when"),
       "заслон от команд раньше шагов записи")


OURA_UID = 9009
AUTO_UID = 9010


def _oura_payloads(day: str) -> dict:
    return {
        "daily_readiness": [{"day": day, "score": 82}],
        "daily_sleep": [{"day": day, "score": 77}],
        "daily_activity": [{"day": day, "score": 88, "steps": 9400,
                            "active_calories": 520, "total_calories": 2600,
                            "equivalent_walking_distance": 7200,
                            "high_activity_time": 720, "medium_activity_time": 1800,
                            "low_activity_time": 7200}],
        "sleep": [{"day": day, "total_sleep_duration": 26640, "deep_sleep_duration": 5400,
                   "rem_sleep_duration": 6300, "light_sleep_duration": 14940,
                   "efficiency": 91, "average_breath": 14.2, "average_hrv": 62,
                   "lowest_heart_rate": 49, "average_heart_rate": 56}],
        "daily_spo2": [{"day": day, "spo2_percentage": {"average": 96.4}}],
        "daily_stress": [{"day": day, "stress_high": 5400, "day_summary": "normal"}],
        "daily_resilience": [{"day": day, "level": "solid"}],
        "daily_cardiovascular_age": [{"day": day, "vascular_age": 31}],
        "vO2_max": [{"day": day, "vo2_max": 44.7}],
        "sleep_time": [{"day": day, "recommendation": "earlier_bedtime", "status": "optimal"}],
        "workout": [
            {"id": "w-1", "day": day, "activity": "calisthenics", "calories": 310,
             "distance": 0, "intensity": "moderate",
             "start_datetime": f"{day}T18:20:00+03:00", "end_datetime": f"{day}T19:05:00+03:00"},
            {"id": "w-2", "day": day, "activity": "walking", "calories": 90,
             "distance": 2400, "intensity": "easy",
             "start_datetime": f"{day}T08:00:00+03:00", "end_datetime": f"{day}T08:30:00+03:00"},
        ],
    }


async def _run_oura_fetch(uid: int, payloads: dict) -> int:
    """Прогон fetch_and_store с подменённым HTTP-слоем — наружу ничего не уходит."""
    async def fake_get(session, token, path, params):
        return payloads.get(path, [])

    async def fake_token(_uid):
        return "test-token"

    saved_get, saved_token = oura_mod._get, oura_mod._valid_token
    oura_mod._get, oura_mod._valid_token = fake_get, fake_token
    try:
        return await oura_mod.fetch_and_store(uid, days=1)
    finally:
        oura_mod._get, oura_mod._valid_token = saved_get, saved_token


def test_oura_full_fetch():
    got = asyncio.run(_run_oura_fetch(OURA_UID, _oura_payloads(TODAY)))
    eq(got, 1, "данные за день сохранены")

    row = db.oura_range(OURA_UID, [TODAY])[TODAY]
    eq(row["readiness"], 82, "готовность")
    eq(row["sleep_score"], 77, "оценка сна")
    eq(row["sleep_h"], 7.4, "длительность сна из секунд")
    eq(row["deep_h"], 1.5, "глубокий сон")
    eq(row["rem_h"], 1.8, "REM-фаза")
    eq(row["light_h"], 4.2, "лёгкий сон")
    eq(row["sleep_efficiency"], 91, "эффективность сна")
    eq(row["breath_avg"], 14.2, "частота дыхания")
    eq(row["hrv"], 62, "HRV")
    eq(row["resting_hr"], 49, "пульс покоя — минимальный за ночь")
    eq(row["activity_score"], 88, "оценка активности")
    eq(row["steps"], 9400, "шаги")
    eq(row["active_kcal"], 520, "активные калории")
    eq(row["total_kcal"], 2600, "калории всего")
    eq(row["distance_m"], 7200, "дистанция")
    eq(row["active_min"], 162, "минуты активности суммируются")
    eq(row["spo2_avg"], 96.4, "SpO2")
    eq(row["stress_high_min"], 90, "высокий стресс из секунд в минуты")
    eq(row["stress_summary"], "normal", "итог дня по стрессу")
    eq(row["resilience"], "solid", "устойчивость")
    eq(row["cardio_age"], 31, "кардио-возраст")
    eq(row["vo2_max"], 44.7, "VO2max")
    ok("sleep_time" in (row["extra_json"] or ""), "необработанное поле легло в extra_json")

    wos = db.oura_workouts_for_date(OURA_UID, TODAY)
    eq(len(wos), 2, "обе тренировки кольца сохранены")
    eq(wos[0]["calories"], 310, "самая «дорогая» тренировка первой")
    eq(wos[0]["intensity"], "moderate", "интенсивность сохранена")

    asyncio.run(_run_oura_fetch(OURA_UID, _oura_payloads(TODAY)))
    eq(len(db.oura_workouts_for_date(OURA_UID, TODAY)), 2,
       "повторный забор не плодит копии (дедуп по oura_id)")
    eq(len(db.oura_workouts_range(OURA_UID, [TODAY, day(1)])), 2, "выборка за окно")
    eq(db.oura_workouts_range(OURA_UID, []), [], "пустое окно — пусто")


def test_oura_partial_fetch():
    """Премиальных эндпоинтов может не быть — забор всё равно должен пройти."""
    uid = 9011
    got = asyncio.run(_run_oura_fetch(uid, {"daily_readiness": [{"day": TODAY, "score": 70}]}))
    eq(got, 1, "день сохранён даже без остальных источников")
    row = db.oura_range(uid, [TODAY])[TODAY]
    eq(row["readiness"], 70, "то, что пришло, сохранено")
    eq(row["spo2_avg"], None, "чего нет в тарифе — осталось пустым")
    eq(row["vo2_max"], None, "VO2max пустой")
    eq(row["sleep_h"], None, "сна нет — поле пустое")
    eq(db.oura_workouts_for_date(uid, TODAY), [], "тренировок кольца нет")

    empty = asyncio.run(_run_oura_fetch(9012, {}))
    eq(empty, 0, "совсем пустой ответ не роняет забор")


def test_oura_autofill():
    import bot as botmod

    db.save_oura_tokens(AUTO_UID, "a", "r", 9_000_000_000.0)
    db.upsert_oura_workout(AUTO_UID, {
        "oura_id": "auto-1", "day": TODAY, "activity": "calisthenics",
        "intensity": "moderate", "calories": 310, "distance": 0,
        "start": f"{TODAY}T18:20:00+03:00", "end": f"{TODAY}T19:05:00+03:00"})

    cand = botmod._oura_candidate(AUTO_UID, TODAY)
    ok(cand is not None, "кандидат из кольца найден")
    eq(cand["time"], "18:20", "время подставляется из кольца")
    eq(cand["minutes"], 45, "длительность считается из интервала")
    eq(cand["calories"], 310, "расход берётся из кольца")

    db.add_workout_log(AUTO_UID, TODAY, "18:20", "done", note="train",
                       duration_min=45, kcal_burned=310, kcal_source="oura")
    ok(botmod._oura_candidate(AUTO_UID, TODAY) is None,
       "уже записанную тренировку повторно не предлагаем")
    ok(botmod._oura_candidate(WO_UID, TODAY) is None, "без кольца кандидата нет")

    labels = [b.text for row in botmod.OURA_TRAIN_KB.inline_keyboard for b in row]
    ok(any("Записать её" in x for x in labels), "есть кнопка подтверждения из кольца")
    ok(any("вручную" in x for x in labels), "есть кнопка ручного ввода")
    ok(any("Не тренировался" in x for x in labels), "есть кнопка пропуска")

    eq(oura_mod.workout_duration_min({"start": None, "end": None}), None,
       "без времени длительность не считаем")
    eq(oura_mod.workout_time({}), None, "без начала времени нет")


def test_oura_in_cabinet():
    db.ensure_user(OURA_UID, "Клиент с кольцом")
    o = web_dashboard.client_detail(OURA_UID, days=7)["oura"]
    ok(o is not None, "блок кольца собран для кабинета")
    eq(o["avg"]["sleep_efficiency"], 91, "эффективность сна в средних")
    eq(o["avg"]["spo2_avg"], 96.4, "SpO2 в средних")
    eq(o["avg"]["active_kcal"], 520, "активные калории в средних")
    eq(o["avg"]["deep_h"], 1.5, "фазы сна в средних")
    eq(o["avg"]["vo2_max"], 44.7, "VO2max в средних")
    eq(o["latest"]["resilience"], "solid", "устойчивость в последнем дне")
    eq(o["latest"]["stress_summary"], "normal", "итог по стрессу в последнем дне")
    eq(len(o["workouts"]), 2, "тренировки кольца доехали до тренера")
    eq(o["workouts"][0]["activity"], "calisthenics", "тип тренировки")


def test_workout_kcal_estimate():
    eq(nutrition.workout_kcal_estimate(80, 40), 320, "MET 6 × 80 кг × 40 мин → 320 ккал")
    eq(nutrition.workout_kcal_estimate(None, 40), 300, "без веса берём 75 кг → 300 ккал")
    eq(nutrition.workout_kcal_estimate(0, 40), 300, "нулевой вес — тоже запасные 75 кг")
    eq(nutrition.workout_kcal_estimate(80, 60), 480, "час при 80 кг → 480 ккал")
    eq(nutrition.workout_kcal_estimate(60, 30), 180, "полчаса при 60 кг → 180 ккал")
    ok(nutrition.workout_kcal_estimate(80, 40) > nutrition.workout_kcal_estimate(60, 40),
       "тяжелее клиент — больше расход")


def test_oura_workout_match():
    rows = [
        {"activity": "calisthenics", "calories": 310,
         "start": "2026-08-27T18:20:00+03:00", "end": "2026-08-27T19:05:00+03:00"},
        {"activity": "walking", "calories": 90,
         "start": "2026-08-27T08:00:00+03:00", "end": "2026-08-27T08:30:00+03:00"},
    ]
    hit = oura_mod.match_workout(rows, "18:30", 40)
    eq(hit["calories"], 310, "берём тренировку, пересекающуюся по времени")
    eq(oura_mod.match_workout(rows, "08:10", 30)["calories"], 90, "утренняя выбирается утром")
    eq(oura_mod.match_workout(rows, "12:00", 30), None, "нет пересечения — нет совпадения")
    eq(oura_mod.match_workout([], "18:30", 40), None, "пустой список — None")
    eq(oura_mod.match_workout(rows, "чепуха", 40), None, "кривое время — None")
    eq(oura_mod.match_workout([{"calories": 100, "start": None, "end": None}], "18:30", 40), None,
       "запись без времени пропускается")


async def _kcal_with_oura():
    import bot as botmod

    async def fake_fetch(uid, date):
        return [
            {"activity": "calisthenics", "calories": 310,
             "start": f"{date}T18:20:00+03:00", "end": f"{date}T19:05:00+03:00"},
            {"activity": "walking", "calories": 90,
             "start": f"{date}T08:00:00+03:00", "end": f"{date}T08:30:00+03:00"},
        ]

    saved_fetch, saved_flag = botmod.oura_mod.fetch_workouts, config.OURA_ENABLED
    botmod.oura_mod.fetch_workouts = fake_fetch
    config.OURA_ENABLED = True
    try:
        return await botmod._workout_kcal(UID, TODAY, "18:30", 40)
    finally:
        botmod.oura_mod.fetch_workouts = saved_fetch
        config.OURA_ENABLED = saved_flag


async def _kcal_without_oura():
    import bot as botmod
    saved = config.OURA_ENABLED
    config.OURA_ENABLED = False
    try:
        return await botmod._workout_kcal(WO_UID, TODAY, "18:30", 40)
    finally:
        config.OURA_ENABLED = saved


def test_workout_kcal_source():
    import bot as botmod

    ok(db.oura_connected(UID), "у владельца кольцо подключено (нужно для теста)")
    kcal, source = asyncio.run(_kcal_with_oura())
    eq(source, "oura", "при совпадении с тренировкой кольца источник — oura")
    eq(kcal, 310, "берём число расхода из кольца")

    kcal, source = asyncio.run(_kcal_without_oura())
    eq(source, "estimate", "без кольца источник — оценка")
    eq(kcal, 300, "оценка считается по весу и времени (75 кг по умолчанию)")

    # Кольцо есть, но подходящей тренировки в нём нет — падаем в оценку
    async def empty_fetch(uid, date):
        return []

    saved_fetch, saved_flag = botmod.oura_mod.fetch_workouts, config.OURA_ENABLED
    botmod.oura_mod.fetch_workouts = empty_fetch
    config.OURA_ENABLED = True
    try:
        kcal, source = asyncio.run(botmod._workout_kcal(UID, TODAY, "18:30", 40))
    finally:
        botmod.oura_mod.fetch_workouts = saved_fetch
        config.OURA_ENABLED = saved_flag
    eq(source, "estimate", "нет совпадения в кольце — оценка")
    eq(kcal, 320, "оценка по весу владельца (80 кг)")

    db.add_workout_log(WO_UID, day(3), "07:00", "done", note="train", duration_min=30,
                       description="брусья", kcal_burned=225, kcal_source="estimate")
    w = db.workout_for_date(WO_UID, day(3))
    eq(w["kcal_burned"], 225, "расход сохранён в базе")
    eq(w["kcal_source"], "estimate", "источник расхода сохранён в базе")


def test_workouts_in_cabinet():
    db.add_workout_log(CLIENT_UID, day(2), "07:30", "done", note="train",
                       duration_min=55, description="брусья и пресс",
                       kcal_burned=330, kcal_source="estimate")
    d = web_dashboard.client_detail(CLIENT_UID, days=7)
    w = d["workouts"]
    ok(w is not None, "блок тренировок в карточке клиента")
    eq(w["done"], 2, "две сделанные тренировки за неделю")
    eq(w["minutes"], 55, "суммарные минуты — по заполненным записям")

    byd = {x["date"]: x for x in w["list"]}
    eq(byd[day(2)]["duration_min"], 55, "длительность доехала до тренера")
    eq(byd[day(2)]["description"], "брусья и пресс", "описание доехало до тренера")
    eq(byd[day(2)]["time"], "07:30", "время доехало до тренера")

    txt = web_dashboard.week_data_text(CLIENT_UID)
    eq(w["kcal_burned"], 330, "суммарный расход за окно")
    eq(byd[day(2)]["kcal_burned"], 330, "расход по тренировке доехал до тренера")
    eq(byd[day(2)]["kcal_source"], "estimate", "источник числа доехал до тренера")

    ok("55 мин" in txt, "в брифе тренеру есть длительность тренировки")
    ok("брусья и пресс" in txt, "в брифе тренеру есть описание тренировки")
    ok("расход ~330 ккал (оценка)" in txt, "в брифе расход помечен как расход и как оценка")
    ok("330" not in str(d["targets"]["kcal"]), "расход не подмешан в цели по еде")

    empty = web_dashboard.client_detail(PLAN_UID, days=7)["workouts"]
    ok(empty is None, "у клиента без тренировок блока нет")


def test_ai_tips_toggle():
    """Тренер может вернуть боту право советовать клиенту — флагом coaches.ai_tips."""
    import bot as botmod
    _fake_coach_bot(botmod)
    coach = botmod.COACH_BY_BOT[FAKE_COACH_BOT]

    data = {
        "dish": "Круассан", "confidence": "высокая", "total_grams": 120,
        "total_kcal": 420, "total_protein_g": 9, "total_fat_g": 24, "total_carbs_g": 42,
        "chek_score": 3, "chek_verdict": "сильно обработанная выпечка",
        "chek_tip": "Замени на цельнозерновой", "items": [],
    }

    coach["ai_tips"] = 0
    eq(botmod.ai_tips_on(FAKE_COACH_BOT), False, "по умолчанию флаг выключен")
    eq(botmod.show_advice(FAKE_COACH_BOT), False, "по умолчанию бот тренера не советует")
    eq(botmod.show_advice(123456), True, "в личном боте советы доступны всегда")
    off = botmod.fmt_meal_reply(data, [], None, advice=False)
    ok("💡" not in off and "обработанная выпечка" not in off, "без флага советов нет")

    coach["ai_tips"] = 1
    eq(botmod.ai_tips_on(FAKE_COACH_BOT), True, "флаг включён")
    eq(botmod.show_advice(FAKE_COACH_BOT), True, "с флагом бот тренера снова советует")

    on = botmod.fmt_meal_reply(data, [], None, advice=True)
    ok("По Чеку: 3/10" in on, "балл Чека остаётся")
    ok("сильно обработанная выпечка" in on, "с флагом вердикт возвращается")
    ok("💡 Замени на цельнозерновой" in on, "с флагом совет возвращается")

    ok("/labreport" in botmod.labs_overview_text(LABS_UID, advice=True),
       "с флагом в /labs возвращается разбор")
    ok("/labreport" not in botmod.labs_overview_text(LABS_UID, advice=False),
       "без флага разбора в /labs нет")

    labels = [b.text for row in botmod.suppl_kb(LABS_UID, TODAY, client=False).inline_keyboard
              for b in row]
    ok(any("Совместимость" in x for x in labels), "с флагом кнопка совместимости есть")
    labels_off = [b.text for row in botmod.suppl_kb(LABS_UID, TODAY, client=True).inline_keyboard
                  for b in row]
    ok(not any("Совместимость" in x for x in labels_off), "без флага кнопки нет")

    with_verdict = botmod.build_day_overview(CLIENT_UID, TODAY, advice=True)
    without = botmod.build_day_overview(CLIENT_UID, TODAY, advice=False)
    ok("Еда по Чеку" in with_verdict and "Еда по Чеку" in without, "балл дня есть в обоих режимах")
    ok(len(with_verdict) > len(without), "с флагом к баллу добавляется словесная оценка")

    coach["ai_tips"] = 0    # возвращаем принцип по умолчанию


def test_ai_tips_migration():
    import sqlite3

    def coach_columns():
        con = sqlite3.connect(os.environ["DB_PATH"])
        try:
            return [r[1] for r in con.execute("PRAGMA table_info(coaches)")]
        finally:
            con.close()

    cols = coach_columns()
    eq(cols.count("ai_tips"), 1, "колонка ai_tips заведена ровно один раз")
    db.init_db()
    db.init_db()
    eq(coach_columns(), cols, "повторная миграция не меняет схему coaches")

    cid = db.list_coaches()[0]["id"]
    eq(db.coach_by_id(cid)["ai_tips"], 0, "у существующих тренеров подсказки выключены")
    db.update_coach(cid, ai_tips=1)
    eq(db.coach_by_id(cid)["ai_tips"], 1, "флаг сохраняется в базе")
    db.update_coach(cid, ai_tips=0)
    eq(db.coach_by_id(cid)["ai_tips"], 0, "и выключается обратно")


def test_coach_still_gets_advice():
    ok("ЧЕРНОВИК СООБЩЕНИЯ КЛИЕНТУ" in analyzer.BRIEF_SYSTEM,
       "в брифе тренеру остался черновик сообщения клиенту")
    ok("НА ЧТО ОБРАТИТЬ ВНИМАНИЕ" in analyzer.BRIEF_SYSTEM,
       "в брифе тренеру остались пункты внимания")
    ok(not hasattr(analyzer, "generate_workout"),
       "бот больше не придумывает тренировки — их задаёт тренер")
    ok(not hasattr(analyzer, "WORKOUT_SYSTEM"), "промпт генерации тренировок удалён")

    captured = {}

    async def fake_text(system, user):
        captured["system"] = system
        captured["user"] = user
        return "📊 ЧТО ПРОИСХОДИТ\nвсё ок\n💬 ЧЕРНОВИК СООБЩЕНИЯ КЛИЕНТУ\nмолодец"

    async def run():
        saved = analyzer.generate_text
        analyzer.generate_text = fake_text
        try:
            return await analyzer.generate_brief("Даша", "данные недели", {"name": "Николай"})
        finally:
            analyzer.generate_text = saved

    out = asyncio.run(run())
    eq(captured["system"], analyzer.BRIEF_SYSTEM, "бриф собирается по системному промпту тренера")
    ok("Николай" in captured["user"], "имя тренера уходит в бриф")
    ok("данные недели" in captured["user"], "данные клиента уходят в бриф")
    ok("ЧЕРНОВИК" in out, "бриф возвращается тренеру целиком")


# ------------------------------------------------- голосовой ввод и анонсы

def test_voice_routing():
    """Голос идёт тем же путём, что и текст; модель при этом не грузим."""
    import bot as botmod
    import stt

    names = [h.callback.__name__ for h in botmod.router.message.handlers]
    ok("on_voice" in names, "обработчик голосовых зарегистрирован")
    ok(names.index("on_voice") < names.index("lw_description"),
       "голос перехватывается раньше шагов диалога — иначе шаг проглотил бы его")
    ok(names.index("cmd_cancel") < names.index("on_voice"), "/cancel по-прежнему первый")

    saved = config.VOICE_ENABLED
    try:
        config.VOICE_ENABLED = False
        eq(stt.enabled(), False, "выключенный голосовой ввод виден модулю")
        raises(stt.SttUnavailable, lambda: stt.transcribe(b"123"),
               "с выключенным флагом распознавание не запускается")
        config.VOICE_ENABLED = True
        eq(stt.enabled(), True, "включённый флаг виден модулю")
        eq(stt.transcribe(b""), "", "пустое аудио — пустой текст, без загрузки модели")
    finally:
        config.VOICE_ENABLED = saved

    eq(stt._model, None, "модель так и не загружалась в тестах")


async def _voice_to(state_name, transcript):
    """Прогоняем route_transcript и смотрим, куда ушёл распознанный текст."""
    import bot as botmod
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=VOICE_UID, user_id=VOICE_UID)
    state = FSMContext(storage=storage, key=key)

    msg = _FakeMessage(VOICE_UID, 1)
    if state_name is not None:
        await state.set_state(state_name)
        await state.set_data({"date": TODAY, "time": "19:00", "duration": 35,
                              "kcal": 260, "kcal_source": "estimate"})

    routed = {}

    async def fake_analyze(message, **kw):
        routed["food"] = kw.get("text")

    saved = botmod.analyze_and_reply
    botmod.analyze_and_reply = fake_analyze
    try:
        await botmod.route_transcript(msg, state, transcript)
    finally:
        botmod.analyze_and_reply = saved
    return routed, msg.sent


def test_voice_goes_to_food():
    db.ensure_user(VOICE_UID, "Голосовой")
    routed, _ = asyncio.run(_voice_to(None, "съел овсянку с ягодами и кофе"))
    eq(routed.get("food"), "съел овсянку с ягодами и кофе",
       "вне диалогов распознанное уходит в анализ еды")


def test_voice_goes_to_workout_description():
    import bot as botmod
    routed, sent = asyncio.run(
        _voice_to(botmod.LogWorkout.description, "турник, пять подходов по восемь"))
    ok("food" not in routed, "в шаге описания тренировки в еду не уходит")
    w = db.workout_for_date(VOICE_UID, TODAY)
    ok(w is not None, "тренировка записана")
    eq(w["description"], "турник, пять подходов по восемь",
       "распознанное легло в описание тренировки")
    eq(w["duration_min"], 35, "длительность из диалога сохранилась")
    ok(any("Записал тренировку" in s for s in sent), "клиент получил подтверждение")


def test_voice_inside_other_dialog():
    import bot as botmod
    routed, sent = asyncio.run(_voice_to(botmod.Profile.age, "тридцать"))
    ok("food" not in routed, "в анкете профиля голос не уходит в еду")
    ok(any("ответь, пожалуйста, текстом" in s for s in sent),
       "в остальных диалогах просим ответить текстом")


def test_announce_plan():
    import announce

    targets = announce.plan("привет")
    by_uid = {t["uid"]: t for t in targets}

    ok(CLIENT_UID in by_uid, "клиент с согласием получает анонс")
    ok(by_uid[CLIENT_UID]["via"].startswith("@"), "клиенту пишет бот его тренера")
    ok("Чек" not in by_uid[CLIENT_UID]["text"], "текст клиенту нейтральный")

    no_consent = 12121
    coach_id = db.list_coaches()[0]["id"]
    db.ensure_user(no_consent, "Без согласия", coach_id=coach_id)
    db.update_user(no_consent, consent=0)
    ok(no_consent not in {t["uid"] for t in announce.plan("привет")},
       "без согласия анонс не уходит")

    ok(all(t["token"] for t in targets), "у каждого получателя есть бот-отправитель")
    own = announce.plan("клиентам", owner_text="владельцу")
    for t in own:
        if t["via"] == "личный бот":
            eq(t["text"], "владельцу", "владельцу уходит свой текст")


async def _announce_run():
    import announce
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

    seen, tries = [], {"n": 0}

    async def sender(token, uid, text):
        seen.append(uid)
        if uid == CLIENT_UID:
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        if uid == VOICE_UID and tries["n"] == 0:
            tries["n"] += 1
            raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
        return None

    return await announce.run("текст", sender=sender, pause=0)


def test_announce_send():
    import announce

    eq(announce.run.__module__, "announce", "рассылка живёт отдельным скриптом")
    stats = asyncio.run(_announce_run())
    ok(stats["planned"] > 0, "получатели найдены")
    eq(stats["blocked"], 1, "заблокировавший бота пропущен, а не сломал рассылку")
    ok(stats["sent"] >= 1, "остальным отправлено")
    eq(stats["failed"], 0, "RetryAfter пережит повторной попыткой")

    dry = asyncio.run(announce.run("текст", dry_run=True))
    eq(dry["sent"], 0, "dry-run никому не пишет")
    ok(dry["planned"] > 0, "но получателей считает")

    import bot as botmod
    src = open(botmod.__file__, encoding="utf-8").read()
    ok("announce" not in src, "бот не знает про рассылку — она не запустится сама на рестарте")


# ------------------------------------------------- маршрутизация команд в диалогах


async def _routing():
    """Кто из обработчиков перехватит сообщение — без реальных вызовов Telegram.

    Проверяем не порядок ради порядка, а фактический выбор: aiogram отдаёт
    сообщение первому обработчику, чьи фильтры прошли.
    """
    from datetime import timezone

    from aiogram import Bot
    from aiogram.types import Chat, Message, User

    import bot as botmod

    fake_bot = Bot("123456789:AAHnTESTtokenTESTtokenTESTtoken1234")

    def msg(text: str) -> Message:
        return Message(message_id=1, date=datetime.now(timezone.utc),
                       chat=Chat(id=1, type="private"),
                       from_user=User(id=1, is_bot=False, first_name="Тест"),
                       text=text).as_(fake_bot)

    async def who(text: str, raw_state):
        m = msg(text)
        for h in botmod.router.message.handlers:
            try:
                res = await h.check(m, bot=fake_bot, raw_state=raw_state, state=None,
                                    event_from_user=m.from_user)
            except Exception:  # noqa: BLE001
                continue
            passed = res[0] if isinstance(res, tuple) else res
            if passed:
                return h.callback.__name__
        return None

    try:
        eq(await who("/cancel", "NewCoach:token"), "cmd_cancel",
           "/cancel выходит из шага «токен» диалога /newcoach")
        eq(await who("/cancel", "NewCoach:name"), "cmd_cancel",
           "/cancel выходит из шага «имя»")
        eq(await who("/cancel", "NewCoach:brand"), "cmd_cancel",
           "/cancel выходит из шага «бренд»")
        eq(await who("/cancel", "AddSuppl:name"), "cmd_cancel",
           "/cancel выходит и из диалога добавления БАДа")
        eq(await who("/cancel", "Profile:age"), "cmd_cancel",
           "/cancel выходит и из анкеты профиля")
        eq(await who("/cancel", None), "cmd_cancel",
           "/cancel вне диалога тоже обрабатывается (ответит «нечего отменять»)")

        eq(await who("/labs", "NewCoach:token"), "nc_ignore_commands",
           "/labs в диалоге не считается вводом токена")
        eq(await who("/water", "NewCoach:name"), "nc_ignore_commands",
           "/water в диалоге не считается именем тренера")
        eq(await who("/today", "NewCoach:brand"), "nc_ignore_commands",
           "/today в диалоге не считается брендом")

        eq(await who("123456789:AAEtokenlike", "NewCoach:token"), "nc_token",
           "обычный текст по-прежнему доходит до шага «токен»")
        eq(await who("Анна", "NewCoach:name"), "nc_name",
           "обычный текст доходит до шага «имя»")
        eq(await who("Анна Фит", "NewCoach:brand"), "nc_brand",
           "обычный текст доходит до шага «бренд»")

        eq(await who("/labs", None), "cmd_labs", "вне диалога /labs работает как обычно")

        names = [h.callback.__name__ for h in botmod.router.message.handlers]
        eq(names.index("cmd_cancel"), 0, "/cancel зарегистрирован самым первым")
        ok(names.index("nc_ignore_commands") < names.index("nc_token"),
           "заслон от команд стоит раньше шагов диалога")
    finally:
        await fake_bot.session.close()


def test_routing():
    asyncio.run(_routing())


# ---------------------------------------------------------------- запуск


def main() -> int:
    print(f"Временная база: {os.environ['DB_PATH']}")
    db.init_db()

    print("- db: пользователи и настройки")
    test_users()
    print("- db: режим WAL")
    test_wal_mode()
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
    print("- db: поздняя привязка к тренеру")
    test_late_join_coach()
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
    print("- БАДы: план приёма")
    test_supplement_plan()
    print("- БАДы: адгеренс за период")
    test_adherence_payload()
    print("- миграция plan_days_per_week идемпотентна")
    test_migration_idempotent()
    print("- период: 1 / 7 / 30 / 365 / all")
    test_resolve_days()
    print("- длинные окна: недельные бакеты")
    test_weekly_buckets()
    print("- кабинет: что ел, состав приёма")
    test_food_breakdown()
    print("- бот: шаг плана приёма БАДов")
    test_suppl_plan_in_bot()
    print("- анализы: только бланки в окне")
    test_labs_window()

    print("- analyzer: извлечение JSON")
    test_extract_json()
    print("- analyzer: нормализация")
    test_normalize()
    print("- analyzer: разбор еды (моки)")
    test_analyzer_meals()
    print("- клиент: ответ по еде без советов")
    test_client_meal_reply()
    print("- клиент: сводка дня без оценок")
    test_client_day_overview()
    print("- клиент: подтверждение анализов нейтральное")
    test_client_labs_confirmation()
    print("- онбординг: приветствие и /help со всеми функциями")
    test_onboarding_lists_features()
    print("- тренировки: лог вместо генерации")
    test_workout_log()
    print("- Oura: полный забор всех эндпоинтов")
    test_oura_full_fetch()
    print("- Oura: пустые премиум-эндпоинты не роняют забор")
    test_oura_partial_fetch()
    print("- Oura: автозаполнение тренировки")
    test_oura_autofill()
    print("- Oura: расширенный блок тренеру")
    test_oura_in_cabinet()
    print("- тренировки: оценка расхода калорий")
    test_workout_kcal_estimate()
    print("- тренировки: совпадение с Oura")
    test_oura_workout_match()
    print("- тренировки: источник расхода (Oura / оценка)")
    test_workout_kcal_source()
    print("- тренировки: длительность и описание тренеру")
    test_workouts_in_cabinet()
    print("- переключатель /aitips")
    test_ai_tips_toggle()
    print("- миграция ai_tips идемпотентна")
    test_ai_tips_migration()
    print("- тренер: бриф по-прежнему советует")
    test_coach_still_gets_advice()
    print("- голос: маршрутизация и выключенный флаг")
    test_voice_routing()
    print("- голос: вне диалогов уходит в еду")
    test_voice_goes_to_food()
    print("- голос: в шаге описания тренировки")
    test_voice_goes_to_workout_description()
    print("- голос: внутри других диалогов")
    test_voice_inside_other_dialog()
    print("- анонс: кому уходит")
    test_announce_plan()
    print("- анонс: рассылка, блокировки, лимиты")
    test_announce_send()
    print("- bot: /cancel и команды внутри диалогов")
    test_routing()

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
