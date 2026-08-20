"""Token alert bot.

Watches Solana tokens and pings Telegram when one crosses every threshold at
once: market cap, volume over the last minute, and lifetime fees — all summed
across every pool the token trades in, the way gmgn counts them.

Runs two modes. In "watch" it stays quiet and collects, sending one digest a
day, which is how thresholds get calibrated on real data. In "live" it sends
alerts as they happen.
"""

import asyncio
import logging
import time

import aiohttp

import market
import sources
import store
import summary
import telegram as tg

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("bot")

SCAN_EVERY = 60          # seconds between full passes over the watchlist
GECKO_EVERY = 180        # new-pool sweep, kept rare to respect its limits
BATCH = 30               # tokens per screening call


# ---------- formatting ----------

def money(v):
    if v is None:
        return "—"
    v = float(v)
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def links(mint):
    return (f'<a href="https://gmgn.ai/sol/token/{mint}">gmgn</a> · '
            f'<a href="https://v2.bubblemaps.io/map?address={mint}&chain=solana">bubblemaps</a> · '
            f'<a href="https://dexscreener.com/solana/{mint}">dexscreener</a>')


def card(t, text=""):
    age = t.get("age_h")
    age_s = "—" if age is None else (f"{age:.0f} ч" if age >= 1 else f"{age*60:.0f} мин")
    head = f"🔥 <b>{t.get('symbol') or '?'}</b>"
    if t.get("name") and t["name"] != t.get("symbol"):
        head += f" · {t['name'][:40]}"

    body = [
        head, "",
        f"Капитализация   <b>{money(t.get('mc'))}</b>",
        f"Объём за 1 мин  <b>{money(t.get('vol1m'))}</b>",
        f"Комиссии всего  <b>{t.get('fees_sol', 0):,.0f} SOL</b> ({t.get('pools', 0)} пулов)",
        f"Ликвидность     {money(t.get('liq'))}",
        f"Возраст         {age_s}",
    ]
    if text:
        body += ["", f"<i>{text}</i>"]
    meta = t.get("meta") or {}
    extra = []
    if meta.get("twitter"):
        extra.append(f'<a href="{meta["twitter"]}">twitter</a>')
    if meta.get("website"):
        extra.append(f'<a href="{meta["website"]}">сайт</a>')
    body += ["", links(t["mint"]) + ("" if not extra else " · " + " · ".join(extra))]
    body += [f"<code>{t['mint']}</code>"]
    return "\n".join(body)


# ---------- scanning ----------

async def scan_once(session, sol_usd):
    """One pass: screen the watchlist cheaply, then look closely at survivors."""
    cfg = store.get_all()
    mc_min = float(cfg["mc_min"])
    vol_min = float(cfg["vol1m_min"])
    fees_min = float(cfg["fees_min"])
    liq_min = float(cfg["liq_min"])
    max_age = float(cfg["max_age_h"])

    rows = store.due_for_check()
    if not rows:
        return []
    by_mint = {r["mint"]: r for r in rows}

    # Stage one: market cap for everything, thirty at a time.
    passed = []
    for i in range(0, len(rows), BATCH):
        chunk = [r["mint"] for r in rows[i:i + BATCH]]
        info = await market.screen(session, chunk)
        for mint in chunk:
            got = info.get(mint)
            if not got:
                store.touch(mint)
                continue
            if got["mc"] >= mc_min:
                passed.append((mint, got))
            else:
                store.touch(mint)

    # Stage two: full picture across all pools, only for those worth it.
    hits = []
    for mint, got in passed:
        det = await market.detail(session, mint)
        if not det:
            store.touch(mint)
            continue

        prev = by_mint[mint]
        vol1m = market.per_minute_volume(prev["last_vol24"], prev["last_ts"],
                                         det["vol24"])
        fees = market.fees_sol(det["vol24"], sol_usd)
        age = market.age_hours(det["pair_created"])

        store.record_check(mint, vol24=det["vol24"], mc=det["mc"],
                           liq=det["liq"], vol1m=vol1m or 0, fees_sol=fees,
                           pair_created=det["pair_created"])

        if vol1m is None:
            continue          # first sighting: nothing to compare against yet
        if age is not None and age > max_age:
            continue
        if (det["mc"] >= mc_min and vol1m >= vol_min
                and fees >= fees_min and det["liq"] >= liq_min):
            if store.recently_alerted(mint):
                continue
            hits.append({**det, "vol1m": vol1m, "fees_sol": fees,
                         "age_h": age,
                         "symbol": det["symbol"] or prev["symbol"],
                         "name": det["name"] or prev["name"]})
    return hits


async def handle_hit(session, hit, live):
    meta = await summary.token_meta(session, hit["mint"])
    hit["meta"] = meta
    text = await summary.describe(session, hit.get("name"), hit.get("symbol"), meta)
    sent = 0
    if live:
        sent = 1 if await tg.send(session, card(hit, text)) else 0
    store.save_alert(hit, summary=text, sent=sent)
    log.info("находка: %s mc=%.0f vol1m=%.0f fees=%.0f%s",
             hit.get("symbol"), hit.get("mc"), hit.get("vol1m"),
             hit.get("fees_sol"), "" if live else " (тихий режим)")


