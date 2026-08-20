"""Market data from DexScreener, aggregated across every pool of a token.

Two stages on purpose. Screening asks about thirty tokens at once and only
looks at market cap — cheap enough to run over the whole watchlist every
minute. Only what survives that gets the detailed call, which costs one
request per token but returns all of its pools.

Per-minute volume is a derived number: for a token younger than a day the
24h window covers its whole life, so the difference between two readings is
exactly what traded in between.
"""

import asyncio
import time

import aiohttp

DEX = "https://api.dexscreener.com"
UA = {"user-agent": "Mozilla/5.0", "accept": "application/json"}

# Trading fee actually reflected in gmgn's "Total Fees". Derived from three
# tokens the owner supplied: MADE came out at 0.239%, STONK and TOAD are
# consistent with the same rate. The "Taxes: Dex %" shown on their page is the
# swap fee of the current pool, a different thing entirely.
FEE_RATE = 0.0025

SOL_USDC_PAIR = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"


class Rate:
    """Small spacing between calls so we stay well inside the free limits."""

    def __init__(self, per_minute):
        self.gap = 60.0 / per_minute
        self.last = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            delay = self.gap - (time.monotonic() - self.last)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()


limiter = Rate(240)


async def _get(session, url):
    await limiter.wait()
    try:
        async with session.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)
    except Exception:
        return None


async def sol_price(session):
    d = await _get(session, f"{DEX}/latest/dex/pairs/solana/{SOL_USDC_PAIR}")
    if not d:
        return None
    pair = d.get("pair") or (d.get("pairs") or [{}])[0]
    try:
        return float(pair.get("priceUsd") or 0) or None
    except (TypeError, ValueError):
        return None


async def screen(session, mints):
    """Cheap pass: market cap and symbol for up to 30 tokens in one call."""
    if not mints:
        return {}
    d = await _get(session, f"{DEX}/tokens/v1/solana/{','.join(mints)}")
    if not d:
        return {}
    out = {}
    for p in d:
        base = p.get("baseToken") or {}
        addr = base.get("address")
        if not addr:
            continue
        mc = p.get("marketCap") or p.get("fdv") or 0
        # A token may appear more than once; keep the largest reading.
        if addr not in out or mc > out[addr]["mc"]:
            out[addr] = {
                "mc": float(mc or 0),
                "symbol": base.get("symbol") or "",
                "name": base.get("name") or "",
                "pair_created": p.get("pairCreatedAt") or 0,
            }
    return out


async def detail(session, mint):
    """Everything about one token, summed over all of its pools."""
    d = await _get(session, f"{DEX}/token-pairs/v1/solana/{mint}")
    if not d:
        return None

    vol24 = liq = 0.0
    mc = 0.0
    oldest = None
    symbol = name = ""
    for p in d:
        vol24 += float((p.get("volume") or {}).get("h24") or 0)
        liq += float((p.get("liquidity") or {}).get("usd") or 0)
        this_mc = float(p.get("marketCap") or p.get("fdv") or 0)
        mc = max(mc, this_mc)
        created = p.get("pairCreatedAt") or 0
        if created and (oldest is None or created < oldest):
            oldest = created
        base = p.get("baseToken") or {}
        if base.get("address") == mint:
            symbol = symbol or (base.get("symbol") or "")
            name = name or (base.get("name") or "")

    return {
        "mint": mint,
        "symbol": symbol,
        "name": name,
        "vol24": vol24,
        "liq": liq,
        "mc": mc,
        "pools": len(d),
        "pair_created": oldest or 0,
    }


def per_minute_volume(prev_vol24, prev_ts, vol24, now=None):
    """Volume traded since the previous reading, scaled to one minute.

    Only meaningful while the token is younger than the 24h window, which is
    the case for everything we watch. A negative delta means the window has
    started sliding, so we report nothing rather than a wrong number.
    """
    if not prev_ts or prev_vol24 <= 0:
        return None
    gap = (now or time.time()) - prev_ts
    if gap < 20:
        return None
    delta = vol24 - prev_vol24
    if delta < 0:
        return None
    return delta * 60.0 / gap


def fees_sol(vol24, sol_usd):
    """Lifetime fees in SOL, the number gmgn shows as Total Fees."""
    if not sol_usd:
        return 0.0
    return vol24 * FEE_RATE / sol_usd


def age_hours(pair_created_ms):
    if not pair_created_ms:
        return None
    return (time.time() * 1000 - pair_created_ms) / 3600000
