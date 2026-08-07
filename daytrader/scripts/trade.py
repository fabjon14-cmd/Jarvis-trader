# daytrader/scripts/trade.py
#
# Order placement for the intraday equities day-trading agent.
#
# Shares the crypto scalper's Alpaca account (operator's explicit choice,
# 2026-08-07 — see CLAUDE.md "Sharing the crypto scalper's Alpaca
# account") rather than a fourth separate one. No symbol overlap risk
# (crypto pairs are "BTC/USD"-shaped, this trades bare tickers), but order
# history on a shared account includes crypto scalper's own orders — every
# check below that reads order history filters to WATCHLIST_SYMBOLS so
# crypto activity never counts against this agent's caps, or vice versa.

import os
import sys
import json
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

from research import (
    get_orders,
    get_circuit_breaker_status,
    get_deployed_notional,
    get_account,
    PER_TRADE_PCT_CAP,
)

with open(os.path.join(os.path.dirname(__file__), "..", "watchlist.json")) as _f:
    WATCHLIST_SYMBOLS = set(json.load(_f)["symbols"])

ALPACA_KEY = os.getenv("DAYTRADER_APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("DAYTRADER_APCA_API_SECRET_KEY")
BASE_URL = os.getenv("DAYTRADER_APCA_BASE_URL", "https://paper-api.alpaca.markets")
REQUEST_TIMEOUT = 15

UNATTENDED = os.getenv("DAYTRADER_UNATTENDED") == "1"

# Not explicitly in the operator's spec, but added deliberately — see
# CLAUDE.md "Caps added beyond the original spec" for why an unbounded
# number of simultaneous positions, even at a conservative 1% each, still
# warrants a backstop (e.g. a run where every watchlist symbol signals at
# once). PER_TRADE_PCT_CAP itself (the operative "1% of account balance
# per trade" rule) is imported from research.py so there's one definition,
# not two that could drift apart.
MAX_ORDER_NOTIONAL = float(os.getenv("DAYTRADER_MAX_ORDER_NOTIONAL", "500"))
MAX_ORDERS_PER_RUN = int(os.getenv("DAYTRADER_MAX_ORDERS_PER_RUN", "5"))
DAILY_NOTIONAL_CAP = float(os.getenv("DAYTRADER_DAILY_NOTIONAL_CAP", "1000"))
MAX_POSITIONS = int(os.getenv("DAYTRADER_MAX_POSITIONS", "5"))

_orders_this_run = 0


def _headers(json_content=False):
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def confirm(prompt):
    if UNATTENDED:
        print(f"[AUTOPILOT] Auto-approved: {prompt}")
        return True
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def _find_duplicate_order(symbol, qty, side, limit_price):
    """Same exact-match-today dedup as the other two bots — protects a run
    that crashes after submitting but before journaling from doubling a
    position on restart. Matching on the exact symbol already makes this
    safe on a shared account without needing a WATCHLIST_SYMBOLS filter —
    a crypto order can never accidentally match an equity symbol string."""
    today = datetime.now(timezone.utc).date()
    for o in get_orders(status="all", limit=300):
        if o.get("symbol") != symbol or o.get("side") != side:
            continue
        if o.get("status") in ("canceled", "cancelled", "rejected", "expired"):
            continue
        try:
            if float(o.get("qty") or 0) != float(qty):
                continue
            if limit_price is not None and float(o.get("limit_price") or 0) != float(limit_price):
                continue
        except (TypeError, ValueError):
            continue
        submitted = o.get("submitted_at") or o.get("created_at")
        if not submitted:
            continue
        try:
            order_date = datetime.strptime(submitted[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if order_date == today:
            return o
    return None


def _check_buy_allowed(symbol, qty, notional):
    """Shared pre-checks for any BUY (plain or bracket) — one definition so
    place_order and place_bracket_order can't drift apart. Returns a
    rejection dict, or None if the buy is allowed."""
    if _orders_this_run >= MAX_ORDERS_PER_RUN:
        return {"placed": False, "reason": f"max orders per run ({MAX_ORDERS_PER_RUN}) reached"}
    if notional > MAX_ORDER_NOTIONAL:
        return {"placed": False, "reason": f"order notional ${notional:.2f} exceeds cap ${MAX_ORDER_NOTIONAL}"}
    cb = get_circuit_breaker_status()
    if cb.get("halted"):
        return {"placed": False, "reason": f"circuit breaker halted: {cb.get('reason')}"}
    deployed_today = get_deployed_notional(symbols=WATCHLIST_SYMBOLS)
    if deployed_today + notional > DAILY_NOTIONAL_CAP:
        return {"placed": False, "reason": f"daily notional cap ${DAILY_NOTIONAL_CAP} would be exceeded (${deployed_today:.2f} already deployed today)"}
    return None


def place_order(symbol, qty, side, limit_price=None, market=False, client_order_id=None):
    """Place a plain buy or sell order (no attached SL/TP legs). Used for
    sell-side exits (crossover reversal, forced EOD flatten) — new BUY
    entries go through place_bracket_order instead (see CLAUDE.md
    'Bracket orders'). market=True is reserved for the forced end-of-day
    flatten, matching the crypto scalper's 'a mandatory exit needs a
    guaranteed fill, not a resting limit order' reasoning."""
    if UNATTENDED and not market and limit_price is None:
        return {"placed": False, "reason": "unattended runs require a limit price (or market=True for a mandatory exit)"}

    duplicate = _find_duplicate_order(symbol, qty, side, limit_price)
    if duplicate:
        return {"placed": False, "duplicate": True, "existing_order": duplicate}

    if side == "buy":
        notional = float(qty) * float(limit_price or 0)
        rejection = _check_buy_allowed(symbol, qty, notional)
        if rejection:
            return rejection

    if not confirm(f"{side.upper()} {qty} {symbol}" + (f" @ limit {limit_price}" if limit_price else " @ market") + "?"):
        return {"placed": False, "reason": "Declined."}

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market" if market else "limit",
        "time_in_force": "day",
    }
    if not market and limit_price is not None:
        order["limit_price"] = str(limit_price)
    if client_order_id:
        order["client_order_id"] = client_order_id

    url = f"{BASE_URL}/v2/orders"
    response = requests.post(url, headers=_headers(json_content=True), json=order, timeout=REQUEST_TIMEOUT)
    if not response.ok:
        return {"placed": False, "reason": f"Alpaca rejected the order ({response.status_code}): {response.text}"}

    global _orders_this_run
    if side == "buy":
        _orders_this_run += 1
    return {**response.json(), "placed": True}


def place_bracket_order(symbol, qty, limit_price, stop_price, tp_price, client_order_id=None):
    """Buy entry with broker-managed stop-loss and take-profit legs
    attached (Alpaca order_class='bracket') — see CLAUDE.md 'Bracket
    orders' for why: the stop/TP fire immediately at the broker on a real
    price cross, not only when this agent happens to poll every 5 minutes.
    Shares the exact same duplicate-check and buy-side caps as place_order
    (via _check_buy_allowed) so a bracket entry can't bypass anything a
    plain entry couldn't."""
    if UNATTENDED and limit_price is None:
        return {"placed": False, "reason": "unattended runs require a limit price"}
    if stop_price >= limit_price or tp_price <= limit_price:
        return {"placed": False, "reason": f"invalid bracket levels: stop={stop_price} limit={limit_price} tp={tp_price} (need stop < limit < tp for a long)"}

    duplicate = _find_duplicate_order(symbol, qty, "buy", limit_price)
    if duplicate:
        return {"placed": False, "duplicate": True, "existing_order": duplicate}

    notional = float(qty) * float(limit_price)
    rejection = _check_buy_allowed(symbol, qty, notional)
    if rejection:
        return rejection

    if not confirm(f"BUY {qty} {symbol} @ limit {limit_price} (bracket: stop {stop_price} / tp {tp_price})?"):
        return {"placed": False, "reason": "Declined."}

    order = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "limit_price": str(limit_price),
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": str(tp_price)},
        "stop_loss": {"stop_price": str(stop_price)},
    }
    if client_order_id:
        order["client_order_id"] = client_order_id

    url = f"{BASE_URL}/v2/orders"
    response = requests.post(url, headers=_headers(json_content=True), json=order, timeout=REQUEST_TIMEOUT)
    if not response.ok:
        return {"placed": False, "reason": f"Alpaca rejected the order ({response.status_code}): {response.text}"}

    global _orders_this_run
    _orders_this_run += 1
    return {**response.json(), "placed": True}


def cancel_symbol_orders(symbol):
    """Cancel every open order for `symbol` — called before a manual
    crossover-reversal or EOD-flatten exit, to clear a bracket entry's
    still-resting stop-loss/take-profit legs first. Without this, the
    manual sell could conflict with (or get rejected by) the qty already
    held by the bracket's open child orders."""
    url = f"{BASE_URL}/v2/orders"
    params = {"status": "open", "symbols": symbol}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    open_orders = response.json()
    cancelled = []
    for o in open_orders:
        del_response = requests.delete(f"{url}/{o['id']}", headers=_headers(), timeout=REQUEST_TIMEOUT)
        cancelled.append({"order_id": o["id"], "status_code": del_response.status_code})
    return {"cancelled_orders": cancelled}


def cancel_all_orders():
    if not confirm(f"Cancel ALL open orders on {BASE_URL}. Approve?"):
        return {"cancelled": False, "reason": "Declined."}
    url = f"{BASE_URL}/v2/orders"
    response = requests.delete(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    return {"cancelled": True, "status_code": response.status_code}


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else None

    if action == "status":
        print(json.dumps(get_account()))
    elif action == "order":
        symbol = sys.argv[2]
        qty = sys.argv[3]
        side = sys.argv[4]
        limit_price = sys.argv[5] if len(sys.argv) > 5 else None
        print(json.dumps(place_order(symbol, qty, side, limit_price)))
    elif action == "cancel":
        print(json.dumps(cancel_all_orders()))
    else:
        print("Usage: trade.py status | order SYMBOL QTY SIDE [LIMIT_PRICE] | cancel")
