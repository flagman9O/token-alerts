"""Where candidates come from.

Migration is no longer a filter, but we still need to know which tokens to
look at — no free API answers "show me everything above $200k cap". So we
keep a rolling watchlist fed from two directions:

  * PumpPortal's websocket, every token created on pump.fun;
  * GeckoTerminal's new pools, which covers the other launchpads too.

Both are free and need no key. Everything they produce lands on the watchlist
and is judged later by the thresholds, whatever launchpad it came from.
"""

import asyncio
import json
import logging

import aiohttp
import websockets

import store

log = logging.getLogger("sources")

PUMP_WS = "wss://pumpportal.fun/api/data"
GECKO = "https://api.geckoterminal.com/api/v2"
UA = {"accept": "application/json", "user-agent": "token-alerts/1.0"}


async def pump_stream(stop):
    """Long-lived listener. Reconnects on its own — the socket drops often."""
    backoff = 2
    while not stop.is_set():
        try:
            async with websockets.connect(PUMP_WS, open_timeout=20,
                                          ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                backoff = 2
                log.info("pumpportal: подключено")
                batch = []
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        if batch:
                            store.add_candidates(batch)
                            batch.clear()
                        continue
                    d = json.loads(raw)
                    mint = d.get("mint")
                    if not mint:
                        continue
                    batch.append((mint, d.get("symbol") or "",
                                  d.get("name") or "", "pump"))
                    # Write in chunks; the stream can burst.
                    if len(batch) >= 25:
                        store.add_candidates(batch)
                        batch.clear()
        except Exception as e:
            log.warning("pumpportal оборвался (%s), переподключаюсь через %ss",
                        type(e).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def gecko_new_pools(session, pages=2):
    """New pools across Solana — catches launchpads other than pump.fun."""
    found = []
    for page in range(1, pages + 1):
        url = f"{GECKO}/networks/solana/new_pools?page={page}"
        try:
            async with session.get(url, headers=UA,
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    if r.status == 429:
                        log.info("geckoterminal: лимит, пропускаю круг")
                    return found
                d = await r.json(content_type=None)
        except Exception as e:
            log.warning("geckoterminal: %s", type(e).__name__)
            return found

        for item in d.get("data", []):
            base = (((item.get("relationships") or {}).get("base_token") or {})
                    .get("data") or {}).get("id", "")
            # ids look like "solana_<mint>"
            mint = base.split("_", 1)[1] if "_" in base else ""
            if not mint:
                continue
            name = (item.get("attributes") or {}).get("name") or ""
            symbol = name.split("/")[0].strip() if "/" in name else ""
            found.append((mint, symbol, name, "gecko"))
        await asyncio.sleep(2.5)  # stay far inside 30 requests per minute

    if found:
        store.add_candidates(found)
    return found
