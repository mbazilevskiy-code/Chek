"""SQLite-дневник питания, воды, привычек и тренировок."""
import json
import secrets
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    name           TEXT,
    sex            TEXT,
    age            INTEGER,
    height_cm      REAL,
    weight_kg      REAL,
    activity       TEXT,
    goal           TEXT,
    kcal_target    INTEGER,
    protein_target INTEGER,
    fat_target     INTEGER,
    carb_target    INTEGER,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    date         TEXT NOT NULL,   -- YYYY-MM-DD (местное время компьютера)
    time         TEXT NOT NULL,   -- HH:MM
    source       TEXT,            -- photo | text
    dish         TEXT,
    grams        REAL,
    kcal         REAL,
    protein     REAL,
    fat          REAL,
    carbs        REAL,
    chek_score   INTEGER,
    chek_verdict TEXT,
    raw_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_meals_user_date ON meals(user_id, date);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS water_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date    TEXT NOT NULL,
    time    TEXT NOT NULL,
    ml      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_water_user_date ON water_log(user_id, date);

CREATE TABLE IF NOT EXISTS habit_log (
    user_id INTEGER NOT NULL,
    date    TEXT NOT NULL,
    key     TEXT NOT NULL,      -- workingin, ...
    value   INTEGER DEFAULT 1,
    PRIMARY KEY (user_id, date, key)
);

CREATE TABLE IF NOT EXISTS workout_plan (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    dow     INTEGER NOT NULL,   -- 0=Пн ... 6=Вс
    time    TEXT NOT NULL       -- HH:MM
);

CREATE TABLE IF NOT EXISTS supplements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    timing     TEXT,               -- утром | днём | вечером | на ночь | с едой
    active     INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_suppl_user ON supplements(user_id);

CREATE TABLE IF NOT EXISTS supplement_log (
    user_id INTEGER NOT NULL,
    date    TEXT NOT NULL,
    suppl_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, date, suppl_id)
);

