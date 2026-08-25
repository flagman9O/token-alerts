"""SQLite state: per-network thresholds and the history of what was found.

There is no watchlist any more. Market cap and per-minute volume are filtered
by the API itself, so the bot no longer has to remember tens of thousands of
tokens to work out which ones moved — it asks for the ones that already did.
What is left to keep is the settings the owner changes from the chat or the
`/settings` menu, and the alerts already sent, so the same token does not
arrive twice in a row.

Every threshold lives per chain (each network can be tuned, or turned off, on
its own) except the handful that stay global: the watch/live mode, the
re-alert window, and the liquidity floor — the owner never asked for those to
differ by chain, and adding the option before it is wanted is just more menu
to click through.
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB = Path(__file__).parent / "alerts.db"

CHAINS = ("sol", "bsc", "base", "robinhood", "eth")

GLOBAL_DEFAULTS = {
    "mode": "watch",         # watch = collect quietly, live = send alerts
    "realert_min": "30",     # do not repeat the same token more often
    "liq_min": "0",          # liquidity floor, USD, off by default, all chains
}


def chain_defaults(chain):
    return {
        # Base/RobinHood on by default alongside Solana and BSC, matching
        # what the owner actually asked to be watched; Ethereum is available
        # but off until asked for.
        "enabled": "1" if chain in ("sol", "bsc", "base", "robinhood") else "0",
        "mc_min": "200000",         # market cap, USD
        "vol1m_min": "50000",       # volume over the last minute, USD
        "fees_min": "2400",         # lifetime fees, USD — about 25 SOL
        "fees_currency": "native",  # which unit /settings shows and accepts
        "max_age_h": "0",           # token age cap in hours; 0 = no limit
        "summary": "1",             # Groq + Twitter meme summary
        "risk_filter": "0",         # skip high rug-risk / wash-traded tokens
    }


@contextmanager
def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                chain    TEXT DEFAULT 'sol',
                address  TEXT NOT NULL,
                symbol   TEXT DEFAULT '',
                name     TEXT DEFAULT '',
                ts       INTEGER NOT NULL,
                mc       REAL, vol1m REAL, liq REAL,
                fees_native REAL,
                fees_usd    REAL,
                age_h    REAL,
                holders  INTEGER,
                summary  TEXT DEFAULT '',
                sent     INTEGER DEFAULT 0
            )
        """)

        # The table predates multi-chain support (mint -> address, fees_sol ->
        # fees_native, pools -> holders); top it up in place so old history
        # survives the rewrite.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
        for col, decl in (("chain", "TEXT DEFAULT 'sol'"),
                          ("address", "TEXT"),
                          ("fees_native", "REAL"),
                          ("fees_usd", "REAL"),
                          ("holders", "INTEGER")):
            if col not in have:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {decl}")
        if "mint" in have:
            conn.execute("UPDATE alerts SET address = mint "
                         "WHERE address IS NULL AND mint IS NOT NULL")
        if "fees_sol" in have:
            conn.execute("UPDATE alerts SET fees_native = fees_sol "
                         "WHERE fees_native IS NULL AND fees_sol IS NOT NULL")

        # `mint` carries a NOT NULL from the very first schema, long before
        # `address` existed, and SQLite cannot just drop that constraint —
        # only a full rebuild does it. Data was already copied onto the new
        # columns above, so this is a straight cutover.
        if "mint" in have:
            conn.execute("""
                CREATE TABLE alerts_new (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    chain    TEXT DEFAULT 'sol',
                    address  TEXT NOT NULL,
                    symbol   TEXT DEFAULT '',
                    name     TEXT DEFAULT '',
                    ts       INTEGER NOT NULL,
                    mc       REAL, vol1m REAL, liq REAL,
                    fees_native REAL,
                    fees_usd    REAL,
                    age_h    REAL,
                    holders  INTEGER,
                    summary  TEXT DEFAULT '',
                    sent     INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                INSERT INTO alerts_new (id, chain, address, symbol, name, ts,
                    mc, vol1m, liq, fees_native, fees_usd, age_h, holders,
                    summary, sent)
                SELECT id, chain, COALESCE(address, mint), symbol, name, ts,
                    mc, vol1m, liq, fees_native, fees_usd, age_h, holders,
                    summary, sent
                FROM alerts WHERE COALESCE(address, mint) IS NOT NULL
            """)
            conn.execute("DROP TABLE alerts")
            conn.execute("ALTER TABLE alerts_new RENAME TO alerts")

        conn.execute("CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS alerts_token "
                     "ON alerts(chain, address)")

        # `until` is a unix timestamp when the mute expires, or 0 for the
        # "forever" choice — those are the ones that show up as "удалённые"
        # in each chain's settings screen.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mutes (
                chain      TEXT NOT NULL,
                address    TEXT NOT NULL,
                symbol     TEXT DEFAULT '',
                name       TEXT DEFAULT '',
                until      INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (chain, address)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        def raw(key):
            row = conn.execute("SELECT value FROM settings WHERE key = ?",
                               (key,)).fetchone()
            return row["value"] if row else None

        def set_raw(key, value):
            conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, str(value)))

        version = int(raw("schema_version") or "0")

        # v0 -> v2: fees_min changed from SOL to USD when the official API
        # replaced the homegrown formula — a stored "25" would silently mean
        # $25 instead of 25 SOL, no filter at all. max_age_h=24 was a crutch
        # for the old 24h-window fee estimate, obsolete now that fees cover a
        # token's whole life.
        if version < 2:
            if conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone():
                if raw("fees_min") is not None:
                    set_raw("fees_min", "2400")
                if raw("max_age_h") is not None:
                    set_raw("max_age_h", "0")
            version = 2

        # v2 -> v3: one shared set of thresholds becomes one set per chain,
        # so each network can be tuned, or switched off, independently.
        if version < 3:
            old_mc = raw("mc_min") or "200000"
            old_vol = raw("vol1m_min") or "50000"
            old_fees = raw("fees_min") or "2400"
            old_age = raw("max_age_h") or "0"
            old_enabled = {c.strip() for c in
                           (raw("chains") or "sol,bsc,base,robinhood").split(",")
                           if c.strip()}
            for chain in CHAINS:
                set_raw(f"{chain}.enabled", "1" if chain in old_enabled else "0")
                set_raw(f"{chain}.mc_min", old_mc)
                set_raw(f"{chain}.vol1m_min", old_vol)
                set_raw(f"{chain}.fees_min", old_fees)
                set_raw(f"{chain}.fees_currency", "native")
                set_raw(f"{chain}.max_age_h", old_age)
                set_raw(f"{chain}.summary", "1")
                set_raw(f"{chain}.risk_filter", "0")
            for stale in ("mc_min", "vol1m_min", "fees_min", "max_age_h", "chains"):
                conn.execute("DELETE FROM settings WHERE key = ?", (stale,))
            version = 3

        # v3 -> v4: re-alert window switched from hours to minutes for
        # 5/10/15/30/60-minute granularity. Unlike the fees_min unit change,
        # this conversion needs no external rate and loses nothing, so the
        # owner's actual value carries over instead of being reset.
        if version < 4:
            old_h = raw("realert_h")
            if old_h is not None:
                set_raw("realert_min", float(old_h) * 60)
                conn.execute("DELETE FROM settings WHERE key = 'realert_h'")
            version = 4

        set_raw("schema_version", str(version))
        for field, val in GLOBAL_DEFAULTS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                         (field, val))

        # Left over from the polling-watchlist design; nothing reads it any more.
        conn.execute("DROP TABLE IF EXISTS watch")


