"""Telegram transport: sending alerts, running the settings menu, taking
commands from the chat.

Long polling rather than webhooks — no public address needed, and the bot
sits behind the same loopback as everything else here. The settings menu
(see menu.py) is built on inline keyboards: buttons carry a `callback_data`
string, presses come back through the same long-poll as `callback_query`
updates, and the menu is redrawn in place with `editMessageText` rather than
piling up a new message per click.
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


async def send(session, text, preview=False, reply_markup=None):
    tok = _token()
    if not tok:
        return False
    payload = {"chat_id": config.owner_id(), "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": not preview}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with session.post(
                API.format(tok, "sendMessage"), json=payload,
                timeout=aiohttp.ClientTimeout(total=25)) as r:
            d = await r.json(content_type=None)
            if not d.get("ok"):
                log.warning("telegram: %s", d.get("description"))
                return False
            return True
    except Exception as e:
        log.warning("telegram send: %s", type(e).__name__)
        return False


async def edit_message(session, chat_id, message_id, text, reply_markup=None):
    """Redraws a menu screen in place instead of sending a new message."""
    tok = _token()
    if not tok:
        return False
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True,
               "reply_markup": reply_markup or {"inline_keyboard": []}}
    try:
        async with session.post(
                API.format(tok, "editMessageText"), json=payload,
                timeout=aiohttp.ClientTimeout(total=25)) as r:
            d = await r.json(content_type=None)
            if not d.get("ok"):
                # "message is not modified" fires when a redraw changes
                # nothing — harmless, the owner just clicked the same value.
                desc = d.get("description") or ""
                if "not modified" not in desc:
                    log.warning("telegram edit: %s", desc)
                return False
            return True
    except Exception as e:
        log.warning("telegram edit: %s", type(e).__name__)
        return False


async def edit_markup(session, chat_id, message_id, reply_markup=None):
    """Swaps the buttons under an already-sent message without touching its
    text — used after a mute button is pressed, so the alert card itself
    stays intact and only the row of buttons changes."""
    tok = _token()
    if not tok:
        return False
    payload = {"chat_id": chat_id, "message_id": message_id,
               "reply_markup": reply_markup or {"inline_keyboard": []}}
    try:
        async with session.post(
                API.format(tok, "editMessageReplyMarkup"), json=payload,
                timeout=aiohttp.ClientTimeout(total=25)) as r:
            d = await r.json(content_type=None)
            if not d.get("ok"):
                desc = d.get("description") or ""
                if "not modified" not in desc:
                    log.warning("telegram edit_markup: %s", desc)
                return False
            return True
    except Exception as e:
        log.warning("telegram edit_markup: %s", type(e).__name__)
        return False


async def answer_callback(session, callback_query_id, text=None, show_alert=False):
    """Must be called for every button press, or the client shows a spinner
    until Telegram times it out on its own."""
    tok = _token()
    if not tok:
        return
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    try:
        async with session.post(
                API.format(tok, "answerCallbackQuery"), json=payload,
                timeout=aiohttp.ClientTimeout(total=15)):
            pass
    except Exception as e:
        log.warning("telegram answer_callback: %s", type(e).__name__)


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


def callback_query(update):
    """A button press from the owner: (id, data, chat_id, message_id), or
    None for anything else — including presses from anyone but the owner,
    since the bot token being private is not itself an access control."""
    cq = update.get("callback_query")
    if not cq:
        return None
    if (cq.get("from") or {}).get("id") != config.owner_id():
        return None
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not chat_id or not message_id:
        return None
    return cq["id"], cq.get("data") or "", chat_id, message_id
