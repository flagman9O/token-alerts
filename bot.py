"""Token alert bot.

Watches several chains through the official GMGN API and pings Telegram when a
token crosses every threshold at once: market cap, volume over the last minute
and lifetime fees — all counted across every pool, the way gmgn counts them.
Every threshold is per chain (see store.py), configurable from the chat
either as a menu (/settings) or as text commands.

The first two thresholds are applied by the API itself, so a pass costs one
request per chain. Only the survivors of that filter — usually none, sometimes
a handful — are looked up individually to check their fees.

Runs two modes. In "watch" it stays quiet and collects, sending one digest a
day, which is how thresholds get calibrated on real data. In "live" it sends
alerts as they happen.
"""

import asyncio
import logging
import time

import aiohttp

import fmt
import gmgn
import menu
import store
import summary
import telegram as tg

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("bot")

SCAN_EVERY = 30          # seconds between passes

# Bubblemaps has its own chain slugs and does not cover every network.
BUBBLE = {"sol": "solana", "bsc": "bsc", "base": "base", "eth": "eth",
         "robinhood": "robinhood"}


# ---------- formatting ----------

def links(chain, address):
    out = [f'<a href="https://gmgn.ai/{chain}/token/{address}">gmgn</a>']
    slug = BUBBLE.get(chain)
    if slug:
        out.append(f'<a href="https://v2.bubblemaps.io/map?address={address}'
                   f'&chain={slug}">bubblemaps</a>')
    return " · ".join(out)


def card(t, text=""):
    age = t.get("age_h")
    if age is None:
        age_s = "—"
    elif age >= 24:
        age_s = f"{age/24:.0f} дн"
    elif age >= 1:
        age_s = f"{age:.0f} ч"
    else:
        age_s = f"{age*60:.0f} мин"

    head = f"🔥 <b>{t.get('symbol') or '?'}</b>"
    if t.get("name") and t["name"] != t.get("symbol"):
        head += f" · {t['name'][:40]}"
    head += f"  <i>{gmgn.CHAIN_NAMES.get(t['chain'], t['chain'])}</i>"

    coin = gmgn.NATIVE.get(t["chain"], "")
    body = [
        head, "",
        f"Капитализация   <b>{fmt.money(t.get('mc'))}</b>",
        f"Объём за {store.VOL_INTERVAL_LABELS.get(t.get('interval'), '1 мин')}  "
        f"<b>{fmt.money(t.get('vol1m'))}</b>",
        f"Комиссии всего  <b>{t.get('fees_native', 0):,.1f} {coin}</b>"
        f" ({fmt.money(t.get('fees_usd'))})",
        f"Ликвидность     {fmt.money(t.get('liq'))}",
        f"Возраст         {age_s}",
    ]
    if t.get("holders"):
        body.append(f"Держателей      {t['holders']:,}")

    flags = []
    if t.get("smart"):
        flags.append(f"смарт-мани {t['smart']}")
    if t.get("renowned"):
        flags.append(f"KOL {t['renowned']}")
    if t.get("rug_ratio", 0) > 0.3:
        flags.append(f"⚠️ риск рага {t['rug_ratio']:.0%}")
    if t.get("wash"):
        flags.append("⚠️ накрутка объёма")
    if t.get("bundler", 0) > 0.3:
        flags.append(f"⚠️ бандлеры {t['bundler']:.0%}")
    if t.get("top10", 0) > 0.5:
        flags.append(f"⚠️ топ-10 держат {t['top10']:.0%}")
    if flags:
        body += ["", " · ".join(flags)]

    if text:
        body += ["", f"<i>{text}</i>"]

    extra = []
    tw = gmgn.twitter_url(t.get("twitter"))
    if tw:
        extra.append(f'<a href="{tw}">twitter</a>')
    if t.get("telegram"):
        extra.append(f'<a href="{t["telegram"]}">telegram</a>')
    if t.get("website"):
        extra.append(f'<a href="{t["website"]}">сайт</a>')
    body += ["", links(t["chain"], t["address"]) +
             ("" if not extra else " · " + " · ".join(extra))]
    body += [f"<code>{t['address']}</code>"]
    return "\n".join(body)