# ---------- per-chain settings ----------

def get_chain(chain, field, cast=float):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (f"{chain}.{field}",)).fetchone()
    val = row["value"] if row else chain_defaults(chain)[field]
    if cast is bool:
        return val == "1"
    return val if cast is str else cast(val)


def put_chain(chain, field, value):
    if isinstance(value, bool):
        value = "1" if value else "0"
    with db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (f"{chain}.{field}", str(value)))


def toggle_chain(chain, field):
    new = not get_chain(chain, field, bool)
    put_chain(chain, field, new)
    return new


def chain_config(chain):
    return {
        "enabled": get_chain(chain, "enabled", bool),
        "mc_min": get_chain(chain, "mc_min", float),
        "vol1m_min": get_chain(chain, "vol1m_min", float),
        "fees_min": get_chain(chain, "fees_min", float),
        "fees_currency": get_chain(chain, "fees_currency", str),
        "max_age_h": get_chain(chain, "max_age_h", float),
        "summary": get_chain(chain, "summary", bool),
        "risk_filter": get_chain(chain, "risk_filter", bool),
    }


def enabled_chains():
    return [c for c in CHAINS if get_chain(c, "enabled", bool)]


# ---------- global settings ----------

def get_global(field, cast=float):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (field,)).fetchone()
    val = row["value"] if row else GLOBAL_DEFAULTS[field]
    return val if cast is str else cast(val)