CREATE TABLE IF NOT EXISTS wellbeing (
    user_id INTEGER NOT NULL,
    date    TEXT NOT NULL,
    energy  INTEGER,
    mood    INTEGER,
    stress  INTEGER,
    libido  INTEGER,
    note    TEXT,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS lab_results (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    date      TEXT NOT NULL,      -- дата анализа (или загрузки)
    panel     TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lab_user ON lab_results(user_id, date);

CREATE TABLE IF NOT EXISTS lab_markers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    date      TEXT NOT NULL,
    name      TEXT NOT NULL,
    value     REAL,
    value_text TEXT,
    unit      TEXT,
    ref_low   REAL,
    ref_high  REAL,
    flag      TEXT               -- низко | норма | высоко
);
CREATE INDEX IF NOT EXISTS idx_marker_user ON lab_markers(user_id, name, date);

CREATE TABLE IF NOT EXISTS oura_tokens (
    user_id      INTEGER PRIMARY KEY,
    access_token TEXT,
    refresh_token TEXT,
    expires_at   REAL
);

CREATE TABLE IF NOT EXISTS oura_daily (
    user_id        INTEGER NOT NULL,
    date           TEXT NOT NULL,
    readiness      INTEGER,
    sleep_score    INTEGER,
    sleep_h        REAL,
    hrv            REAL,
    resting_hr     REAL,
    temp_dev       REAL,
    activity_score INTEGER,
    steps          INTEGER,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS oura_workouts (
    oura_id   TEXT PRIMARY KEY,        -- id из Oura: по нему дедуп
    user_id   INTEGER NOT NULL,
    day       TEXT NOT NULL,
    start_dt  TEXT,
    end_dt    TEXT,
    activity  TEXT,
    intensity TEXT,
    calories  INTEGER,
    distance  REAL
);
CREATE INDEX IF NOT EXISTS idx_oura_wo_user_day ON oura_workouts(user_id, day);

CREATE TABLE IF NOT EXISTS whoop_tokens (
    user_id      INTEGER PRIMARY KEY,
    access_token TEXT,
    refresh_token TEXT,
    expires_at   REAL
);

CREATE TABLE IF NOT EXISTS whoop_daily (
    user_id        INTEGER NOT NULL,
    date           TEXT NOT NULL,
    recovery       INTEGER,        -- % восстановления
    hrv            REAL,           -- мс
    resting_hr     REAL,
    spo2           REAL,
    skin_temp      REAL,
    sleep_h        REAL,
    sleep_perf     INTEGER,        -- % эффективности сна
    deep_h         REAL,
    rem_h          REAL,
    light_h        REAL,
    awake_h        REAL,
    breath_avg     REAL,
    strain         REAL,           -- дневной strain 0..21
    day_kcal       INTEGER,        -- расход за сутки
    avg_hr         INTEGER,
    max_hr         INTEGER,
    extra_json     TEXT,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS whoop_workouts (
    whoop_id  TEXT PRIMARY KEY,     -- id записи WHOOP: по нему дедуп
    user_id   INTEGER NOT NULL,
    day       TEXT NOT NULL,
    start_dt  TEXT,
    end_dt    TEXT,
    sport     TEXT,
    strain    REAL,
    calories  INTEGER,
    distance  REAL,
    avg_hr    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_whoop_wo_user_day ON whoop_workouts(user_id, day);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,      -- water | supplement | workout | custom
    text       TEXT NOT NULL,      -- о чём напомнить, короткой фразой
    time       TEXT,               -- ЧЧ:ММ, если напоминание в конкретное время
    every_min  INTEGER,            -- либо интервал в минутах
    win_from   TEXT,               -- окно для интервального: ЧЧ:ММ
    win_to     TEXT,
    dow_mask   INTEGER,            -- биты Пн..Вс; NULL = каждый день
    enabled    INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders(user_id, enabled);

CREATE TABLE IF NOT EXISTS chat_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role    TEXT NOT NULL,          -- user | assistant
    content TEXT NOT NULL,
    ts      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id, id);

CREATE TABLE IF NOT EXISTS weight_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date    TEXT NOT NULL,
    kg      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_user ON weight_log(user_id, date);

CREATE TABLE IF NOT EXISTS trainer_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    date       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notes_user ON trainer_notes(user_id, date);

CREATE TABLE IF NOT EXISTS coaches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_token     TEXT UNIQUE NOT NULL,
    bot_id        INTEGER,            -- числовой id бота (первая часть токена)
    coach_user_id INTEGER,            -- Telegram ID тренера (первый, кто написал боту)
    name          TEXT,               -- имя тренера для клиентов
    brand         TEXT,               -- название сервиса/бота
    methodology   TEXT,               -- заметки о методике тренера для ИИ
    cabinet_token TEXT,               -- ключ веб-кабинета
    bot_username  TEXT,               -- @username бота (заполняется при старте)
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workout_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    date      TEXT NOT NULL,
    time      TEXT,
    status    TEXT,             -- done | skipped
    note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_workout_user_date ON workout_log(user_id, date);
"""

# Новые колонки users (миграция старых баз): имя -> определение.
_USER_COLUMNS = {
    "water_target_ml": "INTEGER",
    "evening_time": "TEXT DEFAULT '21:00'",
    "reminders_on": "INTEGER DEFAULT 1",
    "coach_id": "INTEGER",           # NULL = личный бот владельца
    "consent": "INTEGER DEFAULT 0",  # согласие клиента на доступ тренера
    "cabinet_token": "TEXT",         # ключ к личной страничке клиента (/me)
    "tz": "TEXT",                    # задел на будущее: пока всё по времени сервера
}

# Новые колонки supplements. План приёма задаёт сам клиент:
# 7 = каждый день, N = N раз в неделю.
_SUPPL_COLUMNS = {
    "plan_days_per_week": "INTEGER DEFAULT 7",
}

# Новые колонки workout_log: тренировку не генерируем, а фиксируем со слов клиента.
_WORKOUT_COLUMNS = {
    "duration_min": "INTEGER",
    "description": "TEXT",
    "kcal_burned": "INTEGER",
    "kcal_source": "TEXT",   # oura | estimate
}

# Новые колонки oura_daily: собираем с кольца максимум, что отдаёт API.
# Чего нет в тарифе — просто остаётся пустым; extra_json — задел на будущее.
_OURA_BASE_COLS = ["readiness", "sleep_score", "sleep_h", "hrv", "resting_hr",
                   "temp_dev", "activity_score", "steps"]
_OURA_COLUMNS = {
    "sleep_efficiency": "INTEGER",
    "breath_avg": "REAL",
    "deep_h": "REAL",
    "rem_h": "REAL",
    "light_h": "REAL",
    "spo2_avg": "REAL",
    "active_kcal": "INTEGER",
    "total_kcal": "INTEGER",
    "distance_m": "INTEGER",
    "active_min": "INTEGER",
    "stress_high_min": "INTEGER",
    "stress_summary": "TEXT",
    "resilience": "TEXT",
    "cardio_age": "INTEGER",
    "vo2_max": "REAL",
    "extra_json": "TEXT",
}

# Новая колонка wellbeing: сон со слов клиента (не путать с sleep_h из кольца).
_WELLBEING_COLUMNS = {
    "sleep_h": "REAL",
}

# Новые колонки coaches. По умолчанию бот тренера клиенту не советует
# (см. журнал решений в CLAUDE.md); тренер может включить подсказки себе.
_COACH_COLUMNS = {
    "ai_tips": "INTEGER DEFAULT 0",
}


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL — режим журнала, через который работает непрерывный бэкап (litestream).
    # Настройка живёт в самом файле базы, так что это идемпотентно и дёшево.
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def journal_mode() -> str:
    """Текущий режим журнала базы — для диагностики и тестов."""
    with _conn() as c:
        return str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        for col, ddl in _USER_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        # У всех, кто завёлся до появления личной странички, ключа нет — догенерим.
        for row in c.execute("SELECT user_id FROM users WHERE cabinet_token IS NULL "
                             "OR cabinet_token = ''").fetchall():
            c.execute("UPDATE users SET cabinet_token = ? WHERE user_id = ?",
                      (secrets.token_urlsafe(16), row["user_id"]))
        have = {r["name"] for r in c.execute("PRAGMA table_info(supplements)").fetchall()}
        for col, ddl in _SUPPL_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE supplements ADD COLUMN {col} {ddl}")
        have = {r["name"] for r in c.execute("PRAGMA table_info(coaches)").fetchall()}
        for col, ddl in _COACH_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE coaches ADD COLUMN {col} {ddl}")
        have = {r["name"] for r in c.execute("PRAGMA table_info(workout_log)").fetchall()}
        for col, ddl in _WORKOUT_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE workout_log ADD COLUMN {col} {ddl}")
        have = {r["name"] for r in c.execute("PRAGMA table_info(wellbeing)").fetchall()}
        for col, ddl in _WELLBEING_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE wellbeing ADD COLUMN {col} {ddl}")
        have = {r["name"] for r in c.execute("PRAGMA table_info(oura_daily)").fetchall()}
        for col, ddl in _OURA_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE oura_daily ADD COLUMN {col} {ddl}")


# ---------- настройки ----------

def get_setting(key: str):
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------- пользователи ----------

def ensure_user(user_id: int, name: str | None = None, coach_id: int | None = None) -> None:
    """Создаёт пользователя и привязывает к тренеру, если тренера у него ещё нет.

    Человек мог завестись раньше в другом боте «Чека» — тогда строка уже есть,
    и без этого он молча остаётся невидимым в кабинете тренера. Привязываем
    только свободных: у кого coach_id уже стоит, к другому тренеру не уводим.
    Согласие при этом сбрасываем — согласие одному тренеру не значит согласие
    другому, бот переспросит.
    """
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id, name, coach_id, cabinet_token) "
                  "VALUES(?, ?, ?, ?)",
                  (user_id, name, coach_id, secrets.token_urlsafe(16)))
        if name:
            c.execute("UPDATE users SET name = ? WHERE user_id = ?", (name, user_id))
        if coach_id is not None:
            c.execute("UPDATE users SET coach_id = ?, consent = 0 "
                      "WHERE user_id = ? AND coach_id IS NULL", (coach_id, user_id))


def get_user(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def update_user(user_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE users SET {cols} WHERE user_id = ?", (*fields.values(), user_id))


def user_by_cabinet_token(token: str) -> dict | None:
    """Клиент по ключу его личной странички."""
    if not token:
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE cabinet_token = ?", (token,)).fetchone()
        return dict(row) if row else None


def cabinet_token_for(user_id: int) -> str:
    """Ключ странички клиента; создаёт, если его почему-то нет."""
    user = get_user(user_id) or {}
    token = user.get("cabinet_token")
    if not token:
        token = secrets.token_urlsafe(16)
        update_user(user_id, cabinet_token=token)
    return token


def all_user_ids() -> list[int]:
    with _conn() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM users").fetchall()]


# Таблицы, по которым считаем «первую активность» клиента (режим «всё время»).
_ACTIVITY_TABLES = ("meals", "water_log", "wellbeing", "workout_log",
                    "supplement_log", "oura_daily", "lab_results")


def first_activity_date(user_id: int) -> str | None:
    """Самая ранняя дата любой активности клиента. None — данных нет вообще."""
    parts = " UNION ALL ".join(
        f"SELECT MIN(date) d FROM {t} WHERE user_id = ?" for t in _ACTIVITY_TABLES
    )
    with _conn() as c:
        row = c.execute(f"SELECT MIN(d) m FROM ({parts})",
                        tuple([user_id] * len(_ACTIVITY_TABLES))).fetchone()
    return row["m"] if row and row["m"] else None


# ---------- приёмы пищи ----------

def add_meal(
    user_id: int,
    date: str,
    time: str,
    source: str,
    dish: str,
    grams: float,
    kcal: float,
    protein: float,
    fat: float,
    carbs: float,
    chek_score: int,
    chek_verdict: str,
    raw: dict | None = None,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO meals(user_id, date, time, source, dish, grams, kcal, protein, fat, "
            "carbs, chek_score, chek_verdict, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, date, time, source, dish, grams, kcal, protein, fat, carbs,
                chek_score, chek_verdict,
                json.dumps(raw, ensure_ascii=False) if raw else None,
            ),
        )
        return cur.lastrowid


def meals_for_date(user_id: int, date: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM meals WHERE user_id = ? AND date = ? ORDER BY id",
            (user_id, date),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_last_meal(user_id: int, date: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM meals WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 1",
            (user_id, date),
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM meals WHERE id = ?", (row["id"],))
        return dict(row)


def meals_for_dates(user_id: int, dates: list[str]) -> list[dict]:
    """Все приёмы пищи за список дат (по убыванию даты, внутри дня по времени)."""
    if not dates:
        return []
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, time, dish, kcal, protein, fat, carbs, chek_score "
            f"FROM meals WHERE user_id = ? AND date IN ({marks}) "
            f"ORDER BY date DESC, id ASC",
            (user_id, *dates),
        ).fetchall()
        return [dict(r) for r in rows]


def meals_detailed(user_id: int, dates: list[str], limit: int = 300) -> list[dict]:
    """Приёмы пищи за окно с составом и вердиктом Чека — свежие первыми.

    raw_json отдаём как есть: в нём разбивка по компонентам, совет и допущения ИИ.
    """
    if not dates:
        return []
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT id, date, time, source, dish, grams, kcal, protein, fat, carbs, "
            f"chek_score, chek_verdict, raw_json FROM meals "
            f"WHERE user_id = ? AND date IN ({marks}) "
            f"ORDER BY date DESC, id DESC LIMIT ?",
            (user_id, *dates, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def meals_count(user_id: int, dates: list[str]) -> int:
    """Сколько всего приёмов пищи в окне (чтобы честно сказать про обрезку)."""
    if not dates:
        return 0
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        row = c.execute(
            f"SELECT COUNT(*) n FROM meals WHERE user_id = ? AND date IN ({marks})",
            (user_id, *dates),
        ).fetchone()
        return int(row["n"])


def totals_by_date(user_id: int, dates: list[str]) -> dict[str, dict]:
    """Суммы КБЖУ и средний балл Чека по каждой дате из списка."""
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, COUNT(*) n, SUM(kcal) kcal, SUM(protein) protein, SUM(fat) fat, "
            f"SUM(carbs) carbs, AVG(chek_score) chek "
            f"FROM meals WHERE user_id = ? AND date IN ({marks}) GROUP BY date",
            (user_id, *dates),
        ).fetchall()
        return {r["date"]: dict(r) for r in rows}


# ---------- тренеры ----------

def add_coach(bot_token: str, name: str, brand: str, cabinet_token: str) -> dict:
    bot_id = None
    head = bot_token.split(":")[0]
    if head.isdigit():
        bot_id = int(head)
    with _conn() as c:
        c.execute(
            "INSERT INTO coaches(bot_token, bot_id, name, brand, cabinet_token) "
            "VALUES(?,?,?,?,?)",
            (bot_token.strip(), bot_id, name, brand, cabinet_token),
        )
        row = c.execute("SELECT * FROM coaches WHERE bot_token = ?", (bot_token.strip(),)).fetchone()
        return dict(row)


def list_coaches() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM coaches ORDER BY id").fetchall()]


def coach_by_id(coach_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM coaches WHERE id = ?", (coach_id,)).fetchone()
        return dict(row) if row else None


def coach_by_cabinet_token(token: str) -> dict | None:
    if not token:
        return None
    with _conn() as c:
        row = c.execute("SELECT * FROM coaches WHERE cabinet_token = ?", (token,)).fetchone()
        return dict(row) if row else None


def set_coach_owner(coach_id: int, user_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE coaches SET coach_user_id = ? WHERE id = ?", (user_id, coach_id))


def update_coach(coach_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE coaches SET {cols} WHERE id = ?", (*fields.values(), coach_id))


def clients_of_coach(coach_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE coach_id = ? AND consent = 1 ORDER BY name",
            (coach_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- БАДы ----------

def add_supplement(user_id: int, name: str, timing: str, plan_days_per_week: int = 7) -> int:
    plan = max(1, min(7, int(plan_days_per_week or 7)))
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO supplements(user_id, name, timing, plan_days_per_week) VALUES(?,?,?,?)",
            (user_id, name.strip()[:80], timing, plan),
        )
        return cur.lastrowid


def list_supplements(user_id: int, only_active: bool = True) -> list[dict]:
    q = "SELECT * FROM supplements WHERE user_id = ?"
    if only_active:
        q += " AND active = 1"
    q += " ORDER BY id"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, (user_id,)).fetchall()]


def get_supplement(suppl_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM supplements WHERE id = ?", (suppl_id,)).fetchone()
        return dict(row) if row else None


def deactivate_supplement(user_id: int, suppl_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE supplements SET active = 0 WHERE id = ? AND user_id = ?",
                  (suppl_id, user_id))


def toggle_supplement_taken(user_id: int, date: str, suppl_id: int) -> bool:
    """Отмечает/снимает приём БАДа за дату. Возвращает новое состояние (принят?)."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM supplement_log WHERE user_id = ? AND date = ? AND suppl_id = ?",
            (user_id, date, suppl_id),
        ).fetchone()
        if row:
            c.execute("DELETE FROM supplement_log WHERE user_id = ? AND date = ? AND suppl_id = ?",
                      (user_id, date, suppl_id))
            return False
        c.execute("INSERT INTO supplement_log(user_id, date, suppl_id) VALUES(?,?,?)",
                  (user_id, date, suppl_id))
        return True


def taken_supplements(user_id: int, date: str) -> set[int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT suppl_id FROM supplement_log WHERE user_id = ? AND date = ?",
            (user_id, date),
        ).fetchall()
        return {r["suppl_id"] for r in rows}


def supplement_taken_dates(user_id: int, dates: list[str]) -> dict[int, list[str]]:
    """Даты приёма каждого БАДа внутри окна (по возрастанию)."""
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT DISTINCT suppl_id, date FROM supplement_log "
            f"WHERE user_id = ? AND date IN ({marks}) ORDER BY date",
            (user_id, *dates),
        ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["suppl_id"], []).append(r["date"])
    return out


def supplement_taken_days(user_id: int, dates: list[str]) -> dict[int, int]:
    """Сколько различных дней окна БАД был отмечен принятым."""
    return {sid: len(days) for sid, days in supplement_taken_dates(user_id, dates).items()}


# ---------- самочувствие ----------

def set_wellbeing(user_id: int, date: str, **fields) -> None:
    allowed = {"energy", "mood", "stress", "libido", "note", "sleep_h"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO wellbeing(user_id, date) VALUES(?, ?)", (user_id, date))
        cols = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE wellbeing SET {cols} WHERE user_id = ? AND date = ?",
                  (*fields.values(), user_id, date))


def get_wellbeing(user_id: int, date: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM wellbeing WHERE user_id = ? AND date = ?",
                        (user_id, date)).fetchone()
        return dict(row) if row else None


def wellbeing_range(user_id: int, dates: list[str]) -> dict[str, dict]:
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM wellbeing WHERE user_id = ? AND date IN ({marks})",
            (user_id, *dates),
        ).fetchall()
        return {r["date"]: dict(r) for r in rows}