# ---------- scanning ----------

async def scan_chain(session, chain, cfg, prices):
    """One request for the shortlist, then one per candidate for its fees.

    `cfg` is a chain's settings from `store.chain_config` — the caller
    decides whether the chain is enabled; this function will happily scan a
    disabled one too, since that is exactly what "проверить сейчас" needs to
    preview a chain before switching it on.
    """
    rows = await gmgn.rank(session, chain, mc_min=cfg["mc_min"],
                           vol_min=cfg["vol1m_min"], interval=cfg["vol_interval"])
    liq_min = store.get_global("liq_min")
    hits = []
    for t in rows:
        age = gmgn.age_hours(t["created"])
        if cfg["max_age_h"] > 0 and age is not None and age > cfg["max_age_h"]:
            continue
        if t["liq"] < liq_min:
            continue
        if cfg["risk_filter"] and (t["rug_ratio"] > 0.3 or t["wash"]
                                   or t["bundler"] > 0.3):
            continue
        if store.recently_alerted(chain, t["address"]):
            continue
        if store.is_muted(chain, t["address"]):
            continue

        info = await gmgn.token_info(session, chain, t["address"])
        if not info:
            continue
        fees_usd = gmgn.fees_usd(chain, info["total_fee"], prices)
        if fees_usd < cfg["fees_min"]:
            continue

        hits.append({**t,
                     "age_h": age,
                     "fees_native": info["total_fee"],
                     "fees_usd": fees_usd,
                     "summary_enabled": cfg["summary"],
                     "symbol": t["symbol"] or info["symbol"],
                     "name": t["name"] or info["name"],
                     "twitter": t["twitter"] or info["twitter"],
                     "telegram": t["telegram"] or info["telegram"],
                     "website": t["website"] or info["website"],
                     "description": info["description"]})
    return hits


MUTE_BUTTONS = (("1h", "1ч"), ("4h", "4ч"), ("24h", "24ч"), ("f", "навсегда"))


def mute_kb(alert_id):
    return {"inline_keyboard": [[
        {"text": f"🔇 {label}", "callback_data": f"mute:{alert_id}:{code}"}
        for code, label in MUTE_BUTTONS
    ]]}


async def handle_hit(session, hit, live):
    text = ""
    if hit.get("summary_enabled", True):
        text = await summary.describe(session, hit.get("name"), hit.get("symbol"),
                                      hit.get("description", ""),
                                      gmgn.twitter_url(hit.get("twitter")))
    alert_id = store.save_alert(hit, summary=text, sent=0)
    sent = 0
    if live:
        if await tg.send(session, card(hit, text), reply_markup=mute_kb(alert_id)):
            sent = 1
            store.mark_sent(alert_id)
    log.info("находка: %s/%s mc=%.0f vol1m=%.0f комиссии=$%.0f%s",
             hit["chain"], hit.get("symbol"), hit.get("mc"), hit.get("vol1m"),
             hit.get("fees_usd"), "" if live else " (тихий режим)")


async def manual_scan(session, chain):
    """"Проверить сейчас" — runs once, on demand, ignoring watch/live: the
    owner asked to see the result right now, not to wait for a digest."""
    cfg = store.chain_config(chain)
    prices = await gmgn.cached_native_prices(session)
    hits = await scan_chain(session, chain, cfg, prices)
    if not hits:
        await tg.send(session, f"🔍 {gmgn.CHAIN_NAMES[chain]}: пока никто не "
                      "подходит под текущие пороги.")
        return
    for hit in hits:
        await handle_hit(session, hit, live=True)


# ---------- loops ----------

async def scanner(session, stop):
    while not stop.is_set():
        started = time.time()
        try:
            prices = await gmgn.cached_native_prices(session)
            live = store.get_global("mode", str) == "live"
            for chain in store.enabled_chains():
                cfg = store.chain_config(chain)
                for hit in await scan_chain(session, chain, cfg, prices):
                    await handle_hit(session, hit, live)
        except Exception as e:
            log.exception("сбой прохода: %s", e)
        await asyncio.sleep(max(5, SCAN_EVERY - (time.time() - started)))


