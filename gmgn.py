"""Official GMGN OpenAPI client.

Read-only routes need the API key in a header plus a timestamp and a random
client id in the query — no request signing. Signing with the Ed25519 key is
only required for trading, which this bot does not do and must not be able to.

Two calls carry the whole bot. `rank` returns the trending list for a chain and
applies the market-cap and volume thresholds server-side, so one request covers
two of the three criteria. `token_info` fills in the third: `total_fee`, the
lifetime fees across every pool — the same number gmgn shows as "Total Fees".
"""

import asyncio
import logging
import time
import uuid

import aiohttp

import config

log = logging.getLogger("gmgn")

HOST = "https://openapi.gmgn.ai"
UA = "gmgn-cli/1.5.8"

# Fees are denominated in the chain's own coin, so a threshold in dollars has
# to be converted per chain before it means anything.
NATIVE = {
    "sol": "SOL",
    "bsc": "BNB",
    "base": "ETH",
    "eth": "ETH",
    "robinhood": "ETH",
}

CHAIN_NAMES = {
    "sol": "Solana",
    "bsc": "BSC",
    "base": "Base",
    "eth": "Ethereum",
    "robinhood": "RobinHood",
}

BINANCE = "https://api.binance.com/api/v3/ticker/price"


class Rate:
    """Leaky bucket on their side allows 20/s; we stay well under it."""

    def __init__(self, per_second=6):
        self.gap = 1.0 / per_second
        self.last = 0.0
        self.blocked_until = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            if now < self.blocked_until:
                await asyncio.sleep(self.blocked_until - now)
            delay = self.gap - (time.monotonic() - self.last)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()

    def back_off(self, seconds):
        """Retrying during a rate-limit ban extends it, so we simply stop."""
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


limiter = Rate()


def _num(v, default=0.0):
    """The API sends numbers as strings often enough to be worth centralising."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _unwrap(payload, key=None):
    """Peel the envelope. Some routes wrap the body twice, some once."""
    seen = 0
    while isinstance(payload, dict) and "data" in payload and seen < 3:
        if key and key in payload:
            break
        payload = payload["data"]
        seen += 1
    if key and isinstance(payload, dict):
        return payload.get(key)
    return payload


async def _get(session, path, params):
    key = config.gmgn_key()
    if not key:
        log.error("не задан ключ gmgn — см. config.py")
        return None

    await limiter.wait()
    query = dict(params)
    query["timestamp"] = int(time.time())
    query["client_id"] = str(uuid.uuid4())
    try:
        async with session.get(
                f"{HOST}{path}", params=query,
                headers={"X-APIKEY": key, "User-Agent": UA},
                timeout=aiohttp.ClientTimeout(total=25)) as r:
            body = await r.json(content_type=None)
            if r.status == 429:
                reset = _num((body or {}).get("reset_at"))
                wait = max(10.0, reset - time.time()) if reset else 60.0
                log.warning("лимит запросов, пауза %.0f с", wait)
                limiter.back_off(min(wait, 300))
                return None
            if r.status != 200 or (body or {}).get("code") not in (0, None):
                log.warning("gmgn %s: HTTP %s %s", path, r.status,
                            (body or {}).get("message") or "")
                return None
            return body
    except Exception as e:
        log.warning("gmgn %s: %s", path, type(e).__name__)
        return None


async def rank(session, chain, *, mc_min=0, vol_min=0, interval="1m", limit=100):
    """Trending tokens, with both numeric thresholds applied server-side.

    Sorted by volume, so with `interval=1m` the top of the list is exactly
    what traded hardest in the last minute.
    """
    params = {
        "chain": chain,
        "interval": interval,
        "orderby": "volume",
        "direction": "desc",
        "limit": limit,
    }
    if mc_min:
        params["min_marketcap"] = int(mc_min)
    if vol_min:
        params["min_volume"] = int(vol_min)

    body = await _get(session, "/v1/market/rank", params)
    rows = _unwrap(body, "rank") or []
    out = []
    for t in rows:
        addr = t.get("address")
        if not addr:
            continue
        out.append({
            "chain": chain,
            "address": addr,
            "symbol": t.get("symbol") or "",
            "name": t.get("name") or "",
            "mc": _num(t.get("market_cap")),
            "vol1m": _num(t.get("volume")),
            "liq": _num(t.get("liquidity")),
            "created": int(_num(t.get("creation_timestamp"))),
            "holders": int(_num(t.get("holder_count"))),
            "rug_ratio": _num(t.get("rug_ratio")),
            "smart": int(_num(t.get("smart_degen_count"))),
            "renowned": int(_num(t.get("renowned_count"))),
            "bundler": _num(t.get("bundler_rate")),
            "wash": bool(t.get("is_wash_trading")),
            "top10": _num(t.get("top_10_holder_rate")),
            "dev_hold": _num(t.get("dev_team_hold_rate")),
            "platform": t.get("launchpad_platform") or "",
            "twitter": t.get("twitter_username") or "",
            "website": t.get("website") or "",
            "telegram": t.get("telegram") or "",
        })
    return out


async def token_info(session, chain, address):
    """Lifetime fees and the social links, straight from the token page."""
    body = await _get(session, "/v1/token/info",
                      {"chain": chain, "address": address})
    d = _unwrap(body)
    if not isinstance(d, dict) or not d.get("address"):
        return None

    price = d.get("price") or {}
    link = d.get("link") or {}
    return {
        "symbol": d.get("symbol") or "",
        "name": d.get("name") or "",
        "total_fee": _num(d.get("total_fee")),
        "vol1m": _num(price.get("volume_1m")),
        "vol24": _num(price.get("volume_24h")),
        "swaps1m": int(_num(price.get("swaps_1m"))),
        "liq": _num(d.get("liquidity")),
        "mc": _num(price.get("price")) * _num(d.get("circulating_supply")),
        "twitter": link.get("twitter_username") or "",
        "website": link.get("website") or "",
        "telegram": link.get("telegram") or "",
        "description": (link.get("description") or "")[:600],
    }


async def native_prices(session):
    """USD price of every coin fees can be denominated in."""
    out = {}
    for sym in sorted(set(NATIVE.values())):
        try:
            async with session.get(BINANCE, params={"symbol": f"{sym}USDT"},
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                d = await r.json(content_type=None)
                price = _num((d or {}).get("price"))
                if price:
                    out[sym] = price
        except Exception:
            continue
    return out


_price_cache = {"ts": 0.0, "data": {}}


async def cached_native_prices(session, max_age=120):
    """Same as `native_prices`, but shared and rate-limited across callers.

    The scanner needs this every pass and the settings menu needs it whenever
    it shows a fee threshold — no reason for each to hit Binance on its own.
    """
    now = time.time()
    if now - _price_cache["ts"] > max_age or not _price_cache["data"]:
        fresh = await native_prices(session)
        if fresh:
            _price_cache["data"] = fresh
            _price_cache["ts"] = now
    return _price_cache["data"]


def fees_usd(chain, total_fee, prices):
    """Fees converted to dollars so one threshold works on every chain."""
    return total_fee * prices.get(NATIVE.get(chain, ""), 0.0)


def twitter_url(raw):
    """The field holds a bare handle on some routes and a full link on others."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"https://x.com/{raw.lstrip('@')}"


def age_hours(created_ts):
    if not created_ts:
        return None
    return max(0.0, (time.time() - created_ts) / 3600)