# ---------- анализы ----------

def add_lab_result(user_id: int, date: str, panel: str, markers: list[dict]) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO lab_results(user_id, date, panel) VALUES(?,?,?)",
            (user_id, date, panel),
        )
        rid = cur.lastrowid
        for m in markers:
            c.execute(
                "INSERT INTO lab_markers(result_id, user_id, date, name, value, value_text, "
                "unit, ref_low, ref_high, flag) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, user_id, date, m.get("name"), m.get("value"), m.get("value_text"),
                 m.get("unit"), m.get("ref_low"), m.get("ref_high"), m.get("flag")),
            )
        return rid


def lab_dates(user_id: int) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT date FROM lab_results WHERE user_id = ? ORDER BY date DESC",
            (user_id,),
        ).fetchall()
        return [r["date"] for r in rows]


def markers_for_date(user_id: int, date: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM lab_markers WHERE user_id = ? AND date = ? ORDER BY id",
            (user_id, date),
        ).fetchall()
        return [dict(r) for r in rows]


def marker_history(user_id: int, name: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT date, value, value_text, unit, flag FROM lab_markers "
            "WHERE user_id = ? AND name = ? ORDER BY date",
            (user_id, name),
        ).fetchall()
        return [dict(r) for r in rows]


def latest_markers(user_id: int) -> list[dict]:
    """Последнее значение каждого маркера (по имени)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT m.* FROM lab_markers m "
            "JOIN (SELECT name, MAX(date) md FROM lab_markers WHERE user_id = ? GROUP BY name) x "
            "ON m.name = x.name AND m.date = x.md WHERE m.user_id = ? ORDER BY m.name",
            (user_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- Oura ----------

def save_oura_tokens(user_id: int, access: str, refresh: str, expires_at: float) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO oura_tokens(user_id, access_token, refresh_token, expires_at) "
            "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
            "expires_at=excluded.expires_at",
            (user_id, access, refresh, expires_at),
        )


def get_oura_tokens(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM oura_tokens WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def delete_oura_tokens(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM oura_tokens WHERE user_id = ?", (user_id,))


def oura_connected(user_id: int) -> bool:
    return get_oura_tokens(user_id) is not None


def oura_users() -> list[int]:
    with _conn() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM oura_tokens").fetchall()]


def upsert_oura_daily(user_id: int, date: str, **fields) -> None:
    cols = _OURA_BASE_COLS + list(_OURA_COLUMNS)
    fields = {k: v for k, v in fields.items() if k in cols and v is not None}
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO oura_daily(user_id, date) VALUES(?, ?)", (user_id, date))
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"UPDATE oura_daily SET {sets} WHERE user_id = ? AND date = ?",
                      (*fields.values(), user_id, date))


def oura_range(user_id: int, dates: list[str]) -> dict[str, dict]:
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM oura_daily WHERE user_id = ? AND date IN ({marks})",
            (user_id, *dates),
        ).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d.pop("user_id", None)
            out[r["date"]] = d
        return out


def upsert_oura_workout(user_id: int, rec: dict) -> None:
    """Тренировка с кольца. Дедуп по oura_id: повторный забор не плодит копии."""
    if not rec.get("oura_id") or not rec.get("day"):
        return
    with _conn() as c:
        c.execute(
            "INSERT INTO oura_workouts(oura_id, user_id, day, start_dt, end_dt, activity, "
            "intensity, calories, distance) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(oura_id) DO UPDATE SET day=excluded.day, start_dt=excluded.start_dt, "
            "end_dt=excluded.end_dt, activity=excluded.activity, intensity=excluded.intensity, "
            "calories=excluded.calories, distance=excluded.distance",
            (str(rec["oura_id"]), user_id, rec["day"], rec.get("start"), rec.get("end"),
             rec.get("activity"), rec.get("intensity"), rec.get("calories"), rec.get("distance")),
        )


def _oura_workout_rows(rows) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        d["start"] = d.pop("start_dt", None)
        d["end"] = d.pop("end_dt", None)
        d.pop("user_id", None)
        out.append(d)
    return out


def oura_workouts_for_date(user_id: int, day: str) -> list[dict]:
    """Тренировки кольца за дату — самая «дорогая» первой."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM oura_workouts WHERE user_id = ? AND day = ? "
            "ORDER BY COALESCE(calories, 0) DESC, start_dt",
            (user_id, day),
        ).fetchall()
    return _oura_workout_rows(rows)