async def handle_mute(session, alert_id, code, chat_id, message_id, cq_id):
    row = store.alert_by_id(alert_id)
    if not row:
        await tg.answer_callback(session, cq_id, "Алерт устарел", show_alert=True)
        return
    store.mute_token(row["chain"], row["address"], row["symbol"], row["name"], code)
    label = store.MUTE_LABELS[code]
    button = {"text": f"🔇 Заглушено: {label}", "callback_data": "noop"}
    await tg.edit_markup(session, chat_id, message_id, {"inline_keyboard": [[button]]})
    extra = (" Токен в списке «Удалённые» в /settings этой сети."
             if code == "f" else "")
    await tg.answer_callback(session, cq_id, f"Алерты по этому токену заглушены "
                             f"{label}.{extra}", show_alert=True)


async def handle_button(session, data, chat_id, message_id, cq_id):
    try:
        if data == "noop":
            await tg.answer_callback(session, cq_id)
        elif data.startswith("scan:"):
            chain = data.split(":", 1)[1]
            await manual_scan(session, chain)
            text, kb = await menu.chain_screen(session, chain)
            await tg.edit_message(session, chat_id, message_id, text, kb)
            await tg.answer_callback(session, cq_id)
        elif data.startswith("mute:"):
            _, alert_id, code = data.split(":")
            await handle_mute(session, int(alert_id), code, chat_id, message_id, cq_id)
        else:
            text, kb = await menu.handle_callback(session, data, chat_id, message_id)
            await tg.edit_message(session, chat_id, message_id, text, kb)
            await tg.answer_callback(session, cq_id)
    except Exception as e:
        log.exception("меню: %s", e)
        await tg.answer_callback(session, cq_id, "Ошибка, попробуйте ещё раз",
                                 show_alert=True)


async def commands(session, stop):
    offset = 0
    while not stop.is_set():
        ups, offset = await tg.get_updates(session, offset)
        for u in ups:
            cq = tg.callback_query(u)
            if cq:
                cq_id, data, chat_id, message_id = cq
                await handle_button(session, data, chat_id, message_id, cq_id)
                continue
            text = tg.message_text(u)
            if not text:
                continue
            if menu.PENDING and await menu.handle_pending_text(session, text):
                continue
            await reply(session, text)


CHAIN_FIELDS = {"mc_min", "vol1m_min", "fees_min", "max_age_h"}
GLOBAL_FIELDS = {"realert_min", "liq_min"}


