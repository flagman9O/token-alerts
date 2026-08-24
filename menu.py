"""The /settings menu: a small tree of inline-keyboard screens.

Everything is driven by `callback_data` strings of the form
`action:scope:field:value`, where `scope` is a chain code or the literal
"global". `handle_callback` is the single entry point bot.py calls for every
button press; it mutates `store` and returns the (text, keyboard) pair for
whatever screen should be shown next, which the caller then pushes with
`telegram.edit_message` so the menu redraws in place instead of spamming the
chat with a new message per click.

Typing a custom number is the one thing buttons cannot do on their own: the
owner is prompted to send it as a plain message, and `PENDING` remembers what
that number is for until it arrives (or until any other button press cancels
it, which happens implicitly since every action clears `PENDING` first).
"""

import fmt
import gmgn
import store
import telegram as tg

# ---------- chip presets ----------

CHIP_MC = (50_000, 100_000, 200_000, 500_000, 1_000_000)
CHIP_VOL = (10_000, 25_000, 50_000, 75_000, 100_000)
CHIP_AGE_H = (0, 6, 24, 72, 168)
CHIP_REALERT_H = (1, 3, 6, 12, 24)
CHIP_LIQ = (0, 10_000, 25_000, 50_000, 100_000)
CHIP_FEES_NATIVE = {"SOL": (5, 10, 25, 50, 100),
                    "BNB": (1, 2, 3, 5, 10),
                    "ETH": (0.25, 0.5, 1, 2, 5)}
CHIP_FEES_USD = (500, 1000, 2400, 5000, 10000)

FIELD_LABELS = {
    "mc_min": "Капитализация", "vol1m_min": "Объём/мин",
    "max_age_h": "Возраст", "realert_h": "Повтор алерта",
    "liq_min": "Мин. ликвидность",
}
FIELD_ICONS = {
    "mc_min": "💰", "vol1m_min": "📈", "max_age_h": "⏳",
    "realert_h": "⏱", "liq_min": "💧",
}
FIELD_EXAMPLES = {
    "mc_min": "300000", "vol1m_min": "75000",
    "max_age_h": "48", "realert_h": "12", "liq_min": "20000",
}

# The one place `PENDING` gets read or written outside this module is bot.py's
# message loop, deciding whether the next text message is a menu answer.
PENDING = None


def _btn(text, data):
    return {"text": text, "callback_data": data}


def _kb(rows):
    return {"inline_keyboard": rows}


def _grid(items, cols=3):
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def _chip_value(field, v):
    if field == "max_age_h":
        return fmt.hours(v)
    if field in ("mc_min", "vol1m_min", "realert_h", "liq_min"):
        return fmt.money(v) if field != "realert_h" else f"{v:.0f} ч"
    return str(v)


def _scope_name(scope):
    return "Общие" if scope == "global" else gmgn.CHAIN_NAMES.get(scope, scope)


def _parent_action(scope):
    return "global" if scope == "global" else f"chain:{scope}"


# ---------- screens ----------