def oura_workouts_range(user_id: int, dates: list[str]) -> list[dict]:
    if not dates:
        return []
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM oura_workouts WHERE user_id = ? AND day IN ({marks}) "
            f"ORDER BY day DESC, start_dt DESC",
            (user_id, *dates),
        ).fetchall()
    return _oura_workout_rows(rows)


def oura_latest(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM oura_daily WHERE user_id = ? ORDER BY date DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------- WHOOP ----------

_WHOOP_COLS = ["recovery", "hrv", "resting_hr", "spo2", "skin_temp", "sleep_h",
               "sleep_perf", "deep_h", "rem_h", "light_h", "awake_h", "breath_avg",
               "strain", "day_kcal", "avg_hr", "max_hr", "extra_json"]


def save_whoop_tokens(user_id: int, access: str, refresh: str, expires_at: float) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO whoop_tokens(user_id, access_token, refresh_token, expires_at) "
            "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "access_token=excluded.access_token, refresh_token=excluded.refresh_token, "
            "expires_at=excluded.expires_at",
            (user_id, access, refresh, expires_at),
        )


def get_whoop_tokens(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM whoop_tokens WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def delete_whoop_tokens(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM whoop_tokens WHERE user_id = ?", (user_id,))


def whoop_connected(user_id: int) -> bool:
    return get_whoop_tokens(user_id) is not None


def whoop_users() -> list[int]:
    with _conn() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM whoop_tokens").fetchall()]


def upsert_whoop_daily(user_id: int, date: str, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in _WHOOP_COLS and v is not None}
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO whoop_daily(user_id, date) VALUES(?, ?)", (user_id, date))
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"UPDATE whoop_daily SET {sets} WHERE user_id = ? AND date = ?",
                      (*fields.values(), user_id, date))


def whoop_range(user_id: int, dates: list[str]) -> dict[str, dict]:
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM whoop_daily WHERE user_id = ? AND date IN ({marks})",
            (user_id, *dates),
        ).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        d.pop("user_id", None)
        out[r["date"]] = d
    return out


