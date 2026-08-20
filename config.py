"""Where secrets and identity come from.

Nothing personal belongs in the source. Values are read from environment
variables, falling back to files in a config directory, so the same code runs
on anyone's machine.

    TOKEN_ALERTS_HOME   directory with the files below, default ~/.token-alerts
    TG_BOT_TOKEN        or file  bot-token     — from @BotFather
    TG_OWNER_ID         or file  owner-id      — numeric chat id to notify
    GROQ_API_KEY        or file  groq-key      — optional, enables summaries
"""

import os
from pathlib import Path

HOME = Path(os.getenv("TOKEN_ALERTS_HOME", Path.home() / ".token-alerts"))


def _read(env, filename):
    val = os.getenv(env)
    if val:
        return val.strip()
    try:
        return (HOME / filename).read_text().strip()
    except OSError:
        return ""


def bot_token():
    return _read("TG_BOT_TOKEN", "bot-token")


def owner_id():
    raw = _read("TG_OWNER_ID", "owner-id")
    try:
        return int(raw)
    except ValueError:
        return 0


def groq_key():
    return _read("GROQ_API_KEY", "groq-key")
