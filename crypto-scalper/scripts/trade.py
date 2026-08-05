# crypto-scalper/scripts/trade.py
#
# Order placement for the crypto scalper. Deliberately a separate module from
# the equities Trader's scripts/trade.py — this agent trades its own,
# separate Alpaca account (own API keys, own equity, own budgets/caps) so it
# can never touch or be affected by the equities bot's account at all.

import os
import sys
import json
import importlib.util
import requests
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# Explicit file-path load under a unique module name — see the same note in
# crypto-scalper/scripts/research.py. This module (crypto-scalper/scripts/
# research.py) already re-exports the equities account/order/circuit-breaker
# functions it needs, so loading it once here is sufficient for both.
_crypto_research_path = os.path.join(os.path.dirname(__file__), "research.py")
_spec = importlib.util.spec_from_file_location("crypto_scalper_research", _crypto_research_path)
_crypto_research = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_crypto_research)

get_crypto_deployed_notional = _crypto_research.get_crypto_deployed_notional
check_max_positions = _crypto_research.check_max_positions
get_crypto_positions = _crypto_research.get_crypto_positions
get_circuit_breaker_status = _crypto_research.get_circuit_breaker_status
get_orders = _crypto_research.get_orders
get_account = _crypto_research.get_account

ALPACA_KEY = os.getenv("CRYPTO_APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("CRYPTO_APCA_API_SECRET_KEY")
BASE_URL = os.getenv("CRYPTO_APCA_BASE_URL", "https://paper-api.alpaca.markets")
REQUEST_TIMEOUT = 15

# Same unattended/interactive split as the equities bot: interactively this
# pauses for a human y/N; under CRYPTO_SCALPER_UNATTENDED=1 (scheduled runs)
# it auto-approves but requires limit orders and enforces the caps below.
UNATTENDED = os.getenv("CRYPTO_SCALPER_UNATTENDED") == "1"
MAX_ORDER_NOTIONAL = float(os.getenv("CRYPTO_MAX_ORDER_NOTIONAL", "200"))
MAX_ORDERS_PER_RUN = int(os.getenv("CRYPTO_MAX_ORDERS_PER_RUN", "5"))
DAILY_NOTIONAL_CAP = float(os.getenv("CRYPTO_DAILY_NOTIONAL_CAP", "300"))
WEEKLY_NOTIONAL_CAP = float(os.getenv("CRYPTO_WEEKLY_NOTIONAL_CAP", "1500"))
MAX_POSITIONS = int(os.getenv("CRYPTO_MAX_POSITIONS", "3"))
PER_TRADE_PCT_CAP = float(os.getenv("CRYPTO_PER_TRADE_PCT_CAP", "0.05"))  # 5% of buying power
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
    """Same exact-match-today dedup as the equities bot, checked against
    Alpaca's own order history rather than in-memory state."""
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
    if UNATTENDED:
        print(f"[AUTOPILOT] Auto-approved: {prompt}")
        return True
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def place_order(symbol, qty, side, limit_price=None):
    """Place a buy or sell order for a crypto pair (e.g. "BTC/USD"). No
    leverage/margin — this trades cash buying power only, same as any other
    order on this account; nothing here requests margin."""
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
                          f"(set CRYPTO_MAX_ORDER_NOTIONAL to change).",
            }
        if _orders_this_run >= MAX_ORDERS_PER_RUN:
            return {
                "placed": False,
                "reason": f"Reached the {MAX_ORDERS_PER_RUN}-order cap for this run "
                          f"(set CRYPTO_MAX_ORDERS_PER_RUN to change).",
            }

        # Buy-only gates, same "fail closed on an unverifiable check" pattern
        # as the equities bot. Sells (exits) skip all of these — they reduce
        # risk, not add it.
        if side == "buy":
            try:
                cb = get_circuit_breaker_status()
            except Exception as exc:
                return {"placed": False, "reason": f"Could not verify circuit breaker status ({exc}) — holding rather than guessing."}
            if cb["halted"]:
                return {"placed": False, "reason": f"Circuit breaker halted: {'; '.join(cb['reasons'])}."}

            try:
                held = {p["symbol"] for p in get_crypto_positions()}
            except Exception as exc:
                return {"placed": False, "reason": f"Could not verify existing crypto positions ({exc}) — holding rather than risking an add to a held pair."}
            if symbol in held:
                return {"placed": False, "reason": f"Already holding a {symbol} position — no averaging in/adding, only one entry per pair at a time."}

            try:
                pos_check = check_max_positions(symbol, MAX_POSITIONS)
            except Exception as exc:
                return {"placed": False, "reason": f"Could not verify max-position cap ({exc}) — holding rather than guessing."}
            if pos_check["blocked"]:
                return {"placed": False, "reason": f"Max concurrent positions: {pos_check['reason']}."}

            try:
                deployed = get_crypto_deployed_notional()
            except Exception as exc:
                return {"placed": False, "reason": f"Could not verify daily/weekly deployed notional ({exc}) — holding rather than guessing."}
            if deployed["daily_deployed"] + notional > DAILY_NOTIONAL_CAP:
                return {
                    "placed": False,
                    "reason": f"Daily crypto notional cap: ${deployed['daily_deployed']:,.2f} already deployed today, "
                              f"this order (${notional:,.2f}) would exceed ${DAILY_NOTIONAL_CAP:,.2f}.",
                }
            if deployed["weekly_deployed"] + notional > WEEKLY_NOTIONAL_CAP:
                return {
                    "placed": False,
                    "reason": f"Weekly crypto notional cap: ${deployed['weekly_deployed']:,.2f} already deployed this week, "
                              f"this order (${notional:,.2f}) would exceed ${WEEKLY_NOTIONAL_CAP:,.2f}.",
                }

            try:
                account = get_account()
                buying_power = float(account.get("buying_power", 0))
            except Exception as exc:
                return {"placed": False, "reason": f"Could not verify buying power ({exc}) — holding rather than guessing."}
            per_trade_cap = buying_power * PER_TRADE_PCT_CAP
            if notional > per_trade_cap:
                return {
                    "placed": False,
                    "reason": f"Order notional ${notional:,.2f} exceeds {PER_TRADE_PCT_CAP*100:.0f}% of buying power "
                              f"(${per_trade_cap:,.2f} of ${buying_power:,.2f}).",
                }

    price_desc = f"limit ${limit_price}" if limit_price else "at market"
    if not confirm(f"{side.upper()} {qty} {symbol} ({price_desc}) on {BASE_URL}. Approve?"):
        return {"placed": False, "reason": "Declined."}

    headers = _headers(json_content=True)
    order_data = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "limit" if limit_price else "market",
        "time_in_force": "gtc",  # crypto has no "day" session to expire against
    }
    if limit_price:
        order_data["limit_price"] = str(limit_price)

    url = f"{BASE_URL}/v2/orders"
    response = requests.post(url, headers=headers, json=order_data, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    if UNATTENDED:
        _orders_this_run += 1
    return response.json()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else None

    if action == "order":
        symbol = sys.argv[2]
        qty = sys.argv[3]
        side = sys.argv[4]
        limit_price = sys.argv[5] if len(sys.argv) > 5 else None
        print(json.dumps(place_order(symbol, qty, side, limit_price)))
    else:
        print("Usage: trade.py order PAIR QTY SIDE [LIMIT_PRICE]")