def put_global(field, value):
    with db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (field, str(value)))


def toggle_mode():
    new = "watch" if get_global("mode", str) == "live" else "live"
    put_global("mode", new)
    return new


# ---------- alerts ----------

def recently_alerted(chain, address):
    window = get_global("realert_min") * 60
    cutoff = time.time() - window
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE chain = ? AND address = ? AND ts > ? "
            "LIMIT 1", (chain, address, cutoff)).fetchone()
    return row is not None


def save_alert(t, summary="", sent=0):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (chain, address, symbol, name, ts, mc, vol1m,"
            " liq, fees_native, fees_usd, age_h, holders, summary, sent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.get("chain", "sol"), t["address"], t.get("symbol", ""),
             t.get("name", ""), int(time.time()), t.get("mc"), t.get("vol1m"),
             t.get("liq"), t.get("fees_native"), t.get("fees_usd"),
             t.get("age_h"), t.get("holders"), summary, sent))
        return cur.lastrowid


def mark_sent(alert_id):
    with db() as conn:
        conn.execute("UPDATE alerts SET sent = 1 WHERE id = ?", (alert_id,))


def alert_by_id(alert_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id = ?",
                           (alert_id,)).fetchone()
    return dict(row) if row else None


def alerts_since(ts):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM alerts WHERE ts >= ? ORDER BY ts DESC", (ts,))]


# ---------- mutes ----------

MUTE_DURATIONS = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600, "f": 0}
MUTE_LABELS = {"1h": "на 1 час", "4h": "на 4 часа", "24h": "на 24 часа",
              "f": "навсегда"}


def mute_token(chain, address, symbol, name, duration):
    now = int(time.time())
    until = now + MUTE_DURATIONS[duration] if duration != "f" else 0
    with db() as conn:
        conn.execute(
            "INSERT INTO mutes (chain, address, symbol, name, until, created_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(chain, address) DO UPDATE SET"
            " until = excluded.until, symbol = excluded.symbol,"
            " name = excluded.name, created_at = excluded.created_at",
            (chain, address, symbol or "", name or "", until, now))


def unmute_token(chain, address):
    with db() as conn:
        conn.execute("DELETE FROM mutes WHERE chain = ? AND address = ?",
                     (chain, address))


def is_muted(chain, address):
    now = int(time.time())
    with db() as conn:
        row = conn.execute(
            "SELECT until FROM mutes WHERE chain = ? AND address = ?",
            (chain, address)).fetchone()
        if not row:
            return False
        if row["until"] and row["until"] < now:
            conn.execute("DELETE FROM mutes WHERE chain = ? AND address = ?",
                         (chain, address))
            return False
        return True


def muted_list(chain):
    """Only the "forever" mutes — the ones the owner actually wants to manage
    from the settings menu. Timed mutes expire on their own and never
    accumulate anywhere that needs cleaning up by hand."""
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM mutes WHERE chain = ? AND until = 0"
            " ORDER BY created_at DESC", (chain,))]


def muted_count(chain):
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM mutes WHERE chain = ? AND until = 0",
            (chain,)).fetchone()
    return row["n"]
