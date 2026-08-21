"""SQLite state: the watchlist, volume snapshots, alert history and settings.

One file next to the code, same approach as the tracker. Every token we hear
about goes on the watchlist and stays there for a day; each poll overwrites its
last volume reading, which is what lets us derive per-minute volume.
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB = Path(__file__).parent / "alerts.db"

# Thresholds the owner can change from the chat. Values are strings so that
# one settings table can hold both numbers and switches.
DEFAULTS = {
    "mc_min": "200000",        # market cap, USD
    "vol1m_min": "50000",      # volume over the last minute, USD, all pools
    "fees_min": "25",          # lifetime fees, SOL, all pools
    "liq_min": "0",            # liquidity floor, USD, off by default
    "max_age_h": "24",         # only tokens younger than this
    "mode": "watch",           # watch = collect quietly, live = send alerts
    "realert_h": "6",          # do not repeat the same token more often
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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watch (
                mint        TEXT PRIMARY KEY,
                symbol      TEXT DEFAULT '',
                name        TEXT DEFAULT '',
                source      TEXT DEFAULT '',
                first_seen  INTEGER NOT NULL,
                pair_created INTEGER DEFAULT 0,
                last_ts     INTEGER DEFAULT 0,
                last_vol24  REAL DEFAULT 0,
                mc          REAL DEFAULT 0,
                liq         REAL DEFAULT 0,
                vol1m       REAL DEFAULT 0,
                fees_sol    REAL DEFAULT 0,
                alerted_at  INTEGER DEFAULT 0,
                checks      INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS watch_seen ON watch(first_seen);

            CREATE TABLE IF NOT EXISTS alerts (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                mint     TEXT NOT NULL,
                symbol   TEXT DEFAULT '',
                name     TEXT DEFAULT '',
                ts       INTEGER NOT NULL,
                mc       REAL, vol1m REAL, fees_sol REAL, liq REAL,
                age_h    REAL,
                pools    INTEGER,
                summary  TEXT DEFAULT '',
                sent     INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS alerts_ts ON alerts(ts);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        for k, v in DEFAULTS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                         (k, v))


# ---------- settings ----------

def get_all():
    with db() as conn:
        return {r["key"]: r["value"]
                for r in conn.execute("SELECT key, value FROM settings")}


def get(key, cast=float):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (key,)).fetchone()
    val = row["value"] if row else DEFAULTS.get(key, "0")
    return val if cast is str else cast(val)


def put(key, value):
    with db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))


# ---------- watchlist ----------

def add_candidates(rows):
    """rows: iterable of (mint, symbol, name, source). Existing ones are kept."""
    now = int(time.time())
    with db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO watch (mint, symbol, name, source, first_seen)"
            " VALUES (?,?,?,?,?)",
            [(m, s or "", n or "", src or "", now) for m, s, n, src in rows])


def hot_list():
    """Tokens already known to clear the market cap bar.

    These are the only ones worth a detailed look every single minute — there
    are a couple of hundred of them against tens of thousands on the list.
    """
    mc_min = get("mc_min")
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM watch WHERE mc >= ? ORDER BY last_ts ASC", (mc_min,))]


def cold_batch(limit):
    """Everything else, least recently screened first, newest breaking ties.

    Fresh arrivals matter more than tokens that have been quiet for hours, so
    among equally stale entries the younger one goes first.
    """
    mc_min = get("mc_min")
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM watch WHERE mc < ? "
            "ORDER BY last_ts ASC, first_seen DESC LIMIT ?", (mc_min, limit))]


def record_screen(mint, mc):
    """Cheap pass result. Keeping the cap for everyone is what lets us tell a
    dead token from one we simply have not looked at yet."""
    with db() as conn:
        conn.execute("UPDATE watch SET last_ts=?, mc=?, checks=checks+1 "
                     "WHERE mint=?", (int(time.time()), mc, mint))


def drop_dead(min_checks=3, min_age_s=1800):
    """Forget tokens that never got anywhere.

    A token screened a few times, older than half an hour and still far below
    the bar is not coming back — and keeping it starves the ones that matter.
    """
    mc_min = get("mc_min")
    cutoff = int(time.time()) - min_age_s
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM watch WHERE checks >= ? AND first_seen < ? "
            "AND mc < ? AND alerted_at = 0",
            (min_checks, cutoff, mc_min * 0.5))
        return cur.rowcount


def record_check(mint, *, vol24, mc, liq, vol1m, fees_sol, pair_created=None):
    now = int(time.time())
    with db() as conn:
        if pair_created is None:
            conn.execute(
                "UPDATE watch SET last_ts=?, last_vol24=?, mc=?, liq=?, vol1m=?,"
                " fees_sol=?, checks=checks+1 WHERE mint=?",
                (now, vol24, mc, liq, vol1m, fees_sol, mint))
        else:
            conn.execute(
                "UPDATE watch SET last_ts=?, last_vol24=?, mc=?, liq=?, vol1m=?,"
                " fees_sol=?, checks=checks+1, pair_created=? WHERE mint=?",
                (now, vol24, mc, liq, vol1m, fees_sol, pair_created, mint))


def touch(mint):
    """Mark as checked without new numbers, so it moves to the back of the queue."""
    with db() as conn:
        conn.execute("UPDATE watch SET last_ts=?, checks=checks+1 WHERE mint=?",
                     (int(time.time()), mint))


def prune():
    """Drop entries past the age window. Returns how many went."""
    cutoff = int(time.time()) - int(get("max_age_h") * 3600)
    with db() as conn:
        cur = conn.execute("DELETE FROM watch WHERE first_seen < ?", (cutoff,))
        return cur.rowcount


def watch_size():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) c FROM watch").fetchone()["c"]


# ---------- alerts ----------

def recently_alerted(mint):
    window = get("realert_h") * 3600
    with db() as conn:
        row = conn.execute("SELECT alerted_at FROM watch WHERE mint = ?",
                           (mint,)).fetchone()
    return bool(row and row["alerted_at"] and
                time.time() - row["alerted_at"] < window)


def save_alert(t, summary="", sent=0):
    now = int(time.time())
    with db() as conn:
        conn.execute(
            "INSERT INTO alerts (mint, symbol, name, ts, mc, vol1m, fees_sol,"
            " liq, age_h, pools, summary, sent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["mint"], t.get("symbol", ""), t.get("name", ""), now,
             t.get("mc"), t.get("vol1m"), t.get("fees_sol"), t.get("liq"),
             t.get("age_h"), t.get("pools"), summary, sent))
        conn.execute("UPDATE watch SET alerted_at = ? WHERE mint = ?",
                     (now, t["mint"]))


def alerts_since(ts):
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM alerts WHERE ts >= ? ORDER BY ts DESC", (ts,))]