def whoop_latest(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM whoop_daily WHERE user_id = ? ORDER BY date DESC LIMIT 1",
                        (user_id,)).fetchone()
        return dict(row) if row else None


def upsert_whoop_workout(user_id: int, rec: dict) -> None:
    """Тренировка с браслета. Дедуп по id записи WHOOP."""
    if not rec.get("whoop_id") or not rec.get("day"):
        return
    with _conn() as c:
        c.execute(
            "INSERT INTO whoop_workouts(whoop_id, user_id, day, start_dt, end_dt, sport, "
            "strain, calories, distance, avg_hr) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(whoop_id) DO UPDATE SET day=excluded.day, start_dt=excluded.start_dt, "
            "end_dt=excluded.end_dt, sport=excluded.sport, strain=excluded.strain, "
            "calories=excluded.calories, distance=excluded.distance, avg_hr=excluded.avg_hr",
            (str(rec["whoop_id"]), user_id, rec["day"], rec.get("start"), rec.get("end"),
             rec.get("sport"), rec.get("strain"), rec.get("calories"),
             rec.get("distance"), rec.get("avg_hr")),
        )


def _whoop_workout_rows(rows) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        d["start"] = d.pop("start_dt", None)
        d["end"] = d.pop("end_dt", None)
        d.pop("user_id", None)
        out.append(d)
    return out


