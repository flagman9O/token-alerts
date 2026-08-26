#!/usr/bin/env bash
# Interactive setup: asks for the four credentials the bot needs, writes them
# to ~/.token-alerts/, builds the virtualenv, and prepares a systemd unit.
# No AI helper required — every step prints exactly what to click and where.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_AS="${SUDO_USER:-$USER}"
CFG="${TOKEN_ALERTS_HOME:-$HOME/.token-alerts}"
UNIT="$DIR/token-alerts.generated.service"

cd "$DIR"

echo "=== Установка Token Alerts ==="
echo "Сейчас спрошу четыре вещи: токен бота, ваш Telegram ID, ключ GMGN и"
echo "(по желанию) ключ Groq. Ничего из этого никуда, кроме вашего сервера,"
echo "не уходит — ключи лягут в $CFG с правами только для вас."
echo

# ---------- уже настроено раньше? ----------

SKIP_CREDS=0
if [ -f "$CFG/bot-token" ] && [ -f "$CFG/owner-id" ] && [ -f "$CFG/gmgn-api-key" ]; then
    read -r -p "Настройка уже есть в $CFG — переспросить ключи заново? [y/N] " ans
    if [[ ! "$ans" =~ ^[Yy]$ ]]; then
        SKIP_CREDS=1
        echo "Оставляю как есть, перехожу к установке зависимостей."
    fi
fi

if [ "$SKIP_CREDS" -eq 0 ]; then

    # ---------- 1. токен бота ----------

    echo
    echo "== 1/4: Токен телеграм-бота =="
    echo "В Telegram напишите @BotFather, отправьте /newbot, придумайте имя."
    echo "В ответ придёт токен вида 123456789:AAF...— вставьте его сюда."
    echo
    while true; do
        read -r -p "Токен: " BOT_TOKEN
        if [[ "$BOT_TOKEN" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]]; then
            break
        fi
        echo "Не похоже на токен бота (должно быть «цифры:буквы»). Ещё раз:"
    done

    echo "Проверяю токен..."
    BOT_INFO="$(curl -s -m 10 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" || true)"
    BOT_USERNAME="$(printf '%s' "$BOT_INFO" | python3 -c \
        "import sys,json
try:
    d = json.load(sys.stdin)
    print(d['result']['username'] if d.get('ok') else '')
except Exception:
    print('')" 2>/dev/null)"
    if [ -z "$BOT_USERNAME" ]; then
        echo "Telegram не подтвердил токен. Проверьте его и запустите скрипт заново."
        exit 1
    fi
    echo "Бот подтверждён: @$BOT_USERNAME"

    # ---------- 2. владелец: сам находит свой chat id ----------

    echo
    echo "== 2/4: Ваш Telegram ID =="
    echo "Откройте Telegram, найдите @$BOT_USERNAME и отправьте ему любое"
    echo "сообщение (например /start) — я подхвачу ваш ID автоматически."
    printf "Жду сообщение "
    OWNER_ID=""
    for _ in $(seq 1 40); do
        UPD="$(curl -s -m 5 "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates?timeout=1" || true)"
        OWNER_ID="$(printf '%s' "$UPD" | python3 -c \
            "import sys, json
try:
    d = json.load(sys.stdin)
    for u in d.get('result', []):
        frm = (u.get('message') or {}).get('from') or {}
        if frm.get('id'):
            print(frm['id']); break
except Exception:
    pass" 2>/dev/null)"
        [ -n "$OWNER_ID" ] && break
        printf "."
        sleep 3
    done
    echo
    if [ -z "$OWNER_ID" ]; then
        echo "Не дождался сообщения. Введите ID вручную — его пришлёт @userinfobot:"
        while true; do
            read -r -p "ID: " OWNER_ID
            [[ "$OWNER_ID" =~ ^[0-9]+$ ]] && break
            echo "ID — это число. Ещё раз:"
        done
    else
        echo "Ваш Telegram ID: $OWNER_ID"
    fi

    # ---------- 3. ключ GMGN ----------

    echo
    echo "== 3/4: Ключ GMGN (рыночные данные) =="
    if ! command -v openssl >/dev/null 2>&1; then
        echo "Нужен openssl, в системе не найден. Установите его и запустите"
        echo "скрипт заново (для Debian/Ubuntu: apt install openssl)."
        exit 1
    fi
    TMP_PRIV="$(mktemp)"; TMP_PUB="$(mktemp)"
    openssl genpkey -algorithm ed25519 -out "$TMP_PRIV" 2>/dev/null
    openssl pkey -in "$TMP_PRIV" -pubout -out "$TMP_PUB" 2>/dev/null
    PUBKEY_ENC="$(python3 -c \
        "import urllib.parse,sys; print(urllib.parse.quote(open(sys.argv[1]).read()))" \
        "$TMP_PUB")"
    rm -f "$TMP_PRIV" "$TMP_PUB"
    # Этому боту private-часть ключа не нужна вовсе — он никогда не торгует и
    # не подписывает запросы, только читает публичные рыночные данные по
    # X-APIKEY. Ключ сгенерирован и сразу выброшен, только чтобы gmgn
    # согласился выдать API-ключ — так требует их форма создания ключа.
    echo "Откройте ссылку — она уже с вашим публичным ключом, форма создания"
    echo "GMGN API Key откроется заполненной:"
    echo
    echo "https://gmgn.ai/ai/generateapi?pbk=${PUBKEY_ENC}"
    echo
    echo "На странице: «Enable Reading» — включить, «Enable Trading» — оставить"
    echo "выключенным (боту она не нужна, торговать он не умеет). Нажмите"
    echo "Create и скопируйте выданный API Key."
    while true; do
        read -r -p "GMGN API Key: " GMGN_KEY
        [ -n "$GMGN_KEY" ] && break
        echo "Пустым оставить нельзя, без него бот не работает."
    done

    # ---------- 4. ключ Groq (необязательно) ----------

    echo
    echo "== 4/4: Ключ Groq — сводки о токенах, необязательно =="
    echo "Бесплатный ключ: https://console.groq.com/keys"
    echo "Оставьте пустым и нажмите Enter, чтобы пропустить — бот будет"
    echo "работать и без сводок «что за мем»."
    read -r -p "Ключ Groq: " GROQ_KEY

    # ---------- запись ----------

    mkdir -p "$CFG"
    chmod 700 "$CFG"
    printf '%s' "$BOT_TOKEN" > "$CFG/bot-token"
    printf '%s' "$OWNER_ID"  > "$CFG/owner-id"
    printf '%s' "$GMGN_KEY"  > "$CFG/gmgn-api-key"
    if [ -n "$GROQ_KEY" ]; then
        printf '%s' "$GROQ_KEY" > "$CFG/groq-key"
    fi
    chmod 600 "$CFG"/*
    echo
    echo "Ключи сохранены в $CFG"
fi

# ---------- окружение ----------

echo
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

Проверить вручную:

    ./.venv/bin/python bot.py

Автозапуск:

    sudo cp "$UNIT" /etc/systemd/system/token-alerts.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now token-alerts

Бот стартует в тихом режиме: копит находки и присылает сводку раз в сутки.
Включить мгновенные уведомления — команда /mode live в чате с ботом.
Настроить пороги по сетям — команда /settings.
EOF
