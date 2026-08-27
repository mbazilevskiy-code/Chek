"""SQLite-дневник питания, воды, привычек и тренировок."""
import json
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
}

# Новые колонки supplements. План приёма задаёт сам клиент:
# 7 = каждый день, N = N раз в неделю.
_SUPPL_COLUMNS = {
    "plan_days_per_week": "INTEGER DEFAULT 7",
}


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        for col, ddl in _USER_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        have = {r["name"] for r in c.execute("PRAGMA table_info(supplements)").fetchall()}
        for col, ddl in _SUPPL_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE supplements ADD COLUMN {col} {ddl}")


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
        c.execute("INSERT OR IGNORE INTO users(user_id, name, coach_id) VALUES(?, ?, ?)",
                  (user_id, name, coach_id))
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
    allowed = {"energy", "mood", "stress", "libido", "note"}
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
    cols = ["readiness", "sleep_score", "sleep_h", "hrv", "resting_hr",
            "temp_dev", "activity_score", "steps"]
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


def oura_latest(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM oura_daily WHERE user_id = ? ORDER BY date DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


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


def add_workout_log(user_id: int, date: str, time: str, status: str, note: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO workout_log(user_id, date, time, status, note) VALUES(?,?,?,?,?)",
            (user_id, date, time, status, note),
        )


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