def whoop_workouts_for_date(user_id: int, day: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM whoop_workouts WHERE user_id = ? AND day = ? "
            "ORDER BY COALESCE(calories, 0) DESC, start_dt", (user_id, day),
        ).fetchall()
    return _whoop_workout_rows(rows)


def whoop_workouts_range(user_id: int, dates: list[str]) -> list[dict]:
    if not dates:
        return []
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM whoop_workouts WHERE user_id = ? AND day IN ({marks}) "
            f"ORDER BY day DESC, start_dt DESC", (user_id, *dates),
        ).fetchall()
    return _whoop_workout_rows(rows)


# ---------- напоминания ----------

_REMINDER_FIELDS = ("kind", "text", "time", "every_min", "win_from", "win_to",
                    "dow_mask", "enabled")


def add_reminder(user_id: int, kind: str, text: str, time: str | None = None,
                 every_min: int | None = None, win_from: str | None = None,
                 win_to: str | None = None, dow_mask: int | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO reminders(user_id, kind, text, time, every_min, win_from, "
            "win_to, dow_mask) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, kind, (text or "").strip()[:200], time, every_min,
             win_from, win_to, dow_mask),
        )
        return cur.lastrowid


def list_reminders(user_id: int, only_enabled: bool = True) -> list[dict]:
    q = "SELECT * FROM reminders WHERE user_id = ?"
    if only_enabled:
        q += " AND enabled = 1"
    q += " ORDER BY COALESCE(time, win_from, ''), id"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, (user_id,)).fetchall()]


