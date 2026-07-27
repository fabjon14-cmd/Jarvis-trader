# scripts/trade.py

import os
import requests
import json
import sys

from dotenv import load_dotenv

load_dotenv()

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
    response.raise_for_status()
    if UNATTENDED:
        _orders_this_run += 1
    return response.json()


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
