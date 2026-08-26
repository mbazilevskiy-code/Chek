#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1

# --- Первый запуск: создаём .env и открываем его для заполнения ---
if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo "ПЕРВЫЙ ЗАПУСК. Открой файл .env в этой папке, вставь туда токен бота"
    echo "и ключ Anthropic, сохрани — и запусти скрипт ещё раз."
    echo
    open -t .env 2>/dev/null || xdg-open .env 2>/dev/null || true
    exit 0
fi

# --- Если в .env остались незаполненные поля, открываем его ---
if grep -q "ВСТАВЬ" .env; then
    echo
    echo "В файле .env остались незаполненные поля — вставь недостающий ключ,"
    echo "сохрани файл и запусти скрипт ещё раз."
    echo
    open -t .env 2>/dev/null || xdg-open .env 2>/dev/null || true
    exit 0
fi

# --- Python ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 не найден. Установи с https://www.python.org/downloads/ и запусти снова."
    exit 1
fi

# --- Виртуальное окружение и зависимости ---
if [ ! -d .venv ]; then
    echo "Готовлю окружение, это одна минута..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

# --- Запуск ---
python bot.py