def get_reminder(reminder_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return dict(row) if row else None


def update_reminder(reminder_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in _REMINDER_FIELDS}
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE reminders SET {cols} WHERE id = ?",
                  (*fields.values(), reminder_id))


def disable_reminder(reminder_id: int) -> None:
    """Выключаем, а не удаляем: клиент может попросить вернуть."""
    update_reminder(reminder_id, enabled=0)


def all_reminders() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM reminders WHERE enabled = 1").fetchall()]


def _hhmm_to_min(value: str | None) -> int | None:
    try:
        h, m = str(value).split(":")
        return int(h) * 60 + int(m)
    except (AttributeError, ValueError):
        return None


def reminder_due(rem: dict, now) -> bool:
    """Пора ли слать это напоминание в указанную минуту."""
    if not rem.get("enabled"):
        return False
    mask = rem.get("dow_mask")
    if mask is not None and not (int(mask) >> now.weekday()) & 1:
        return False

    minute_now = now.hour * 60 + now.minute
    if rem.get("time"):
        return _hhmm_to_min(rem["time"]) == minute_now

    every = rem.get("every_min")
    if not every:
        return False
    start = _hhmm_to_min(rem.get("win_from")) or 0
    end = _hhmm_to_min(rem.get("win_to"))
    if end is None:
        end = 24 * 60 - 1
    if not start <= minute_now <= end:
        return False
    return (minute_now - start) % int(every) == 0


def due_reminders(now) -> list[dict]:
    """Все напоминания, которые пора отправить в эту минуту."""
    return [r for r in all_reminders() if reminder_due(r, now)]


# ---------- вода ----------

def add_water(user_id: int, date: str, time: str, ml: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO water_log(user_id, date, time, ml) VALUES(?,?,?,?)",
            (user_id, date, time, ml),
        )


def water_total(user_id: int, date: str) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(ml), 0) s FROM water_log WHERE user_id = ? AND date = ?",
            (user_id, date),
        ).fetchone()
        return int(row["s"])


def reset_water(user_id: int, date: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM water_log WHERE user_id = ? AND date = ?", (user_id, date))


def water_by_date(user_id: int, dates: list[str]) -> dict[str, int]:
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, SUM(ml) s FROM water_log WHERE user_id = ? AND date IN ({marks}) "
            f"GROUP BY date",
            (user_id, *dates),
        ).fetchall()
        return {r["date"]: int(r["s"]) for r in rows}


# ---------- память разговора ----------

def add_chat(user_id: int, role: str, content: str) -> None:
    """Реплика в историю разговора. Пишем и клиента, и ассистента."""
    content = (content or "").strip()
    if not content:
        return
    with _conn() as c:
        c.execute("INSERT INTO chat_history(user_id, role, content) VALUES(?,?,?)",
                  (user_id, role, content[:4000]))


