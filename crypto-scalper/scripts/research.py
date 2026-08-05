# crypto-scalper/scripts/research.py
#
# Read-only data + signal computation for the crypto scalper. This is a
# fully separate Alpaca account from the equities Trader — its own API
# keys, its own equity curve, its own circuit breaker — not the same
# account the equities bot trades on. Account/order/circuit-breaker
# plumbing is implemented locally here rather than imported from
# ../../scripts/research.py, since that module is wired to the equities
# account's credentials.

import os
import sys
import json
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

ALPACA_KEY = os.getenv("CRYPTO_APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("CRYPTO_APCA_API_SECRET_KEY")
BASE_URL = os.getenv("CRYPTO_APCA_BASE_URL", "https://paper-api.alpaca.markets")
REQUEST_TIMEOUT = 15

PROFIT_TARGET_PCT = float(os.getenv("CRYPTO_PROFIT_TARGET_PCT", "1.5"))
STOP_LOSS_PCT = float(os.getenv("CRYPTO_STOP_LOSS_PCT", "0.75"))
MAX_HOLD_HOURS = float(os.getenv("CRYPTO_MAX_HOLD_HOURS", "4"))


def _headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def get_account():
    """Get current account status on this agent's own Alpaca account."""
    url = f"{BASE_URL}/v2/account"
    response = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_positions():
    """Get all open positions on this agent's own Alpaca account."""
    url = f"{BASE_URL}/v2/positions"
    response = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_orders(status="all", limit=100):
    """Get historical orders on this agent's own account, most recent first."""
    url = f"{BASE_URL}/v2/orders"
    params = {"status": status, "limit": limit, "direction": "desc"}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_portfolio_history(period="1D", timeframe="15Min"):
    """Get this account's own equity history over a period."""
    url = f"{BASE_URL}/v2/account/portfolio/history"
    params = {"period": period, "timeframe": timeframe}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _portfolio_pct_change(history):
    equity = [e for e in (history.get("equity") or []) if e is not None]
    if len(equity) < 2 or not equity[0]:
        return None
    return (equity[-1] - equity[0]) / equity[0] * 100


def get_circuit_breaker_status():
    """Portfolio-level circuit breaker on this agent's own account equity:
    halts new buys (not exits) if equity dropped more than 4% intraday or
    8% over the trailing 5 trading days. Same thresholds as the equities
    bot's circuit breaker, computed independently against this account's
    own equity curve rather than a shared one."""
    intraday = get_portfolio_history(period="1D", timeframe="15Min")
    five_day = get_portfolio_history(period="5D", timeframe="1D")

    intraday_pct = _portfolio_pct_change(intraday)
    five_day_pct = _portfolio_pct_change(five_day)

    reasons = []
    if intraday_pct is not None and intraday_pct <= -4:
        reasons.append(f"intraday drawdown {intraday_pct:.2f}% (limit -4%)")
    if five_day_pct is not None and five_day_pct <= -8:
        reasons.append(f"5-trading-day drawdown {five_day_pct:.2f}% (limit -8%)")

    return {
        "halted": bool(reasons),
        "intraday_pct": round(intraday_pct, 2) if intraday_pct is not None else None,
        "five_day_pct": round(five_day_pct, 2) if five_day_pct is not None else None,
        "reasons": reasons,
    }


_TIMEFRAME_MINUTES = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60, "1Day": 1440}