async def reply(session, text):
    parts = text.split()
    cmd = parts[0].lower().lstrip("/")

    if cmd in ("start", "help"):
        await tg.send(session,
            "Сканер токенов на связи.\n\n"
            "/settings — меню настроек по сетям\n"
            "/status — пороги и статистика\n"
            "/set ключ значение — сменить порог текстом\n"
            "/chains — какие сети включены; /chains sol off — выключить\n"
            "/mode watch|live — тихий режим или алерты\n"
            "/last — последние находки\n\n"
            "В /set ключ — либо общий (realert_min, liq_min), либо сетевой "
            "вида sol.mc_min. Комиссии через /set — только в долларах, "
            "валюту ввода меняйте в /settings.")

    elif cmd == "settings":
        text0, kb0 = menu.root_screen()
        await tg.send(session, text0, reply_markup=kb0)

    elif cmd == "status":
        mode = store.get_global("mode", str)
        day = store.alerts_since(int(time.time()) - 86400)
        lines = [
            f"<b>Режим:</b> {mode}",
            f"<b>Находок за сутки:</b> {len(day)}",
            f"<b>Ликвидность (все сети) ≥</b> {fmt.money(store.get_global('liq_min'))}",
            f"<b>Повтор алерта:</b> не чаще {store.get_global('realert_min'):.0f} мин",
            "",
        ]
        for chain in store.CHAINS:
            c = store.chain_config(chain)
            mark = "🟢" if c["enabled"] else "⛔"
            lines.append(
                f"{mark} <b>{gmgn.CHAIN_NAMES[chain]}</b>: "
                f"капа≥{fmt.money(c['mc_min'])} · "
                f"объём/{store.VOL_INTERVAL_LABELS[c['vol_interval']]}≥{fmt.money(c['vol1m_min'])} · "
                f"комиссии≥{fmt.money(c['fees_min'])} · "
                f"возраст≤{fmt.hours(c['max_age_h'])}")
        await tg.send(session, "\n".join(lines))

    elif cmd == "set" and len(parts) == 3:
        key, val = parts[1], parts[2]
        try:
            num = float(val)
        except ValueError:
            await tg.send(session, "Значение должно быть числом")
            return
        if "." in key:
            chain, field = key.split(".", 1)
            if chain not in store.CHAINS or field not in CHAIN_FIELDS:
                await tg.send(session, f"Не знаю ключ {key}. Сети: "
                              + ", ".join(store.CHAINS))
                return
            store.put_chain(chain, field, num)
            await tg.send(session, f"{key} = {val}")
        elif key in GLOBAL_FIELDS:
            store.put_global(key, num)
            await tg.send(session, f"{key} = {val}")
        else:
            await tg.send(session, f"Не знаю ключ {key}. Общие: "
                          + ", ".join(GLOBAL_FIELDS) +
                          ". Сетевые — вида sol.mc_min: " +
                          ", ".join(CHAIN_FIELDS))

    elif cmd == "chains" and len(parts) == 1:
        lines = [f"{'🟢' if store.get_chain(c, 'enabled', bool) else '⛔'} {c}"
                 for c in store.CHAINS]
        await tg.send(session, "<b>Сети</b>\n" + "\n".join(lines))

    elif cmd == "chains" and len(parts) == 3 and parts[2] in ("on", "off"):
        chain = parts[1].lower()
        if chain not in store.CHAINS:
            await tg.send(session, "Не знаю сеть. Доступно: "
                          + ", ".join(store.CHAINS))
            return
        store.put_chain(chain, "enabled", parts[2] == "on")
        await tg.send(session, f"{chain}: {'включено' if parts[2] == 'on' else 'выключено'}")

    elif cmd == "mode" and len(parts) == 2 and parts[1] in ("watch", "live"):
        store.put_global("mode", parts[1])
        await tg.send(session, "Режим: " + parts[1] +
                      (" — присылаю находки сразу" if parts[1] == "live"
                       else " — коплю тихо, сводка раз в сутки"))

    elif cmd == "last":
        rows = store.alerts_since(int(time.time()) - 86400)[:10]
        if not rows:
            await tg.send(session, "За сутки находок не было")
            return
        lines = [f"{r['symbol'] or '?'} ({r['chain']}) · {fmt.money(r['mc'])} · "
                 f"объём {fmt.money(r['vol1m'])} · комиссии {fmt.money(r['fees_usd'])}"
                 for r in rows]
        await tg.send(session, "<b>Находки за сутки</b>\n" + "\n".join(lines))

    else:
        await tg.send(session, "Не понял. /help")


async def digest(session, stop):
    """One summary a day while in watch mode."""
    while not stop.is_set():
        await asyncio.sleep(3600)
        if store.get_global("mode", str) != "watch":
            continue
        now = int(time.time())
        if time.localtime(now).tm_hour != 10:
            continue
        rows = store.alerts_since(now - 86400)
        if not rows:
            await tg.send(session, "За сутки под пороги никто не подошёл. "
                                   "Наблюдаю дальше.")
            continue
        lines = [f"{r['symbol'] or '?'} ({r['chain']}) · капа {fmt.money(r['mc'])} · "
                 f"объём/мин {fmt.money(r['vol1m'])} · комиссии {fmt.money(r['fees_usd'])}"
                 for r in rows[:15]]
        await tg.send(session,
            f"<b>Сводка за сутки: {len(rows)} находок</b>\n\n" + "\n".join(lines) +
            "\n\nВключить уведомления сразу: /mode live")


async def main():
    store.init()
    stop = asyncio.Event()
    log.info("старт, сети: %s", ", ".join(store.enabled_chains()))
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            scanner(session, stop),
            commands(session, stop),
            digest(session, stop),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