def chat_tail(user_id: int, limit: int = 15) -> list[dict]:
    """Последние реплики по возрастанию — как их читает модель."""
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content, ts FROM chat_history WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?", (user_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def trim_chat(user_id: int, keep: int = 400) -> int:
    """Подрезает старое: история нужна, но не бесконечная."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM chat_history WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, keep),
        )
        return cur.rowcount


def chat_count(user_id: int) -> int:
    with _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM chat_history WHERE user_id = ?",
                             (user_id,)).fetchone()["n"])


# ---------- вес ----------

def add_weight(user_id: int, date: str, kg: float) -> None:
    with _conn() as c:
        c.execute("INSERT INTO weight_log(user_id, date, kg) VALUES(?,?,?)",
                  (user_id, date, float(kg)))


def weight_history(user_id: int, limit: int = 30) -> list[dict]:
    """История веса, свежее первым."""
    with _conn() as c:
        rows = c.execute(
            "SELECT date, kg FROM weight_log WHERE user_id = ? ORDER BY date DESC, id DESC "
            "LIMIT ?", (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_last_weight(user_id: int) -> float | None:
    with _conn() as c:
        row = c.execute("SELECT id, kg FROM weight_log WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                        (user_id,)).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM weight_log WHERE id = ?", (row["id"],))
        return float(row["kg"])


# ---------- заметки тренеру ----------

def add_trainer_note(user_id: int, date: str, text: str) -> None:
    """Что ассистент решил подсветить тренеру: вопрос, жалоба, тревога клиента."""
    text = (text or "").strip()
    if not text:
        return
    with _conn() as c:
        c.execute("INSERT INTO trainer_notes(user_id, date, text) VALUES(?,?,?)",
                  (user_id, date, text[:1000]))


def trainer_notes_range(user_id: int, dates: list[str]) -> list[dict]:
    if not dates:
        return []
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, text, created_at FROM trainer_notes "
            f"WHERE user_id = ? AND date IN ({marks}) ORDER BY date DESC, id DESC",
            (user_id, *dates),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- привычки ----------

def set_habit(user_id: int, date: str, key: str, value: int = 1) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO habit_log(user_id, date, key, value) VALUES(?,?,?,?)",
            (user_id, date, key, value),
        )


def get_habit(user_id: int, date: str, key: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM habit_log WHERE user_id = ? AND date = ? AND key = ?",
            (user_id, date, key),
        ).fetchone()
        return bool(row and row["value"])


def habit_dates(user_id: int, key: str, dates: list[str]) -> set[str]:
    if not dates:
        return set()
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT date FROM habit_log WHERE user_id = ? AND key = ? AND value = 1 "
            f"AND date IN ({marks})",
            (user_id, key, *dates),
        ).fetchall()
        return {r["date"] for r in rows}


def habit_streak(user_id: int, key: str, dates_desc: list[str]) -> int:
    """Сколько дней подряд (начиная с сегодняшнего/вчерашнего) привычка выполнена.
    dates_desc — список дат по убыванию, начиная с сегодня."""
    done = habit_dates(user_id, key, dates_desc)
    streak = 0
    for i, d in enumerate(dates_desc):
        if d in done:
            streak += 1
        elif i == 0:
            continue  # сегодня ещё можно успеть — серия не рвётся
        else:
            break
    return streak


# ---------- тренировки ----------

def set_workout_plan(user_id: int, entries: list[tuple[int, str]]) -> None:
    """entries: [(dow, 'HH:MM'), ...] — полностью заменяет план."""
    with _conn() as c:
        c.execute("DELETE FROM workout_plan WHERE user_id = ?", (user_id,))
        c.executemany(
            "INSERT INTO workout_plan(user_id, dow, time) VALUES(?,?,?)",
            [(user_id, d, t) for d, t in entries],
        )


def get_workout_plan(user_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM workout_plan WHERE user_id = ? ORDER BY dow, time", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def add_workout_log(user_id: int, date: str, time: str, status: str, note: str = "",
                    duration_min: int | None = None, description: str = "",
                    kcal_burned: int | None = None, kcal_source: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO workout_log(user_id, date, time, status, note, duration_min, "
            "description, kcal_burned, kcal_source) VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, date, time, status, note, duration_min,
             (description or "").strip()[:500], kcal_burned, kcal_source),
        )


def delete_last_workout(user_id: int, date: str) -> dict | None:
    """Убирает последнюю запись о тренировке за дату (для /undo)."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM workout_log WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 1",
            (user_id, date),
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM workout_log WHERE id = ?", (row["id"],))
        return dict(row)


def delete_last_water(user_id: int, date: str) -> int:
    """Убирает последнюю порцию воды за дату. Возвращает сколько мл убрали."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, ml FROM water_log WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 1",
            (user_id, date),
        ).fetchone()
        if not row:
            return 0
        c.execute("DELETE FROM water_log WHERE id = ?", (row["id"],))
        return int(row["ml"])


def delete_wellbeing(user_id: int, date: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM wellbeing WHERE user_id = ? AND date = ?", (user_id, date))
        return cur.rowcount > 0


def workouts_detailed(user_id: int, dates: list[str], limit: int = 200) -> list[dict]:
    """Записанные тренировки за окно — свежие первыми, с длительностью и описанием."""
    if not dates:
        return []
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, time, status, duration_min, description, kcal_burned, kcal_source "
        f"FROM workout_log "
            f"WHERE user_id = ? AND date IN ({marks}) "
            f"ORDER BY date DESC, id DESC LIMIT ?",
            (user_id, *dates, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def workout_for_date(user_id: int, date: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM workout_log WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 1",
            (user_id, date),
        ).fetchone()
        return dict(row) if row else None


def workouts_by_date(user_id: int, dates: list[str]) -> dict[str, str]:
    """date -> статус последней записи за дату."""
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    with _conn() as c:
        rows = c.execute(
            f"SELECT date, status FROM workout_log WHERE user_id = ? AND date IN ({marks}) "
            f"ORDER BY id",
            (user_id, *dates),
        ).fetchall()
        return {r["date"]: r["status"] for r in rows}
