# daytrader/scripts/research.py
#
# Read-only data + signal computation for the intraday equities day-trading
# agent. A third, independent strategy in this repo — separate from both
# the equities Trader (hourly, hold-for-days, research-driven) and the
# crypto scalper (5-min, 24/7, RSI/EMA scalp).
#
# Shares the crypto scalper's Alpaca account (DAYTRADER_APCA_* env vars
# are populated from the same credentials as CRYPTO_APCA_* at the
# workflow level — operator's explicit choice, 2026-08-07, see
# CLAUDE.md "Sharing the crypto scalper's Alpaca account"). Safe because
# equities and crypto symbols never collide (bare tickers vs "BTC/USD"),
# but every function here that reads order history accepts an optional
# `symbols` filter so crypto scalper's own orders never count against
# this agent's caps — always pass it when the account is shared.
#
# Market-data calls (get_bars) hit data.alpaca.markets, which accepts any
# valid Alpaca key pair regardless of which paper account it's tied to —
# same endpoint/pattern as the equities Trader's scripts/research.py.

import os
import re
import sys
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

EASTERN = ZoneInfo("US/Eastern")

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

# ATR-based stop/take-profit (replaced the flat STOP_LOSS_PCT/TAKE_PROFIT_PCT
# below on 2026-08-07 — see CLAUDE.md "ATR-based stop/take-profit"). A flat
# percentage doesn't account for each stock's own volatility; sizing the
# stop/TP off that pair's actual 5-min ATR at entry time does, same fix the
# crypto scalper already needed for the same reason.
ATR_PERIOD = int(os.getenv("DAYTRADER_ATR_PERIOD", "14"))
ATR_STOP_MULTIPLIER = float(os.getenv("DAYTRADER_ATR_STOP_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.getenv("DAYTRADER_ATR_TP_MULTIPLIER", "3.0"))
# ATR volatility filter (added 2026-08-07): only trade when the current
# bar's ATR is strictly above its own 20-period SMA — i.e. only in
# above-average-movement conditions. Targets the whipsaw problem found in
# the first backtest directly: most losing trades exited via the opposite
# crossover (a small reversal), not the stop-loss, meaning most losses came
# from trading in flat/choppy stretches where the EMA cross had nothing
# real behind it. This is the OPPOSITE condition from the crypto scalper's
# ATR spike filter (which BLOCKS trades above a volatility threshold,
# because its RSI-oversold signal gets unreliable during a shock) — that
# filter exists to avoid trading during a spike; this one exists to avoid
# trading during the absence of one. Different strategy, different reason,
# not a contradiction.
ATR_SMA_PERIOD = int(os.getenv("DAYTRADER_ATR_SMA_PERIOD", "20"))

# Flat fallbacks — no longer the operative stop/TP (see ATR-based sizing
# above), kept only for any position opened before this change with no
# recoverable ATR-based levels.
STOP_LOSS_PCT = float(os.getenv("DAYTRADER_STOP_LOSS_PCT", "1.0"))
TAKE_PROFIT_PCT = float(os.getenv("DAYTRADER_TAKE_PROFIT_PCT", "2.0"))
# "Risk a maximum of 1% of the total account balance per trade" — a
# position-size cap, not a stop-distance-scaled dollar-risk target. See
# CLAUDE.md "Position sizing" for why this matters. Deliberately NOT tied
# to the ATR-based stop distance above — see CLAUDE.md "Why position
# sizing stays decoupled from the ATR stop" for why coupling them (as
# originally proposed) blows up to 200-650% of account per trade.
PER_TRADE_PCT_CAP = float(os.getenv("DAYTRADER_PER_TRADE_PCT_CAP", "1.0"))
DAILY_DRAWDOWN_LIMIT_PCT = float(os.getenv("DAYTRADER_DAILY_DRAWDOWN_LIMIT_PCT", "3.0"))

# Time-of-day filter (added 2026-08-07): new entries only inside these two
# US/Eastern windows — the opening-range chop (9:30-9:45) and the midday
# lull (11:30-15:00) are excluded entirely, leaving the post-opening-range
# morning trend window and a narrow pre-close window. Exits are never
# gated by this — same "a filter only blocks new buys, never a mandatory
# exit" convention as every other gate in this project.
ENTRY_WINDOWS_ET = [
    ((9, 45), (11, 30)),
    ((15, 0), (15, 45)),
]


def is_in_entry_window(dt_utc):
    """True if dt_utc (a timezone-aware UTC datetime) falls within one of
    ENTRY_WINDOWS_ET, converted to US/Eastern (handles EDT/EST correctly,
    unlike a fixed UTC offset)."""
    et = dt_utc.astimezone(EASTERN)
    minutes = et.hour * 60 + et.minute
    for (start_h, start_m), (end_h, end_m) in ENTRY_WINDOWS_ET:
        if start_h * 60 + start_m <= minutes <= end_h * 60 + end_m:
            return True
    return False


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


def get_signal(symbol, timeframe="5Min", limit=150, now=None):
    """Buy signal — ALL of:
    - Fast EMA(9) crosses above Slow EMA(21) THIS bar (previous bar's
      fast<=slow, this bar's fast>slow) — a cross event, not a level check
      (a symbol already trading fast-above-slow with no fresh cross does
      not qualify).
    - RSI(14) is within [RSI_ENTRY_MIN, RSI_ENTRY_MAX] (default 40-65) on
      the same bar — a trend-continuation filter, not a reversal trigger;
      unlike the crypto scalper's RSI-oversold gate, this one is deliberately
      NOT an extreme reading, so there's no same-bar-conflict with the EMA
      cross the way there was for crypto.
    - ATR(14) is strictly above its own 20-period SMA (added 2026-08-07) —
      only trade in above-average-movement conditions. See ATR_SMA_PERIOD
      above for why.
    - Current time (US/Eastern) falls in one of ENTRY_WINDOWS_ET (added
      2026-08-07). `now` defaults to the real current time; the backtest
      passes each historical bar's own timestamp instead, so the exact
      same rule replays correctly against history.
    All computed on the same 5-minute timeframe (no multi-timeframe filter
    here, unlike the crypto scalper) — deliberately kept close to what was
    specified rather than added complexity that wasn't asked for.
    """
    now = now or datetime.now(timezone.utc)
    bars = get_bars(symbol, timeframe=timeframe, limit=limit)
    closes = [b["c"] for b in bars]
    if len(closes) < SLOW_EMA_PERIOD + 2:
        return {"symbol": symbol, "error": "insufficient bars", "buy_signal": False}

    fast_ema = compute_ema_series(closes, FAST_EMA_PERIOD)
    slow_ema = compute_ema_series(closes, SLOW_EMA_PERIOD)
    rsi_series = compute_rsi_series(closes, RSI_PERIOD)
    atr_series = compute_atr_series(bars, ATR_PERIOD)

    if len(fast_ema) < 2 or len(slow_ema) < 2 or not rsi_series:
        return {"symbol": symbol, "error": "insufficient bars for indicators", "buy_signal": False}
    if len(atr_series) < ATR_SMA_PERIOD:
        return {"symbol": symbol, "error": "insufficient bars for ATR filter", "buy_signal": False}

    prev_fast, curr_fast = fast_ema[-2], fast_ema[-1]
    prev_slow, curr_slow = slow_ema[-2], slow_ema[-1]
    curr_rsi = rsi_series[-1]
    curr_atr = atr_series[-1]
    atr_sma = sum(atr_series[-ATR_SMA_PERIOD:]) / ATR_SMA_PERIOD

    crossed_above = prev_fast <= prev_slow and curr_fast > curr_slow
    rsi_in_band = RSI_ENTRY_MIN <= curr_rsi <= RSI_ENTRY_MAX
    atr_above_average = curr_atr > atr_sma
    in_entry_window = is_in_entry_window(now)
    buy_signal = crossed_above and rsi_in_band and atr_above_average and in_entry_window

    entry_price = closes[-1]
    stop_price, tp_price = compute_atr_based_stop_tp(entry_price, curr_atr)

    return {
        "symbol": symbol,
        "buy_signal": buy_signal,
        "last_price": entry_price,
        "fast_ema9": round(curr_fast, 4),
        "slow_ema21": round(curr_slow, 4),
        "crossed_above": crossed_above,
        "rsi14": round(curr_rsi, 2),
        "rsi_in_band": rsi_in_band,
        "atr14": round(curr_atr, 4),
        "atr_sma20": round(atr_sma, 4),
        "atr_above_average": atr_above_average,
        "in_entry_window": in_entry_window,
        "stop_price": round(stop_price, 2),
        "tp_price": round(tp_price, 2),
    }


def compute_atr_series(bars, period=14):
    """Wilder-smoothed ATR(14) from OHLC bars — same formula as the crypto
    scalper's. atr[-1] corresponds to bars[-1] (needs one fewer element
    than `bars` for true range, then `period` more to seed Wilder
    smoothing), same alignment convention as compute_ema_series /
    compute_rsi_series above."""
    if len(bars) < period + 2:
        return []
    trs = []
    for i in range(1, len(bars)):
        high, low, prev_close = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return []
    atr = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atr.append((atr[-1] * (period - 1) + tr) / period)
    return atr


def compute_atr_based_stop_tp(entry_price, atr14):
    """Absolute stop/take-profit prices from that pair's own ATR at entry
    time — ATR_STOP_MULTIPLIER (default 1.5x) / ATR_TP_MULTIPLIER (default
    3.0x), preserving a 1:2 risk:reward ratio. Long-only, so stop is below
    entry and TP is above it. Returns (stop_price, tp_price)."""
    stop_price = entry_price - (ATR_STOP_MULTIPLIER * atr14)
    tp_price = entry_price + (ATR_TP_MULTIPLIER * atr14)
    return stop_price, tp_price


def encode_trade_params(stop_price, tp_price):
    """Packs a plain (non-bracket) order's ATR-derived stop/TP prices into
    the entry order's client_order_id.

    Needed because Alpaca rejects fractional-quantity bracket orders
    ('fractional orders must be simple orders', confirmed 2026-08-07 —
    this account's $1,000 equity means a 1%-of-account position is under
    1 share for every $100+ watchlist symbol, so EVERY trade this account
    can actually afford was being rejected). Sub-1-share entries fall back
    to a plain order instead, which means there's no broker-side bracket
    leg to recover the stop/TP from later — so they're persisted here,
    same technique the crypto scalper already uses for the identical
    problem. Encoded in cents to avoid float-formatting ambiguity."""
    return f"dt-sl{round(stop_price * 100)}-tp{round(tp_price * 100)}"


def decode_trade_params(client_order_id):
    """Inverse of encode_trade_params(). Returns None if client_order_id
    is missing or wasn't produced by it (e.g. a bracket-order entry, which
    doesn't need this — its stop/TP live as broker-managed child orders)."""
    if not client_order_id:
        return None
    m = re.match(r"^dt-sl(-?\d+)-tp(-?\d+)$", client_order_id)
    if not m:
        return None
    return {"stop_price": int(m.group(1)) / 100, "tp_price": int(m.group(2)) / 100}


def get_position_entry_order(symbol):
    """Most recent filled BUY order for this symbol — used to recover a
    plain-order position's encoded stop/TP (see decode_trade_params)."""
    for o in get_orders(status="closed", limit=200):
        if o.get("symbol") == symbol and o.get("side") == "buy" and o.get("status") == "filled":
            return o
    return None


def has_open_bracket_legs(symbol):
    """True if `symbol` currently has an open bracket-order child leg
    (stop_loss/take_profit) — i.e. its entry was a whole-share bracket
    order and the broker is still managing its exit. False for a plain
    fractional-qty entry, which has no such legs (see encode_trade_params
    for why fractional entries can't use bracket orders at all)."""
    url = f"{BASE_URL}/v2/orders"
    params = {"status": "open", "symbols": symbol}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return any(o.get("order_class") == "bracket" for o in response.json())


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


def get_deployed_notional(symbols=None):
    """Sum of today's buy-order notional from this account's own order
    history — used for the daily notional cap, same pattern as the other
    two bots.

    `symbols`, if given, restricts this to that set — REQUIRED when this
    account is shared with the crypto scalper (see CLAUDE.md "Sharing the
    crypto scalper's Alpaca account"), otherwise crypto buy orders would
    count against daytrader's own daily notional cap and vice versa."""
    today = datetime.now(timezone.utc).date()
    deployed = 0.0
    for o in get_orders(status="all", limit=300):
        if symbols is not None and o.get("symbol") not in symbols:
            continue
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


def get_day_trade_count(lookback_business_days=5, symbols=None):
    """Informational only (see CLAUDE.md 'Day-trade tracking') — paper
    accounts have no PDT rule, but this strategy closes same-day essentially
    every time, so tracking the trailing count now means no surprise if this
    account is ever made live. `symbols` filters to daytrader's own
    watchlist when the account is shared with the crypto scalper."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_business_days * 1.6)
    fills = {}
    for o in get_orders(status="closed", limit=300):
        if symbols is not None and o.get("symbol") not in symbols:
            continue
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
        with open(os.path.join(os.path.dirname(__file__), "..", "watchlist.json")) as f:
            wl = set(json.load(f)["symbols"])
        print(json.dumps({"deployed_notional": get_deployed_notional(symbols=wl)}))
    elif action == "day-trades":
        with open(os.path.join(os.path.dirname(__file__), "..", "watchlist.json")) as f:
            wl = set(json.load(f)["symbols"])
        print(json.dumps({"day_trade_count": get_day_trade_count(symbols=wl)}))
    else:
        print("Usage: research.py account | positions | orders [STATUS] | clock | bars SYMBOL | signal SYMBOL | circuit-breaker | deployed | day-trades")
