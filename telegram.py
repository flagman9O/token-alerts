"""Telegram transport: sending alerts and taking commands from the chat.

Long polling rather than webhooks — no public address needed, and the bot
sits behind the same loopback as everything else here.
"""

import logging

import aiohttp

import config

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{}/{}"


def _token():
    tok = config.bot_token()
    if not tok:
        log.error("не задан токен бота — см. config.py")
    return tok


async def send(session, text, preview=False):
    tok = _token()
    if not tok:
        return False
    try:
        async with session.post(
                API.format(tok, "sendMessage"),
                json={"chat_id": config.owner_id(), "text": text,
                      "parse_mode": "HTML",
                      "disable_web_page_preview": not preview},
                timeout=aiohttp.ClientTimeout(total=25)) as r:
            d = await r.json(content_type=None)
            if not d.get("ok"):
                log.warning("telegram: %s", d.get("description"))
                return False
            return True
    except Exception as e:
        log.warning("telegram send: %s", type(e).__name__)
        return False


async def get_updates(session, offset, timeout=25):
    """Returns (updates, new_offset)."""
    tok = _token()
    if not tok:
        return [], offset
    try:
        async with session.get(
                API.format(tok, "getUpdates"),
                params={"offset": offset, "timeout": timeout},
                timeout=aiohttp.ClientTimeout(total=timeout + 10)) as r:
            d = await r.json(content_type=None)
    except Exception:
        return [], offset

    if not d.get("ok"):
        return [], offset
    ups = d.get("result", [])
    for u in ups:
        offset = max(offset, u["update_id"] + 1)
    return ups, offset


def message_text(update):
    """Owner's plain text, or None for anything else."""
    msg = update.get("message") or update.get("edited_message") or {}
    if (msg.get("from") or {}).get("id") != config.owner_id():
        return None
    return (msg.get("text") or "").strip() or None
