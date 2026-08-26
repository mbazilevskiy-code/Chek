#!/usr/bin/env bash
# Разворачивает бота «Чек» на сервере (Ubuntu/Debian). Запускается автоматически
# из deploy_windows.bat; можно запустить и вручную: bash /opt/chek/deploy.sh
set -e
cd /opt/chek

echo "== 1/5. Часовой пояс =="
timedatectl set-timezone Europe/Moscow || true

echo "== 2/5. Python и зависимости =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip >/dev/null
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
./.venv/bin/pip install -q -r requirements.txt

echo "== 3/5. Ключ дашборда =="
if ! grep -q '^DASHBOARD_TOKEN=..*' .env; then
    TOKEN=$(head -c 32 /dev/urandom | md5sum | cut -c1-16)
    if grep -q '^DASHBOARD_TOKEN=' .env; then
        sed -i "s/^DASHBOARD_TOKEN=.*/DASHBOARD_TOKEN=$TOKEN/" .env
    else
        printf '\nDASHBOARD_TOKEN=%s\n' "$TOKEN" >> .env
    fi
fi
TOKEN=$(grep '^DASHBOARD_TOKEN=' .env | head -1 | cut -d= -f2)

echo "== 4/5. Служба автозапуска =="
cat > /etc/systemd/system/chek-bot.service <<'UNIT'
[Unit]
Description=Chek health bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/chek
ExecStart=/opt/chek/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable chek-bot >/dev/null 2>&1
systemctl restart chek-bot

echo "== 5/5. Проверка =="
sleep 4
if systemctl is-active --quiet chek-bot; then
    IP=$(hostname -I | awk '{print $1}')
    echo
    echo "======================================================"
    echo "  ГОТОВО! Бот работает на сервере 24/7."
    echo
    echo "  Дашборд (открой и сохрани в закладки):"
    echo "  http://$IP:8127/?key=$TOKEN"
    echo
    echo "  Логи бота:   journalctl -u chek-bot -n 50"
    echo "  Перезапуск:  systemctl restart chek-bot"
    echo "======================================================"
else
    echo
    echo "!! Служба не запустилась. Последние строки лога:"
    journalctl -u chek-bot -n 25 --no-pager || true
    exit 1
fi