# ---------- loops ----------

async def scanner(session, stop):
    sol = await market.sol_price(session) or 0
    last_price = time.time()
    while not stop.is_set():
        started = time.time()
        try:
            if time.time() - last_price > 300:
                sol = await market.sol_price(session) or sol
                last_price = time.time()
            live = store.get("mode", str) == "live"
            for hit in await scan_once(session, sol):
                await handle_hit(session, hit, live)
            store.prune()
        except Exception as e:
            log.exception("сбой прохода: %s", e)
        await asyncio.sleep(max(5, SCAN_EVERY - (time.time() - started)))


async def gecko_loop(session, stop):
    while not stop.is_set():
        try:
            await sources.gecko_new_pools(session)
        except Exception as e:
            log.warning("gecko: %s", type(e).__name__)
        await asyncio.sleep(GECKO_EVERY)


async def commands(session, stop):
    offset = 0
    while not stop.is_set():
        ups, offset = await tg.get_updates(session, offset)
        for u in ups:
            text = tg.message_text(u)
            if text:
                await reply(session, text)


async def reply(session, text):
    parts = text.split()
    cmd = parts[0].lower().lstrip("/")

    if cmd in ("start", "help"):
        await tg.send(session,
            "Сканер токенов на связи.\n\n"
            "/status — пороги и статистика\n"
            "/set ключ значение — сменить порог\n"
            "/mode watch|live — тихий режим или алерты\n"
            "/last — последние находки\n\n"
            "Ключи: mc_min, vol1m_min, fees_min, liq_min, max_age_h, realert_h")

    elif cmd == "status":
        cfg = store.get_all()
        day = store.alerts_since(int(time.time()) - 86400)
        await tg.send(session,
            f"<b>Режим:</b> {cfg['mode']}\n"
            f"<b>Под наблюдением:</b> {store.watch_size()} токенов\n"
            f"<b>Находок за сутки:</b> {len(day)}\n\n"
            f"Капитализация ≥ {money(float(cfg['mc_min']))}\n"
            f"Объём за 1 мин ≥ {money(float(cfg['vol1m_min']))}\n"
            f"Комиссии ≥ {cfg['fees_min']} SOL\n"
            f"Ликвидность ≥ {money(float(cfg['liq_min']))}\n"
            f"Возраст ≤ {cfg['max_age_h']} ч\n"
            f"Повтор не чаще {cfg['realert_h']} ч")

    elif cmd == "set" and len(parts) == 3:
        key, val = parts[1], parts[2]
        if key not in store.DEFAULTS:
            await tg.send(session, f"Не знаю ключ {key}")
            return
        try:
            float(val)
        except ValueError:
            await tg.send(session, "Значение должно быть числом")
            return
        store.put(key, val)
        await tg.send(session, f"{key} = {val}")

    elif cmd == "mode" and len(parts) == 2 and parts[1] in ("watch", "live"):
        store.put("mode", parts[1])
        await tg.send(session, "Режим: " + parts[1] +
                      (" — присылаю находки сразу" if parts[1] == "live"
                       else " — коплю тихо, сводка раз в сутки"))

    elif cmd == "last":
        rows = store.alerts_since(int(time.time()) - 86400)[:10]
        if not rows:
            await tg.send(session, "За сутки находок не было")
            return
        lines = [f"{r['symbol'] or '?'} · {money(r['mc'])} · "
                 f"объём {money(r['vol1m'])} · {r['fees_sol']:,.0f} SOL"
                 for r in rows]
        await tg.send(session, "<b>Находки за сутки</b>\n" + "\n".join(lines))

    else:
        await tg.send(session, "Не понял. /help")


async def digest(session, stop):
    """One summary a day while in watch mode."""
    while not stop.is_set():
        await asyncio.sleep(3600)
        if store.get("mode", str) != "watch":
            continue
        now = int(time.time())
        if time.localtime(now).tm_hour != 10:
            continue
        rows = store.alerts_since(now - 86400)
        if not rows:
            await tg.send(session, "За сутки под пороги никто не подошёл. "
                                   "Наблюдаю дальше.")
            continue
        lines = [f"{r['symbol'] or '?'} · капа {money(r['mc'])} · "
                 f"объём/мин {money(r['vol1m'])} · {r['fees_sol']:,.0f} SOL"
                 for r in rows[:15]]
        await tg.send(session,
            f"<b>Сводка за сутки: {len(rows)} находок</b>\n\n" + "\n".join(lines) +
            "\n\nВключить уведомления сразу: /mode live")


async def main():
    store.init()
    stop = asyncio.Event()
    log.info("старт, под наблюдением %s токенов", store.watch_size())
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            sources.pump_stream(stop),
            gecko_loop(session, stop),
            scanner(session, stop),
            commands(session, stop),
            digest(session, stop),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