def root_screen():
    rows = []
    line = []
    for chain in store.CHAINS:
        mark = "🟢" if store.get_chain(chain, "enabled", bool) else "⛔"
        line.append(_btn(f"{mark} {gmgn.CHAIN_NAMES[chain]}", f"chain:{chain}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)

    mode = store.get_global("mode", str)
    pause_label = ("▶️ Возобновить алерты" if mode != "live"
                   else "⏸ Остановить все алерты")
    rows.append([_btn("🌐 Общие параметры", "global")])
    rows.append([_btn(pause_label, "pause")])
    rows.append([_btn("✖️ Закрыть", "close")])

    text = ("⚙️ <b>Настройки алертов</b>\n\n"
            "Выберите сеть, или общие параметры внизу.\n"
            "🟢 = алерты включены   ⛔ = выключены")
    return text, _kb(rows)


async def chain_screen(session, chain):
    cfg = store.chain_config(chain)
    prices = await gmgn.cached_native_prices(session)
    coin = gmgn.NATIVE[chain]
    price = prices.get(coin, 0)
    native = cfg["fees_min"] / price if price else 0
    fee_line = (f"{native:,.2f} {coin} (≈ {fmt.money(cfg['fees_min'])})"
                if cfg["fees_currency"] == "native"
                else f"{fmt.money(cfg['fees_min'])} (≈ {native:,.2f} {coin})")

    text = (
        f"⚙️ <b>{gmgn.CHAIN_NAMES[chain]}</b>\n\n"
        f"Алерты: {'🟢 включены' if cfg['enabled'] else '⛔ выключены'}\n"
        f"Капитализация ≥ {fmt.money(cfg['mc_min'])}\n"
        f"Объём/мин ≥ {fmt.money(cfg['vol1m_min'])}\n"
        f"Комиссии ≥ {fee_line}\n"
        f"Возраст ≤ {fmt.hours(cfg['max_age_h'])}\n"
        f"Сводка о токене: {'🟢 включена' if cfg['summary'] else '⛔ выключена'}\n"
        f"Риск-фильтр: {'🟢 включён' if cfg['risk_filter'] else '⛔ выключен'}"
    )
    rows = [
        [_btn(f"{'🟢' if cfg['enabled'] else '⛔'} Алерты по сети",
              f"tgl:{chain}:enabled")],
        [_btn(f"💰 Капитализация: {fmt.money(cfg['mc_min'])} ▸",
              f"edit:{chain}:mc_min")],
        [_btn(f"📈 Объём/мин: {fmt.money(cfg['vol1m_min'])} ▸",
              f"edit:{chain}:vol1m_min")],
        [_btn("💵 Комиссии ▸", f"edit:{chain}:fees_min")],
        [_btn(f"⏳ Возраст: {fmt.hours(cfg['max_age_h'])} ▸",
              f"edit:{chain}:max_age_h")],
        [_btn(f"{'🟢' if cfg['summary'] else '⛔'} Сводка о токене",
              f"tgl:{chain}:summary")],
        [_btn(f"{'🟢' if cfg['risk_filter'] else '⛔'} Риск-фильтр",
              f"tgl:{chain}:risk_filter")],
        [_btn("🔍 Проверить сейчас", f"scan:{chain}")],
        [_btn("◀️ Назад", "root"), _btn("✖️ Закрыть", "close")],
    ]
    return text, _kb(rows)


async def global_screen(session):
    mode = store.get_global("mode", str)
    realert = store.get_global("realert_h")
    liq = store.get_global("liq_min")
    text = (
        "🌐 <b>Общие параметры</b>\n\n"
        f"Режим: {'🔴 live (алерты сразу)' if mode == 'live' else '🟡 watch (тихо, сводка раз в сутки)'}\n"
        f"Повтор алерта: не чаще {realert:.0f} ч\n"
        f"Мин. ликвидность: {fmt.money(liq)} (все сети)"
    )
    rows = [
        [_btn(f"Режим: {'LIVE' if mode == 'live' else 'WATCH'}", "tgl:global:mode")],
        [_btn(f"⏱ Повтор алерта: {realert:.0f} ч ▸", "edit:global:realert_h")],
        [_btn(f"💧 Мин. ликвидность: {fmt.money(liq)} ▸", "edit:global:liq_min")],
        [_btn("◀️ Назад", "root"), _btn("✖️ Закрыть", "close")],
    ]
    return text, _kb(rows)


def chip_screen(scope, field, current):
    chips = {"mc_min": CHIP_MC, "vol1m_min": CHIP_VOL, "max_age_h": CHIP_AGE_H,
             "realert_h": CHIP_REALERT_H, "liq_min": CHIP_LIQ}[field]
    buttons = [_btn(_chip_value(field, v), f"set:{scope}:{field}:{v}")
              for v in chips]
    rows = _grid(buttons, cols=3)
    rows.append([_btn("✏️ Своё значение", f"custom:{scope}:{field}")])
    rows.append([_btn("◀️ Назад", _parent_action(scope))])

    text = (f"{FIELD_ICONS[field]} <b>{_scope_name(scope)} → {FIELD_LABELS[field]}</b>\n\n"
            f"Сейчас: {_chip_value(field, current)}")
    return text, _kb(rows)


async def fees_screen(session, chain):
    cfg = store.chain_config(chain)
    prices = await gmgn.cached_native_prices(session)
    coin = gmgn.NATIVE[chain]
    price = prices.get(coin, 0)
    currency = cfg["fees_currency"]

    if currency == "native":
        chips = CHIP_FEES_NATIVE.get(coin, (5, 10, 25, 50, 100))
        cur_native = cfg["fees_min"] / price if price else 0
        cur_txt = (f"{cur_native:,.2f} {coin} (≈ {fmt.money(cfg['fees_min'])}"
                   f" по курсу ${price:,.2f})")
        chip_fmt = lambda v: f"{v:g} {coin}"
    else:
        chips = CHIP_FEES_USD
        cur_txt = fmt.money(cfg["fees_min"])
        chip_fmt = lambda v: fmt.money(v)

    buttons = [_btn(chip_fmt(v), f"setfee:{chain}:{currency}:{v}") for v in chips]
    rows = _grid(buttons, cols=3)
    rows.append([_btn(f"Валюта: {'●' + coin + ' ○USDT' if currency == 'native' else '○' + coin + ' ●USDT'}",
                      f"cur:{chain}")])
    rows.append([_btn("✏️ Своё значение", f"customfee:{chain}")])
    rows.append([_btn("◀️ Назад", f"chain:{chain}")])

    text = (f"💵 <b>{gmgn.CHAIN_NAMES[chain]} → Комиссии</b>\n\n"
            f"Валюта ввода: {'⦿ ' + coin + '   ○ USDT' if currency == 'native' else '○ ' + coin + '   ⦿ USDT'}\n"
            f"Сейчас: {cur_txt}")
    return text, _kb(rows)


def custom_prompt(scope, field, unit_hint=""):
    text = (f"✏️ Введите число{unit_hint}.\n"
            f"Например: {FIELD_EXAMPLES.get(field, '100')}")
    rows = [[_btn("❌ Отмена", _parent_action(scope) if field != "fees_min"
                  else f"chain:{scope}")]]
    return text, _kb(rows)


# ---------- dispatch ----------

async def handle_callback(session, data, chat_id, message_id):
    """Mutates settings for one button press and returns the next screen.

    Any press clears a pending custom-value prompt except the two actions
    that open one — so navigating away is an implicit cancel, no separate
    "cancel and also go back" logic needed.
    """
    global PENDING
    parts = data.split(":")
    action = parts[0]
    if action not in ("custom", "customfee"):
        PENDING = None

    if action == "root":
        return root_screen()
    if action == "global":
        return await global_screen(session)
    if action == "close":
        return "Настройки закрыты. /settings — открыть снова.", None
    if action == "pause":
        store.toggle_mode()
        return root_screen()
    if action == "chain":
        return await chain_screen(session, parts[1])

    if action == "tgl":
        _, scope, field = parts
        if scope == "global" and field == "mode":
            store.toggle_mode()
            return await global_screen(session)
        store.toggle_chain(scope, field)
        return await chain_screen(session, scope)

    if action == "edit":
        _, scope, field = parts
        if field == "fees_min":
            return await fees_screen(session, scope)
        current = (store.get_global(field) if scope == "global"
                  else store.get_chain(scope, field))
        return chip_screen(scope, field, current)

    if action == "cur":
        _, chain = parts
        cfg = store.chain_config(chain)
        store.put_chain(chain, "fees_currency",
                        "usd" if cfg["fees_currency"] == "native" else "native")
        return await fees_screen(session, chain)

    if action == "set":
        _, scope, field, value = parts
        if scope == "global":
            store.put_global(field, float(value))
            return await global_screen(session)
        store.put_chain(scope, field, float(value))
        return await chain_screen(session, scope)

    if action == "setfee":
        _, chain, currency, value = parts
        prices = await gmgn.cached_native_prices(session)
        coin = gmgn.NATIVE[chain]
        usd = float(value) * prices.get(coin, 0) if currency == "native" else float(value)
        store.put_chain(chain, "fees_min", usd)
        return await chain_screen(session, chain)

    if action == "custom":
        _, scope, field = parts
        PENDING = {"scope": scope, "field": field, "chat_id": chat_id,
                   "message_id": message_id, "kind": "field"}
        return custom_prompt(scope, field, " (в " +
                             ("часах" if field == "max_age_h" else "долларах")
                             + ")" if field != "realert_h" else " (в часах)")

    if action == "customfee":
        _, chain = parts
        cfg = store.chain_config(chain)
        coin = gmgn.NATIVE[chain]
        PENDING = {"scope": chain, "field": "fees_min", "chat_id": chat_id,
                   "message_id": message_id, "kind": "fees",
                   "currency": cfg["fees_currency"]}
        unit = coin if cfg["fees_currency"] == "native" else "USDT"
        return custom_prompt(chain, "fees_min", f" (в {unit})")

    # "scan:<chain>" is intercepted by bot.py before it reaches this
    # dispatcher — it needs scan_chain and the alert card, which live there.

    return root_screen()


async def handle_pending_text(session, text):
    """The owner's reply to a custom-value prompt. Returns True if it was
    consumed (whether valid or not), so the caller never falls through to
    treating the number as a slash command."""
    global PENDING
    p = PENDING
    if not p:
        return False

    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        await tg.send(session, "Не понял число, попробуйте ещё раз или "
                      "нажмите ❌ Отмена на сообщении выше")
        return True

    if p["kind"] == "fees":
        prices = await gmgn.cached_native_prices(session)
        coin = gmgn.NATIVE[p["scope"]]
        usd = value * prices.get(coin, 0) if p["currency"] == "native" else value
        store.put_chain(p["scope"], "fees_min", usd)
    elif p["scope"] == "global":
        store.put_global(p["field"], value)
    else:
        store.put_chain(p["scope"], p["field"], value)

    PENDING = None
    text2, kb2 = (await global_screen(session) if p["scope"] == "global"
                  else await chain_screen(session, p["scope"]))
    await tg.edit_message(session, p["chat_id"], p["message_id"], text2, kb2)
    return True
