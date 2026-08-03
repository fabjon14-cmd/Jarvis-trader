# scripts/trade.py

import os
import requests
import json
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from research import (
    get_circuit_breaker_status,
    get_deployed_notional,
    check_sector_cap,
    get_orders,
    would_be_day_trade,
    get_day_trade_count,
)

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")

REQUEST_TIMEOUT = 15

# Unattended (scheduled) runs auto-approve instead of blocking on stdin, since
# there's no one at a terminal to answer a y/N prompt. In that mode we also
# require limit orders and cap size, since nothing else is reviewing the trade.
UNATTENDED = os.getenv("TRADER_UNATTENDED") == "1"
MAX_ORDER_NOTIONAL = float(os.getenv("TRADER_MAX_ORDER_NOTIONAL", "2000"))
MAX_ORDERS_PER_RUN = int(os.getenv("TRADER_MAX_ORDERS_PER_RUN", "10"))
DAILY_NOTIONAL_CAP = float(os.getenv("TRADER_DAILY_NOTIONAL_CAP", "500"))
WEEKLY_NOTIONAL_CAP = float(os.getenv("TRADER_WEEKLY_NOTIONAL_CAP", "1000"))
_orders_this_run = 0


def _headers(json_content=False):
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def _find_duplicate_order(symbol, qty, side, limit_price):
    """Check Alpaca's own order history — not in-memory state, which doesn't
    survive a crash — for an order already submitted today with these exact
    parameters. Protects against a run that crashes after submitting but
    before journaling, then restarts and re-attempts the same decision:
    without this, that would double the position instead of no-op'ing."""
    today = datetime.now(timezone.utc).date()
    for o in get_orders(status="all", limit=200):
        if o.get("symbol") != symbol or o.get("side") != side:
            continue
        if o.get("status") in ("canceled", "cancelled", "rejected", "expired"):
            continue
        try:
            if float(o.get("qty") or 0) != float(qty) or float(o.get("limit_price") or 0) != float(limit_price):
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