def get_crypto_bars(pair, timeframe="5Min", limit=60):
    """Fetch the most recent `limit` bars for a crypto pair (e.g. "BTC/USD").
    Crypto trades 24/7 so there are no weekend/holiday gaps to buffer for,
    unlike the equities bars fetch — but we still sort by timestamp
    ourselves rather than trust API ordering, same defensive habit."""
    minutes_per_bar = _TIMEFRAME_MINUTES.get(timeframe, 5)
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes_per_bar * limit * 1.5 + 60)
    url = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
    params = {
        "symbols": pair,
        "timeframe": timeframe,
        "limit": limit,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    bars = (response.json().get("bars") or {}).get(pair) or []
    bars.sort(key=lambda b: b.get("t", ""))
    return bars[-limit:]


def compute_rsi(closes, period=14):
    """Wilder's RSI, standard 14-period smoothing. Returns the RSI as of the
    most recent close, or None if there isn't enough history."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_ema_series(closes, period=20):
    """Standard EMA, seeded with an SMA over the first `period` closes.
    Returned series is aligned to closes[period - 1:], i.e. ema[-1]
    corresponds to closes[-1]."""
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def get_signal(pair, timeframe="5Min", limit=60):
    """Buy trigger, precisely: RSI(14) on the latest completed bar is below
    30, AND the close crosses above EMA(20) on this bar (previous bar's
    close was at or below its EMA, this bar's close is above its EMA) — a
    cross event, not merely "currently above". Both conditions must be true
    on the same bar."""
    bars = get_crypto_bars(pair, timeframe, limit)
    closes = [b["c"] for b in bars]
    if len(closes) < 25:
        return {"pair": pair, "error": f"only {len(closes)} bars available, need at least 25"}

    rsi = compute_rsi(closes, 14)
    ema_series = compute_ema_series(closes, 20)
    if rsi is None or len(ema_series) < 2:
        return {"pair": pair, "error": "insufficient data for RSI/EMA"}

    prev_close, curr_close = closes[-2], closes[-1]
    prev_ema, curr_ema = ema_series[-2], ema_series[-1]
    crossed_above = prev_close <= prev_ema and curr_close > curr_ema
    rsi_oversold = rsi < 30

    return {
        "pair": pair,
        "rsi": round(rsi, 2),
        "rsi_oversold": rsi_oversold,
        "ema20": round(curr_ema, 4),
        "prev_close": prev_close,
        "curr_close": curr_close,
        "crossed_above_ema": crossed_above,
        "buy_signal": rsi_oversold and crossed_above,
    }


def get_crypto_positions():
    return [p for p in get_positions() if p.get("asset_class") == "crypto" or "/" in (p.get("symbol") or "")]


def get_crypto_deployed_notional():
    """Daily/weekly buy notional on this agent's own account, tracked
    independently of the equities Trader's own daily/weekly cap — separate
    accounts, separate budgets."""
    today = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=7)
    daily_total = weekly_total = 0.0
    for o in get_orders(status="all", limit=500):
        symbol = o.get("symbol") or ""
        if "/" not in symbol or o.get("side") != "buy":
            continue
        if o.get("status") in ("canceled", "cancelled", "rejected", "expired"):
            continue
        submitted = o.get("submitted_at") or o.get("created_at")
        if not submitted:
            continue
        try:
            order_date = datetime.strptime(submitted[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        qty = float(o.get("qty") or 0)
        price = float(o.get("limit_price") or o.get("filled_avg_price") or 0)
        notional = qty * price
        if order_date == today:
            daily_total += notional
        if order_date >= week_start:
            weekly_total += notional
    return {"daily_deployed": round(daily_total, 2), "weekly_deployed": round(weekly_total, 2)}


def get_position_entry_time(symbol):
    """Timestamp of the most recent filled buy for `symbol` — under the
    single-entry-per-pair rule this is the entry time for whatever position
    is currently open, used for the max-hold-time safeguard."""
    for o in get_orders(status="closed", limit=200):
        if o.get("symbol") != symbol or o.get("side") != "buy":
            continue
        if o.get("status") != "filled":
            continue
        ts = o.get("filled_at") or o.get("submitted_at")
        if ts:
            return ts
    return None


def get_exit_flags():
    """Per-position exit check for open crypto positions: profit target,
    stop loss, and max-hold-time, evaluated in that priority order. Mirrors
    the equities per-position stop-loss in spirit (mechanical, fires on
    price/time alone) but with this strategy's own thresholds."""
    flags = []
    for p in get_crypto_positions():
        symbol = p.get("symbol")
        plpc = float(p.get("unrealized_plpc", 0)) * 100
        action, reason = None, None
        if plpc >= PROFIT_TARGET_PCT:
            action, reason = "close_profit_target", f"unrealized {plpc:.2f}% >= +{PROFIT_TARGET_PCT}% target"
        elif plpc <= -STOP_LOSS_PCT:
            action, reason = "close_stop_loss", f"unrealized {plpc:.2f}% <= -{STOP_LOSS_PCT}% stop"
        else:
            entry_ts = get_position_entry_time(symbol)
            if entry_ts:
                try:
                    entry_dt = datetime.strptime(entry_ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                    hours_held = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                    if hours_held >= MAX_HOLD_HOURS:
                        action, reason = "close_timeout", f"held {hours_held:.1f}h >= {MAX_HOLD_HOURS}h max hold, no TP/SL hit"
                except ValueError:
                    pass
        if action:
            flags.append({
                "symbol": symbol,
                "qty": float(p.get("qty", 0)),
                "unrealized_plpc": round(plpc, 2),
                "action": action,
                "reason": reason,
            })
    return flags


def check_max_positions(pair, max_positions):
    """Would opening a NEW position in `pair` exceed the concurrent crypto
    position cap? Only relevant for pairs not already held."""
    held = {p["symbol"] for p in get_crypto_positions()}
    if pair in held:
        return {"pair": pair, "blocked": False, "reason": None, "already_held": True}
    blocked = len(held) >= max_positions
    return {
        "pair": pair,
        "already_held": False,
        "current_positions": len(held),
        "blocked": blocked,
        "reason": f"already {len(held)} of {max_positions} max concurrent crypto positions open" if blocked else None,
    }


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "account"
    pair = sys.argv[2] if len(sys.argv) > 2 else None

    if action == "signal" and pair:
        print(json.dumps(get_signal(pair)))
    elif action == "bars" and pair:
        print(json.dumps(get_crypto_bars(pair)))
    elif action == "positions":
        print(json.dumps(get_crypto_positions()))
    elif action == "deployed":
        print(json.dumps(get_crypto_deployed_notional()))
    elif action == "exit-flags":
        print(json.dumps(get_exit_flags()))
    elif action == "circuit-breaker":
        print(json.dumps(get_circuit_breaker_status()))
    elif action == "max-positions" and pair:
        max_positions = int(os.getenv("CRYPTO_MAX_POSITIONS", "3"))
        print(json.dumps(check_max_positions(pair, max_positions)))
    elif action == "account":
        print(json.dumps(get_account()))
    else:
        print("Usage: research.py signal PAIR | bars PAIR | positions | deployed | exit-flags | circuit-breaker | max-positions PAIR | account")
