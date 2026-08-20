#!/usr/bin/env bash
# Prepares the virtualenv and a systemd unit for this machine.
# Privileged steps are printed, not executed.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_AS="${SUDO_USER:-$USER}"
UNIT="$DIR/token-alerts.generated.service"

cd "$DIR"

echo "==> Виртуальное окружение"
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
echo "    зависимости установлены"

echo "==> Файл службы"
sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$RUN_AS|g" \
    token-alerts.service > "$UNIT"
echo "    $UNIT"

cat <<EOF

Перед запуском положите ключи (chmod 600):

    ~/.claude-lab/shared/secrets/tg-alerts-bot-token   — токен бота от @BotFather
    ~/.claude-lab/shared/secrets/groq-api-key          — ключ Groq, опционально

Пути к ним заданы в telegram.py и summary.py.

Проверить вручную:

    ./.venv/bin/python bot.py

Автозапуск:

    sudo cp "$UNIT" /etc/systemd/system/token-alerts.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now token-alerts

Бот стартует в тихом режиме: копит находки и присылает сводку раз в сутки.
Включить мгновенные уведомления — команда /mode live в чате с ботом.
EOF
