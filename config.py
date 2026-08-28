"""Загрузка настроек из файла .env."""
import os

from dotenv import load_dotenv

load_dotenv()


def _clean(value: str) -> str:
    """Пустая строка вместо незаполненных заглушек вида ВСТАВЬ_СЮДА_..."""
    value = (value or "").strip().strip('"').strip("'")
    return "" if "ВСТАВЬ" in value else value


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# --- ИИ для анализа еды: достаточно одного из ключей ---
ANTHROPIC_API_KEY = _clean(os.getenv("ANTHROPIC_API_KEY", ""))
OPENROUTER_API_KEY = _clean(os.getenv("OPENROUTER_API_KEY", ""))

# Модель Claude (если задан ANTHROPIC_API_KEY).
MODEL = os.getenv("MODEL", "claude-sonnet-5").strip() or "claude-sonnet-5"

# Модель OpenRouter. "auto" — бот сам подберёт бесплатную модель с поддержкой фото.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "auto").strip() or "auto"

# Принудительный выбор: anthropic | openrouter | demo (обычно не нужен).
_FORCED_PROVIDER = os.getenv("PROVIDER", "").strip().lower()


def resolve_provider() -> str:
    if _FORCED_PROVIDER in ("anthropic", "openrouter", "demo"):
        return _FORCED_PROVIDER
    if ANTHROPIC_API_KEY:
        return "anthropic"
    if OPENROUTER_API_KEY:
        return "openrouter"
    return "demo"


ACTIVE_PROVIDER = resolve_provider()

# Необязательно: список Telegram ID через запятую, кому разрешён доступ.
# Если пусто — бот привязывается к первому, кто ему напишет.
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# Файл базы данных (дневник питания).
DB_PATH = os.getenv("DB_PATH", "food_diary.db")

# Веб-дашборд.
DASHBOARD_ENABLED = os.getenv("DASHBOARD", "on").strip().lower() not in ("off", "0", "false", "нет")
try:
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8127"))
except ValueError:
    DASHBOARD_PORT = 8127

# Публичный адрес бота снаружи, например https://example.com — слэш справа
# убираем. Пусто — ссылки собираются как раньше, из адреса сервера и порта.
PUBLIC_BASE_URL = _clean(os.getenv("PUBLIC_BASE_URL", "")).rstrip("/")


def public_url(path: str = "", host: str = "<адрес-сервера>") -> str:
    """Публичная ссылка на дашборд или кабинет тренера.

    Задан PUBLIC_BASE_URL — берём его. Иначе прежнее поведение:
    http://<host>:DASHBOARD_PORT, где host — заглушка или реальный адрес.
    """
    if path and not path.startswith("/"):
        path = "/" + path
    base = PUBLIC_BASE_URL or f"http://{host}:{DASHBOARD_PORT}"
    return f"{base}{path}"


# --- Разговорный ассистент (v2) ---
# on — весь клиентский ввод идёт в agent.py: команд и шаговых диалогов нет.
# off — рубильник отката на прежние хендлеры, пока v2 не отстоится.
AGENT_MODE = os.getenv("AGENT_MODE", "on").strip().lower() not in ("off", "0", "false", "нет")

# --- Голосовой ввод (faster-whisper) ---
# По умолчанию выключен: пока модель не развёрнута на машине, бот вежливо
# просит написать текстом вместо того, чтобы падать.
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "off").strip().lower() in ("on", "1", "true", "да")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium").strip() or "medium"
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8").strip() or "int8"
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "ru").strip() or "ru"
try:
    VOICE_MAX_SECONDS = int(os.getenv("VOICE_MAX_SECONDS", "180"))
except ValueError:
    VOICE_MAX_SECONDS = 180

# --- Oura (кольцо) ---
OURA_CLIENT_ID = _clean(os.getenv("OURA_CLIENT_ID", ""))
OURA_CLIENT_SECRET = _clean(os.getenv("OURA_CLIENT_SECRET", ""))
# Полный адрес обратного вызова, зарегистрированный в Oura-приложении,
# например http://<IP-сервера>:8127/oura/callback
OURA_REDIRECT_URI = _clean(os.getenv("OURA_REDIRECT_URI", ""))
OURA_ENABLED = bool(OURA_CLIENT_ID and OURA_CLIENT_SECRET and OURA_REDIRECT_URI)

# --- WHOOP (браслет) ---
WHOOP_CLIENT_ID = _clean(os.getenv("WHOOP_CLIENT_ID", ""))
WHOOP_CLIENT_SECRET = _clean(os.getenv("WHOOP_CLIENT_SECRET", ""))
WHOOP_REDIRECT_URI = _clean(os.getenv("WHOOP_REDIRECT_URI", ""))
WHOOP_ENABLED = bool(WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET and WHOOP_REDIRECT_URI)

# Ключ доступа к дашборду. Пусто — дашборд виден только с этого компьютера
# (localhost). Задан (на сервере генерируется при деплое) — дашборд доступен
# извне по ссылке http://<сервер>:8127/?key=КЛЮЧ
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
