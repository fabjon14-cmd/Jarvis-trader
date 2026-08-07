# daytrader/scripts/research.py
#
# Read-only data + signal computation for the intraday equities day-trading
# agent. A third, independent agent in this repo — separate from both the
# equities Trader (hourly, hold-for-days, research-driven) and the crypto
# scalper (5-min, 24/7, RSI/EMA scalp). Own Alpaca account/keys
# (DAYTRADER_APCA_*), own equity curve, own risk budget, own journal — full
# blast-radius isolation, same reasoning as crypto-scalper/CLAUDE.md.
#
# Market-data calls (get_bars) hit data.alpaca.markets, which accepts any
# valid Alpaca key pair regardless of which paper account it's tied to —
# same endpoint/pattern as the equities Trader's scripts/research.py.

import os
import sys
import json
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

ALPACA_KEY = os.getenv("DAYTRADER_APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("DAYTRADER_APCA_API_SECRET_KEY")
BASE_URL = os.getenv("DAYTRADER_APCA_BASE_URL", "https://paper-api.alpaca.markets")
REQUEST_TIMEOUT = 15

# Strategy parameters — see CLAUDE.md "Strategy — precisely" for the exact
# entry/exit rules these back. Long-only (see CLAUDE.md "Long-only, no
# shorting").
FAST_EMA_PERIOD = int(os.getenv("DAYTRADER_FAST_EMA", "9"))
SLOW_EMA_PERIOD = int(os.getenv("DAYTRADER_SLOW_EMA", "21"))
RSI_PERIOD = int(os.getenv("DAYTRADER_RSI_PERIOD", "14"))
RSI_ENTRY_MIN = float(os.getenv("DAYTRADER_RSI_ENTRY_MIN", "40"))
RSI_ENTRY_MAX = float(os.getenv("DAYTRADER_RSI_ENTRY_MAX", "65"))

STOP_LOSS_PCT = float(os.getenv("DAYTRADER_STOP_LOSS_PCT", "1.0"))
TAKE_PROFIT_PCT = float(os.getenv("DAYTRADER_TAKE_PROFIT_PCT", "2.0"))
# "Risk a maximum of 1% of the total account balance per trade" — a
# position-size cap, not a stop-distance-scaled dollar-risk target. See
# CLAUDE.md "Position sizing" for why this matters.
PER_TRADE_PCT_CAP = float(os.getenv("DAYTRADER_PER_TRADE_PCT_CAP", "1.0"))
DAILY_DRAWDOWN_LIMIT_PCT = float(os.getenv("DAYTRADER_DAILY_DRAWDOWN_LIMIT_PCT", "3.0"))


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


def get_orders(status="all", limit=200):
    """Get historical orders on this agent's own account, most recent first."""
    url = f"{BASE_URL}/v2/orders"
    params = {"status": status, "limit": limit, "direction": "desc"}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_portfolio_history(period="1D", timeframe="5Min"):
    """This account's own equity history over a period."""
    url = f"{BASE_URL}/v2/account/portfolio/history"
    params = {"period": period, "timeframe": timeframe}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_market_clock():
    """Alpaca's own market-hours clock — source of truth for is_open /
    next_open / next_close, instead of hardcoding NYSE hours (handles
    holidays and early closes correctly)."""
    url = f"{BASE_URL}/v2/clock"
    response = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_bars(symbol, timeframe="5Min", limit=150):
    """Most recent `limit` intraday bars, oldest to newest. 10 calendar days
    is comfortably enough to cover 150 5-minute bars (~2 trading days'
    worth) even across a weekend or a holiday."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "desc",
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
    }
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    bars = data.get("bars") or []
    return list(reversed(bars))


def compute_rsi_series(closes, period=14):
    """Wilder's RSI, full series — rsi_series[-1] corresponds to closes[-1]
    (the most recent bar), same alignment convention as compute_ema_series
    below, so the two series' last elements are directly comparable."""
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi(ag, al):
        if al == 0:
            return 100.0
        return 100 - (100 / (1 + ag / al))

    series = [_rsi(avg_gain, avg_loss)]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        series.append(_rsi(avg_gain, avg_loss))
    return series


def compute_ema_series(closes, period):
    """Standard EMA, seeded with an SMA over the first `period` closes.
    ema[-1] always corresponds to closes[-1] regardless of period, so two
    EMA series of different periods can be compared directly at [-1]/[-2]
    without re-aligning indices."""
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def get_signal(symbol, timeframe="5Min", limit=150):
    """Buy signal — ALL of:
    - Fast EMA(9) crosses above Slow EMA(21) THIS bar (previous bar's
      fast<=slow, this bar's fast>slow) — a cross event, not a level check
      (a symbol already trading fast-above-slow with no fresh cross does
      not qualify).
    - RSI(14) is within [RSI_ENTRY_MIN, RSI_ENTRY_MAX] (default 40-65) on
      the same bar — a trend-continuation filter, not a reversal trigger;
      unlike the crypto scalper's RSI-oversold gate, this one is deliberately
      NOT an extreme reading, so there's no same-bar-conflict with the EMA
      cross the way there was for crypto (see crypto-scalper/CLAUDE.md
      "Multi-timeframe trend filter" for that unrelated problem — it doesn't
      apply here since this signal isn't an oversold-reversal bet).
    All computed on the same 5-minute timeframe (no multi-timeframe filter
    here, unlike the crypto scalper) — deliberately kept close to what was
    specified rather than added complexity that wasn't asked for. Revisit if
    backtesting surfaces a reason to add one.
    """
    bars = get_bars(symbol, timeframe=timeframe, limit=limit)
    closes = [b["c"] for b in bars]
    if len(closes) < SLOW_EMA_PERIOD + 2:
        return {"symbol": symbol, "error": "insufficient bars", "buy_signal": False}

    fast_ema = compute_ema_series(closes, FAST_EMA_PERIOD)
    slow_ema = compute_ema_series(closes, SLOW_EMA_PERIOD)
    rsi_series = compute_rsi_series(closes, RSI_PERIOD)

    if len(fast_ema) < 2 or len(slow_ema) < 2 or not rsi_series:
        return {"symbol": symbol, "error": "insufficient bars for indicators", "buy_signal": False}

    prev_fast, curr_fast = fast_ema[-2], fast_ema[-1]
    prev_slow, curr_slow = slow_ema[-2], slow_ema[-1]
    curr_rsi = rsi_series[-1]

    crossed_above = prev_fast <= prev_slow and curr_fast > curr_slow
    rsi_in_band = RSI_ENTRY_MIN <= curr_rsi <= RSI_ENTRY_MAX
    buy_signal = crossed_above and rsi_in_band

    return {
        "symbol": symbol,
        "buy_signal": buy_signal,
        "last_price": closes[-1],
        "fast_ema9": round(curr_fast, 4),
        "slow_ema21": round(curr_slow, 4),
        "crossed_above": crossed_above,
        "rsi14": round(curr_rsi, 2),
        "rsi_in_band": rsi_in_band,
    }


def get_exit_crossunder(symbol, timeframe="5Min", limit=150):
    """True if fast EMA(9) has just crossed below slow EMA(21) this bar —
    the 'opposite crossover' exit rule, checked independently of the
    stop-loss/take-profit levels below."""
    bars = get_bars(symbol, timeframe=timeframe, limit=limit)
    closes = [b["c"] for b in bars]
    fast_ema = compute_ema_series(closes, FAST_EMA_PERIOD)
    slow_ema = compute_ema_series(closes, SLOW_EMA_PERIOD)
    if len(fast_ema) < 2 or len(slow_ema) < 2:
        return False
    prev_fast, curr_fast = fast_ema[-2], fast_ema[-1]
    prev_slow, curr_slow = slow_ema[-2], slow_ema[-1]
    return prev_fast >= prev_slow and curr_fast < curr_slow


def compute_position_qty(entry_price, account_balance):
    """Position size = PER_TRADE_PCT_CAP of total account balance (default
    1%), per the operator's spec: 'risk a maximum of 1% of the total
    account balance per trade' — a straightforward position-size cap, same
    phrasing pattern as the equities Trader's '5% of buying power per
    symbol'. Combined with the 1% stop-loss, actual dollar loss if stopped
    out is ~1% x 1% = 0.01% of the account — conservative by construction,
    not the full-account sizing an earlier (incorrect) reading of this
    would have produced. See CLAUDE.md 'Position sizing' for that history.
    """
    target_notional = (PER_TRADE_PCT_CAP / 100) * account_balance
    if entry_price <= 0:
        return 0.0
    return target_notional / entry_price


def get_circuit_breaker_status():
    """Daily drawdown circuit breaker: halts new buys if equity has dropped
    more than DAILY_DRAWDOWN_LIMIT_PCT from today's opening equity. Matches
    the operator's spec exactly ('stop all trading entirely if portfolio
    drops 3% on the current day') — simpler than the other two bots'
    circuit breakers (no trailing-multi-day leg), since that's all that was
    asked for here. Exits are never blocked by this — same convention as
    the equities Trader and crypto scalper."""
    try:
        history = get_portfolio_history(period="1D", timeframe="5Min")
    except requests.RequestException as e:
        return {"halted": True, "reason": f"could not fetch portfolio history: {e}", "intraday_pct": None}
    equity = [e for e in (history.get("equity") or []) if e is not None]
    if len(equity) < 2:
        return {"halted": False, "reason": "insufficient history", "intraday_pct": None}
    open_equity, latest_equity = equity[0], equity[-1]
    if open_equity <= 0:
        return {"halted": False, "reason": "invalid opening equity", "intraday_pct": None}
    intraday_pct = (latest_equity - open_equity) / open_equity * 100
    halted = intraday_pct <= -DAILY_DRAWDOWN_LIMIT_PCT
    return {
        "halted": halted,
        "intraday_pct": round(intraday_pct, 2),
        "limit_pct": DAILY_DRAWDOWN_LIMIT_PCT,
        "reason": f"intraday drawdown {intraday_pct:.2f}% <= -{DAILY_DRAWDOWN_LIMIT_PCT}%" if halted else None,
    }


def get_deployed_notional():
    """Sum of today's buy-order notional from this account's own order
    history — used for the daily notional cap, same pattern as the other
    two bots."""
    today = datetime.now(timezone.utc).date()
    deployed = 0.0
    for o in get_orders(status="all", limit=200):
        if o.get("side") != "buy":
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
        if order_date != today:
            continue
        qty = float(o.get("filled_qty") or o.get("qty") or 0)
        price = float(o.get("filled_avg_price") or o.get("limit_price") or 0)
        deployed += qty * price
    return deployed


def get_day_trade_count(lookback_business_days=5):
    """Informational only (see CLAUDE.md 'Day-trade tracking') — paper
    accounts have no PDT rule, but this strategy closes same-day essentially
    every time, so tracking the trailing count now means no surprise if this
    account is ever made live."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_business_days * 1.6)
    fills = {}
    for o in get_orders(status="closed", limit=300):
        if o.get("status") != "filled":
            continue
        filled_at = o.get("filled_at")
        if not filled_at:
            continue
        try:
            ts = datetime.strptime(filled_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        day = ts.date()
        symbol = o.get("symbol")
        fills.setdefault((day, symbol), []).append(o.get("side"))
    count = 0
    for (_, _), sides in fills.items():
        if "buy" in sides and "sell" in sides:
            count += 1
    return count


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "account"
    symbol = sys.argv[2] if len(sys.argv) > 2 else None

    if action == "account":
        print(json.dumps(get_account()))
    elif action == "positions":
        print(json.dumps(get_positions()))
    elif action == "orders":
        status = symbol or "all"
        print(json.dumps(get_orders(status=status)))
    elif action == "clock":
        print(json.dumps(get_market_clock()))
    elif action == "bars" and symbol:
        print(json.dumps(get_bars(symbol)))
    elif action == "signal" and symbol:
        print(json.dumps(get_signal(symbol)))
    elif action == "circuit-breaker":
        print(json.dumps(get_circuit_breaker_status()))
    elif action == "deployed":
        print(json.dumps({"deployed_notional": get_deployed_notional()}))
    elif action == "day-trades":
        print(json.dumps({"day_trade_count": get_day_trade_count()}))
    else:
        print("Usage: research.py account | positions | orders [STATUS] | clock | bars SYMBOL | signal SYMBOL | circuit-breaker | deployed | day-trades")