def confirm(prompt):
    """Approve an order. In unattended mode this auto-approves and logs the
    decision instead of blocking on input(), since there's no terminal to
    answer it. Otherwise it's a real y/N prompt."""
    if UNATTENDED:
        print(f"[AUTOPILOT] Auto-approved: {prompt}")
        return True
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def place_order(symbol, qty, side, limit_price=None):
    """Place a buy or sell order."""
    global _orders_this_run

    if UNATTENDED:
        if not limit_price:
            return {"placed": False, "reason": "Unattended runs require limit_price (no market orders)."}

        try:
            dup = _find_duplicate_order(symbol, qty, side, limit_price)
        except Exception as exc:
            return {"placed": False, "reason": f"Could not verify duplicate-order protection ({exc}) — holding rather than risking a double-submit."}
        if dup:
            return {
                "placed": False,
                "duplicate": True,
                "existing_order": dup,
                "reason": f"An identical order was already submitted today (id {dup.get('id')}, status {dup.get('status')}) — not re-submitting.",
            }

        notional = float(qty) * float(limit_price)
        if notional > MAX_ORDER_NOTIONAL:
            return {
                "placed": False,
                "reason": f"Order notional ${notional:,.2f} exceeds cap of ${MAX_ORDER_NOTIONAL:,.2f} "
                          f"(set TRADER_MAX_ORDER_NOTIONAL to change).",
            }
        if _orders_this_run >= MAX_ORDERS_PER_RUN:
            return {
                "placed": False,
                "reason": f"Reached the {MAX_ORDERS_PER_RUN}-order cap for this run "
                          f"(set TRADER_MAX_ORDERS_PER_RUN to change).",
            }

        # The three checks below only apply to buys — sells/trims reduce risk,
        # not add it, and the circuit breaker explicitly exempts them. Each
        # fails closed (holds) if the check itself can't be verified, same
        # "don't guess on missing data" principle as everywhere else here.
        if side == "buy":
            try:
                cb = get_circuit_breaker_status()
            except Exception as exc:
                return {"placed": False, "reason": f"Could not verify circuit breaker status ({exc}) — holding rather than guessing."}
            if cb["halted"]:
                return {"placed": False, "reason": f"Circuit breaker halted: {'; '.join(cb['reasons'])}."}

            try:
                deployed = get_deployed_notional()
            except Exception as exc:
                return {"placed": False, "reason": f"Could not verify daily/weekly deployed notional ({exc}) — holding rather than guessing."}
            if deployed["daily_deployed"] + notional > DAILY_NOTIONAL_CAP:
                return {
                    "placed": False,
                    "reason": f"Daily notional cap: ${deployed['daily_deployed']:,.2f} already deployed today, "
                              f"this order (${notional:,.2f}) would exceed ${DAILY_NOTIONAL_CAP:,.2f}.",
                }
            if deployed["weekly_deployed"] + notional > WEEKLY_NOTIONAL_CAP:
                return {
                    "placed": False,
                    "reason": f"Weekly notional cap: ${deployed['weekly_deployed']:,.2f} already deployed this week, "
                              f"this order (${notional:,.2f}) would exceed ${WEEKLY_NOTIONAL_CAP:,.2f}.",
                }

            try:
                sector_check = check_sector_cap(symbol)
            except Exception as exc:
                return {"placed": False, "reason": f"Could not verify sector cap ({exc}) — holding rather than guessing."}
            if sector_check["blocked"]:
                return {"placed": False, "reason": f"Sector cap: {sector_check['reason']} ({sector_check['sector']})."}

    # Informational only (see CLAUDE.md "Day-trade tracking") — never blocks,
    # since it must not stand in the way of a mandatory stop-loss trim/close,
    # and PDT doesn't apply to this paper account anyway. Failing to compute
    # this is not a reason to hold, unlike the hard gates above.
    day_trade_info = {}
    if UNATTENDED:
        try:
            if would_be_day_trade(symbol, side):
                day_trade_info = {"day_trade": True, "trailing_5bd_count": get_day_trade_count()["count"]}
        except Exception:
            pass

    price_desc = f"limit ${limit_price}" if limit_price else "at market"
    if not confirm(f"{side.upper()} {qty} {symbol} ({price_desc}) on {BASE_URL}. Approve?"):
        return {"placed": False, "reason": "Declined."}

    headers = _headers(json_content=True)

    order_data = {
        "symbol": symbol,
        "qty": qty,
        "side": side,  # "buy" or "sell"
        "type": "limit" if limit_price else "market",
        "time_in_force": "day",
    }

    if limit_price:
        order_data["limit_price"] = str(limit_price)

    url = f"{BASE_URL}/v2/orders"
    response = requests.post(url, headers=headers, json=order_data, timeout=REQUEST_TIMEOUT)
    if not response.ok:
        # Fractional qty introduces a real rejection path that whole-share
        # orders rarely hit (e.g. the symbol isn't fractionable on Alpaca) —
        # return it as a clean, loggable rejection like any other capped
        # order instead of letting an HTTPError traceback surface.
        return {"placed": False, "reason": f"Alpaca rejected the order ({response.status_code}): {response.text}"}
    if UNATTENDED:
        _orders_this_run += 1
    return {**response.json(), **day_trade_info}


def cancel_all_orders():
    """Cancel all open orders."""
    if not confirm(f"Cancel ALL open orders on {BASE_URL}. Approve?"):
        return {"cancelled": False, "reason": "Declined."}
    url = f"{BASE_URL}/v2/orders"
    response = requests.delete(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    return {"cancelled": True, "status_code": response.status_code}


def get_market_status():
    """Check if the market is open."""
    url = f"{BASE_URL}/v2/clock"
    response = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else None

    if action == "status":
        print(json.dumps(get_market_status()))

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
