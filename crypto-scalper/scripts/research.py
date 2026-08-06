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
import re
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

# Fallback TP/SL when a position's ATR-based params can't be recovered
# (see compute_atr_based_stop_tp_pct below for the operative sizing) — a
# position opened before this feature existed, or an order placed some
# other way without our client_order_id encoding, falls back to these
# flat defaults rather than erroring or guessing.
PROFIT_TARGET_PCT = float(os.getenv("CRYPTO_PROFIT_TARGET_PCT", "1.5"))
STOP_LOSS_PCT = float(os.getenv("CRYPTO_STOP_LOSS_PCT", "0.75"))
MAX_HOLD_HOURS = float(os.getenv("CRYPTO_MAX_HOLD_HOURS", "4"))
TARGET_RISK_PCT = float(os.getenv("CRYPTO_TARGET_RISK_PCT", "1.0"))
ATR_STOP_MULTIPLIER = float(os.getenv("CRYPTO_ATR_STOP_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.getenv("CRYPTO_ATR_TP_MULTIPLIER", "3.0"))


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


def compute_rsi_series(closes, period=14):
    """Wilder's RSI, full series (not just the latest value) — aligned to
    closes[period:], i.e. rsi_series[k] corresponds to closes[period + k].
    Needed to check whether RSI touched oversold at ANY point in a lookback
    window, not just the current bar — see get_signal()'s "RSI lookback"
    note for why same-bar-only checking doesn't work in practice."""
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


def compute_rsi(closes, period=14):
    """Wilder's RSI as of the most recent close, or None if there isn't
    enough history. Thin wrapper over compute_rsi_series()."""
    series = compute_rsi_series(closes, period)
    return series[-1] if series else None


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


def compute_atr_series(bars, period=14):
    """Wilder-smoothed ATR(14) from OHLC bars. True range for bar i needs
    bar i-1's close, so the series has one fewer element than `bars`;
    Wilder smoothing then needs `period` of those to seed. Returned series
    is aligned to bars[period:], i.e. atr[-1] corresponds to bars[-1]."""
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


ATR_SPIKE_MULTIPLE = float(os.getenv("CRYPTO_ATR_SPIKE_MULTIPLE", "2.0"))


def get_1h_trend_alignment(pair, limit=80):
    """EMA(20) > EMA(50) computed on 1-HOUR bars — deliberately a different,
    slower timeframe from the 5-minute entry signal (changed 2026-08-05,
    see CLAUDE.md "Multi-timeframe trend filter"). A backtest found the
    original same-timeframe design (both on 5-min bars) fired on ZERO
    trades across 60 days / 136k bars: a real RSI(14)-oversold dip on
    5-minute bars usually also drags the fast-reacting 5-min EMA(20) below
    the 5-min EMA(50) at the same time, making "oversold" and "still
    bullish-aligned" nearly mutually exclusive on one fast timeframe. A
    1-hour EMA reacts far more slowly, so a brief 5-minute dip doesn't also
    flip the hourly trend — this keeps the original intent (don't buy dips
    in a genuine downtrend) while actually being satisfiable alongside a
    5-minute oversold reading.

    Only uses FULLY COMPLETED hourly bars — Alpaca's most recent returned
    bar for a live "now" fetch is very likely the currently-forming
    (incomplete) hour, and using its still-changing values would be the
    same "don't count today's still-forming data" mistake this project
    avoids elsewhere (see the equities bot's falling-knife rule)."""
    bars = get_crypto_bars(pair, timeframe="1Hour", limit=limit + 2)
    now = datetime.now(timezone.utc)
    completed = []
    for b in bars:
        try:
            bar_start = datetime.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        if bar_start + timedelta(hours=1) <= now:
            completed.append(b)

    if len(completed) < 50:
        return {"error": f"only {len(completed)} completed 1-hour bars available, need at least 50", "ema20_1h": None, "ema50_1h": None, "bullish": False}

    closes = [b["c"] for b in completed]
    ema20_series = compute_ema_series(closes, 20)
    ema50_series = compute_ema_series(closes, 50)
    if len(ema20_series) < 1 or not ema50_series:
        return {"error": "insufficient 1-hour data for EMA20/50", "ema20_1h": None, "ema50_1h": None, "bullish": False}

    ema20_1h, ema50_1h = ema20_series[-1], ema50_series[-1]
    return {"error": None, "ema20_1h": round(ema20_1h, 4), "ema50_1h": round(ema50_1h, 4), "bullish": ema20_1h > ema50_1h}


RSI_LOOKBACK_BARS = int(os.getenv("CRYPTO_RSI_LOOKBACK_BARS", "12"))  # 12 x 5-min = 1 hour


def get_signal(pair, timeframe="5Min", limit=80):
    """Buy trigger, precisely — ALL of the following:
    - RSI(14) touched below 30 (oversold) at some point in the last
      `CRYPTO_RSI_LOOKBACK_BARS` bars (default 12 = 1 hour), on 5-minute
      bars — NOT required on the same bar as the cross below. A diagnostic
      over 60 days of real BTC/USD data found RSI is reliably back in the
      50-65 range by the time price actually crosses back above EMA(20)
      (1,193 cross events, RSI at every single one already >50) — RSI is a
      fast oscillator that recovers well before a lagging 20-period EMA
      does, so requiring both on the identical bar produced ZERO trades
      across 136k bars total. This lookback window fixes that while
      keeping the original intent: a real oversold dip, confirmed by a
      recovery signal, not necessarily on the exact same candle.
    - Close crosses above EMA(20), on 5-minute bars, on the CURRENT bar —
      previous bar's close was at or below its EMA, this bar's close is
      above it. A cross event, not merely "currently above".
    - EMA(20) > EMA(50) on the 1-HOUR timeframe ("bullish alignment") — a
      deliberately slower timeframe than the entry signal above, so a
      5-minute dip doesn't also disqualify itself. See
      get_1h_trend_alignment()'s docstring for why this isn't computed on
      the same 5-minute bars as everything else.
    - NOT an ATR volatility spike: ATR(14) on 5-minute bars is not more
      than `CRYPTO_ATR_SPIKE_MULTIPLE` (default 2x) its own trailing
      20-period average — skips entries during a volatility/news shock,
      where RSI and EMA behave unreliably.

    Needs more history than RSI/EMA20 alone (EMA50 seed + a 20-period ATR
    average), hence the higher default `limit` than earlier versions of
    this function used.
    """
    bars = get_crypto_bars(pair, timeframe, limit)
    closes = [b["c"] for b in bars]
    if len(closes) < 55:
        return {"pair": pair, "error": f"only {len(closes)} bars available, need at least 55"}

    rsi_series = compute_rsi_series(closes, 14)
    ema20_series = compute_ema_series(closes, 20)
    atr_series = compute_atr_series(bars, 14)
    if not rsi_series or len(ema20_series) < 2 or len(atr_series) < 20:
        return {"pair": pair, "error": "insufficient data for RSI/EMA/ATR"}

    trend = get_1h_trend_alignment(pair)
    if trend["error"]:
        return {"pair": pair, "error": f"1-hour trend check failed: {trend['error']}"}

    rsi = rsi_series[-1]
    rsi_lookback_window = rsi_series[-RSI_LOOKBACK_BARS:]
    rsi_min_in_lookback = min(rsi_lookback_window)
    rsi_oversold = rsi < 30
    rsi_recently_oversold = rsi_min_in_lookback < 30

    prev_close, curr_close = closes[-2], closes[-1]
    prev_ema20, curr_ema20 = ema20_series[-2], ema20_series[-1]
    crossed_above = prev_close <= prev_ema20 and curr_close > curr_ema20
    ema_alignment_bullish = trend["bullish"]

    curr_atr = atr_series[-1]
    atr_avg20 = sum(atr_series[-20:]) / 20
    atr_volatility_spike = atr_avg20 > 0 and curr_atr > ATR_SPIKE_MULTIPLE * atr_avg20

    buy_signal = rsi_recently_oversold and crossed_above and ema_alignment_bullish and not atr_volatility_spike

    return {
        "pair": pair,
        "rsi": round(rsi, 2),
        "rsi_oversold": rsi_oversold,
        "rsi_min_in_lookback": round(rsi_min_in_lookback, 2),
        "rsi_recently_oversold": rsi_recently_oversold,
        "ema20": round(curr_ema20, 4),
        "ema20_1h": trend["ema20_1h"],
        "ema50_1h": trend["ema50_1h"],
        "ema_alignment_bullish": ema_alignment_bullish,
        "prev_close": prev_close,
        "curr_close": curr_close,
        "crossed_above_ema": crossed_above,
        "atr14": round(curr_atr, 6),
        "atr14_avg20": round(atr_avg20, 6),
        "atr_volatility_spike": atr_volatility_spike,
        "buy_signal": buy_signal,
    }


def compute_atr_based_stop_tp_pct(entry_price, atr14):
    """Stop-loss/take-profit as a % of entry price, sized off THIS pair's
    own ATR(14) at entry time rather than one flat percentage for every
    pair (changed 2026-08-06). An out-of-sample backtest found the flat
    0.75%/1.5% design didn't fail because of any one specific pair — AVAX
    was the worst performer in one 60-day window, but DOT was equally bad
    (74% stop-loss rate) in a different window. The pattern rotates with
    whichever pair happens to be most volatile relative to a fixed
    percentage stop in the period being tested; removing pairs one at a
    time doesn't fix that. Scaling the stop to each pair's own current
    volatility does: a naturally choppier pair gets proportionally more
    room, a calmer one gets a tighter stop, so the SAME relative risk
    applies regardless of which pair happens to be volatile right now.
    `CRYPTO_ATR_STOP_MULTIPLIER`/`CRYPTO_ATR_TP_MULTIPLIER` (default 1.5x/
    3x, preserving the original 2:1 reward:risk ratio) are tunable via env
    var. Returns (None, None) if atr14 or entry_price is missing/zero.
    """
    if not entry_price or not atr14 or entry_price <= 0:
        return None, None
    stop_pct = round(ATR_STOP_MULTIPLIER * atr14 / entry_price * 100, 4)
    tp_pct = round(ATR_TP_MULTIPLIER * atr14 / entry_price * 100, 4)
    return stop_pct, tp_pct


def encode_trade_params(stop_pct, tp_pct):
    """Packs this trade's entry-time ATR-based stop/TP into a
    client_order_id string. Alpaca doesn't otherwise let us attach custom
    metadata to a position, and per-trade parameters (unlike the old flat
    globals) can't just be read back from a constant — this is the only
    way to recover "what was THIS trade's stop/TP" on a later run without
    a separate local state file that could desync from Alpaca's own
    records (same "Alpaca's own history is the source of truth" principle
    used everywhere else in this project). Encoded as integer basis-
    points-ish (2 decimal places of %) to keep it short and alphanumeric."""
    return f"cs-sl{int(round(stop_pct * 100))}-tp{int(round(tp_pct * 100))}"


def decode_trade_params(client_order_id):
    """Inverse of encode_trade_params(). Returns (stop_pct, tp_pct), or
    (None, None) if the id is missing/malformed — e.g. a position opened
    before this feature existed. Callers fall back to the flat
    STOP_LOSS_PCT/PROFIT_TARGET_PCT defaults in that case."""
    if not client_order_id:
        return None, None
    m = re.match(r"^cs-sl(-?\d+)-tp(-?\d+)$", client_order_id)
    if not m:
        return None, None
    return int(m.group(1)) / 100, int(m.group(2)) / 100


def compute_risk_based_qty(equity, entry_price, stop_loss_pct, target_risk_pct):
    """Quantity such that (entry_price - stop_price) * qty == target_risk_pct
    of equity, where stop_price = entry_price * (1 - stop_loss_pct/100) —
    i.e. size so a stop-out loses exactly target_risk_pct of equity. This is
    informational/a ceiling, NOT the operative cap: the existing notional
    caps (CRYPTO_MAX_ORDER_NOTIONAL, CRYPTO_PER_TRADE_PCT_CAP, daily/weekly
    headroom) still apply via min(), and given the current 0.75% stop, a 1%
    risk target implies a position far larger than those caps allow — so in
    practice the notional caps bind, not this. See crypto-scalper/CLAUDE.md
    "Risk-based sizing" for the numbers and why that's a deliberate choice,
    not a bug. Returns 0 if entry_price or stop_loss_pct is 0 (can't size
    against a zero stop distance).
    """
    if not entry_price or not stop_loss_pct or not equity:
        return 0.0
    risk_per_unit = entry_price * (stop_loss_pct / 100)
    if risk_per_unit <= 0:
        return 0.0
    return (target_risk_pct / 100 * equity) / risk_per_unit


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


def get_position_entry_order(symbol):
    """Most recent filled buy order for `symbol` — under the single-entry-
    per-pair rule this is the entry order for whatever position is
    currently open. Returns the full order dict so callers can pull
    filled_at (max-hold-time) or client_order_id (ATR-based stop/TP) as
    needed, rather than each maintaining its own separate lookup."""
    for o in get_orders(status="closed", limit=200):
        if o.get("symbol") != symbol or o.get("side") != "buy":
            continue
        if o.get("status") != "filled":
            continue
        return o
    return None


def get_position_entry_time(symbol):
    """Timestamp of the most recent filled buy for `symbol` — under the
    single-entry-per-pair rule this is the entry time for whatever position
    is currently open, used for the max-hold-time safeguard."""
    order = get_position_entry_order(symbol)
    if not order:
        return None
    return order.get("filled_at") or order.get("submitted_at")


def get_position_trade_params(symbol, entry_order=None):
    """(stop_pct, tp_pct) for the currently open position in `symbol`,
    recovered from the entry order's client_order_id if it was placed with
    ATR-based sizing, else the flat STOP_LOSS_PCT/PROFIT_TARGET_PCT
    defaults (a position opened before this feature existed, or via a
    manual/interactive order without the encoding)."""
    if entry_order is None:
        entry_order = get_position_entry_order(symbol)
    stop_pct, tp_pct = decode_trade_params(entry_order.get("client_order_id") if entry_order else None)
    if stop_pct is None or tp_pct is None:
        return STOP_LOSS_PCT, PROFIT_TARGET_PCT
    return stop_pct, tp_pct


def get_exit_flags():
    """Per-position exit check for open crypto positions: profit target,
    stop loss, and max-hold-time, evaluated in that priority order. Mirrors
    the equities per-position stop-loss in spirit (mechanical, fires on
    price/time alone) but with this strategy's own thresholds — sized per
    trade off that trade's own entry-time ATR, not one flat percentage for
    every position (see compute_atr_based_stop_tp_pct)."""
    flags = []
    for p in get_crypto_positions():
        symbol = p.get("symbol")
        plpc = float(p.get("unrealized_plpc", 0)) * 100
        entry_order = get_position_entry_order(symbol)
        stop_pct, tp_pct = get_position_trade_params(symbol, entry_order=entry_order)

        action, reason = None, None
        if plpc >= tp_pct:
            action, reason = "close_profit_target", f"unrealized {plpc:.2f}% >= +{tp_pct}% target"
        elif plpc <= -stop_pct:
            action, reason = "close_stop_loss", f"unrealized {plpc:.2f}% <= -{stop_pct}% stop"
        else:
            entry_ts = entry_order.get("filled_at") or entry_order.get("submitted_at") if entry_order else None
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
                "stop_pct": stop_pct,
                "tp_pct": tp_pct,
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


_CATEGORY_MAP = None


def _load_categories():
    global _CATEGORY_MAP
    if _CATEGORY_MAP is None:
        path = os.path.join(os.path.dirname(__file__), "..", "categories.json")
        with open(path) as f:
            _CATEGORY_MAP = json.load(f)
    return _CATEGORY_MAP


def get_category(pair):
    return _load_categories().get(pair, "Unknown")


def check_category_cap(pair, max_per_category=2):
    """Would opening a NEW position in `pair` push its category above
    `max_per_category` of the open positions? Mirrors the equities bot's
    sector cap exactly — only relevant for pairs not already held; adding
    to an existing position doesn't change the category count."""
    category = get_category(pair)
    positions = get_crypto_positions()
    held_symbols = {p["symbol"] for p in positions}
    if pair in held_symbols:
        return {"pair": pair, "category": category, "blocked": False, "reason": None}
    same_category_count = sum(1 for p in positions if get_category(p["symbol"]) == category)
    blocked = same_category_count >= max_per_category
    return {
        "pair": pair,
        "category": category,
        "current_same_category_positions": same_category_count,
        "blocked": blocked,
        "reason": f"already {same_category_count} open position(s) in {category}" if blocked else None,
    }


def build_decision_envelope(pair, action, signal, account, positions, invalidation_price, reason, risk_sizing=None):
    """Assemble the strict JSON decision record for one pair's decision
    this run: portfolio cash/NLV, open positions & exposure by category,
    technical validation (RSI/EMA20/EMA50 alignment/ATR bounds), the
    invalidation (hard stop) price, and — for NEW_TRADE decisions —
    risk_sizing (target vs. actual risk %, and which constraint bound the
    final quantity). Logged for every pair on every run — NEW_TRADE, HOLD,
    and CLOSE alike — not just the ones that traded."""
    exposure_by_category = {}
    for p in positions:
        cat = get_category(p.get("symbol"))
        exposure_by_category[cat] = exposure_by_category.get(cat, 0) + float(p.get("market_value", 0))

    technical_validation = None
    if signal and not signal.get("error"):
        technical_validation = {
            "rsi14": signal.get("rsi"),
            "rsi_oversold": signal.get("rsi_oversold"),
            "ema20": signal.get("ema20"),
            "ema20_1h": signal.get("ema20_1h"),
            "ema50_1h": signal.get("ema50_1h"),
            "ema_alignment_bullish": signal.get("ema_alignment_bullish"),
            "crossed_above_ema20": signal.get("crossed_above_ema"),
            "atr14": signal.get("atr14"),
            "atr14_avg20": signal.get("atr14_avg20"),
            "atr_volatility_spike": signal.get("atr_volatility_spike"),
        }
    elif signal and signal.get("error"):
        technical_validation = {"error": signal["error"]}

    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pair": pair,
        "category": get_category(pair),
        "action": action,
        "portfolio": {
            "cash": float(account.get("cash", 0)) if account else None,
            "net_liquidation_value": float(account.get("equity", 0)) if account else None,
        },
        "positions": {
            "open_count": len(positions),
            "exposure_by_category": {k: round(v, 2) for k, v in exposure_by_category.items()},
        },
        "technical_validation": technical_validation,
        "invalidation_price": invalidation_price,
        "risk_sizing": risk_sizing,
        "reason": reason,
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
        max_positions = int(os.getenv("CRYPTO_MAX_POSITIONS", "5"))
        print(json.dumps(check_max_positions(pair, max_positions)))
    elif action == "category-cap" and pair:
        print(json.dumps(check_category_cap(pair)))
    elif action == "account":
        print(json.dumps(get_account()))
    else:
        print("Usage: research.py signal PAIR | bars PAIR | positions | deployed | exit-flags | circuit-breaker | max-positions PAIR | category-cap PAIR | account")
