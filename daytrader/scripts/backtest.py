# daytrader/scripts/backtest.py
#
# Replays the live EMA9/21-cross + RSI-band signal and exit logic bar-by-bar
# over historical 5-minute equity bars, before trusting the strategy with
# even paper money. Same discipline as crypto-scalper/scripts/backtest.py:
# no look-ahead (bar i's decision only uses bars[0..i]), and the
# out-of-sample end_days_ago knob to test a window that had no part in
# tuning anything.
#
# Scope — read before trusting the output: signal-only. Per-trade % return,
# not dollar P&L; does NOT simulate the daily notional cap, max-positions
# cap, or cross-symbol allocation — those govern how much capital gets
# deployed, not whether an individual trade wins or loses.

import os
import sys
import json
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

import research

FAST_EMA_PERIOD = research.FAST_EMA_PERIOD
SLOW_EMA_PERIOD = research.SLOW_EMA_PERIOD
RSI_PERIOD = research.RSI_PERIOD
RSI_ENTRY_MIN = research.RSI_ENTRY_MIN
RSI_ENTRY_MAX = research.RSI_ENTRY_MAX
STOP_LOSS_PCT = research.STOP_LOSS_PCT
TAKE_PROFIT_PCT = research.TAKE_PROFIT_PCT

INDICATOR_WINDOW = 150  # rolling lookback used to (re)compute indicators at each step


def fetch_historical_bars(symbol, timeframe="5Min", lookback_days=30, end_days_ago=0):
    """Paginated historical fetch — a 30-day 5-minute history exceeds
    Alpaca's per-request bar limit. end_days_ago shifts the window
    backward for out-of-sample testing, same convention as the crypto
    scalper's backtest."""
    end = datetime.now(timezone.utc) - timedelta(days=end_days_ago)
    start = end - timedelta(days=lookback_days)
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    all_bars = []
    page_token = None
    while True:
        params = {
            "timeframe": timeframe,
            "adjustment": "raw",
            "feed": "iex",
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
        }
        if page_token:
            params["page_token"] = page_token
        response = requests.get(url, headers=research._headers(), params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        all_bars.extend(data.get("bars") or [])
        page_token = data.get("next_page_token")
        if not page_token:
            break
    all_bars.sort(key=lambda b: b.get("t", ""))
    return all_bars


def _bar_date(bar):
    return bar["t"][:10]


def simulate_symbol(symbol, bars):
    """Long-only. Enters on a fresh EMA9>EMA21 cross with RSI in-band; exits
    on the opposite crossunder, stop-loss, take-profit, or end of that
    trading day (mirrors the live agent's forced EOD flatten — an intraday
    strategy should never carry a simulated position into the next day's
    gap). No look-ahead: bar i's decision only ever sees bars[0..i]."""
    trades = []
    position = None  # {"entry_price", "entry_index", "entry_date"}

    for i in range(SLOW_EMA_PERIOD + 2, len(bars)):
        window = bars[max(0, i - INDICATOR_WINDOW + 1):i + 1]
        closes = [b["c"] for b in window]
        if len(closes) < SLOW_EMA_PERIOD + 2:
            continue

        fast_ema = research.compute_ema_series(closes, FAST_EMA_PERIOD)
        slow_ema = research.compute_ema_series(closes, SLOW_EMA_PERIOD)
        rsi_series = research.compute_rsi_series(closes, RSI_PERIOD)
        if len(fast_ema) < 2 or len(slow_ema) < 2 or not rsi_series:
            continue

        prev_fast, curr_fast = fast_ema[-2], fast_ema[-1]
        prev_slow, curr_slow = slow_ema[-2], slow_ema[-1]
        curr_rsi = rsi_series[-1]
        crossed_above = prev_fast <= prev_slow and curr_fast > curr_slow
        crossed_below = prev_fast >= prev_slow and curr_fast < curr_slow
        rsi_in_band = RSI_ENTRY_MIN <= curr_rsi <= RSI_ENTRY_MAX

        bar = bars[i]
        is_last_bar_of_day = (i == len(bars) - 1) or (_bar_date(bars[i + 1]) != _bar_date(bar))

        if position is not None:
            entry_price = position["entry_price"]
            change_pct = (bar["c"] - entry_price) / entry_price * 100
            stop_price = entry_price * (1 - STOP_LOSS_PCT / 100)
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
            exit_reason, exit_price = None, None

            # Same bar could touch both SL and TP — conservatively assume
            # the worse outcome (stop) hit first, same convention as the
            # crypto scalper's backtest.
            if bar["l"] <= stop_price:
                exit_reason, exit_price = "stop_loss", stop_price
            elif bar["h"] >= tp_price:
                exit_reason, exit_price = "take_profit", tp_price
            elif crossed_below:
                exit_reason, exit_price = "opposite_crossover", bar["c"]
            elif is_last_bar_of_day:
                exit_reason, exit_price = "eod_flatten", bar["c"]

            if exit_reason:
                pct_return = (exit_price - entry_price) / entry_price * 100
                trades.append({
                    "symbol": symbol,
                    "entry_time": position["entry_time"],
                    "exit_time": bar["t"],
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pct_return": round(pct_return, 3),
                    "exit_reason": exit_reason,
                })
                position = None
                continue  # don't re-enter on the same bar we just exited

        if position is None and not is_last_bar_of_day and crossed_above and rsi_in_band:
            position = {"entry_price": bar["c"], "entry_time": bar["t"]}

    return trades


def run_backtest(lookback_days=30, end_days_ago=0, symbols=None):
    if symbols is None:
        with open(os.path.join(os.path.dirname(__file__), "..", "watchlist.json")) as f:
            symbols = json.load(f)["symbols"]

    all_trades = []
    per_symbol = {}
    for symbol in symbols:
        bars = fetch_historical_bars(symbol, lookback_days=lookback_days, end_days_ago=end_days_ago)
        trades = simulate_symbol(symbol, bars)
        all_trades.extend(trades)
        total_return = sum(t["pct_return"] for t in trades)
        wins = [t for t in trades if t["pct_return"] > 0]
        per_symbol[symbol] = {
            "bars_fetched": len(bars),
            "trade_count": len(trades),
            "win_count": len(wins),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "total_pct_return": round(total_return, 2),
        }

    exit_breakdown = {}
    for t in all_trades:
        exit_breakdown.setdefault(t["exit_reason"], []).append(t["pct_return"])
    avg_return_by_exit_reason = {
        reason: round(sum(vals) / len(vals), 3) for reason, vals in exit_breakdown.items()
    }

    total_trades = len(all_trades)
    total_wins = len([t for t in all_trades if t["pct_return"] > 0])
    return {
        "lookback_days": lookback_days,
        "end_days_ago": end_days_ago,
        "symbols_tested": symbols,
        "total_trades": total_trades,
        "overall_win_rate_pct": round(total_wins / total_trades * 100, 1) if total_trades else None,
        "avg_pct_return_per_trade": round(sum(t["pct_return"] for t in all_trades) / total_trades, 3) if total_trades else None,
        "sum_pct_return_all_trades": round(sum(t["pct_return"] for t in all_trades), 2),
        "avg_return_by_exit_reason": avg_return_by_exit_reason,
        "per_symbol": per_symbol,
    }


def detail_symbol(symbol, lookback_days=30, end_days_ago=0):
    bars = fetch_historical_bars(symbol, lookback_days=lookback_days, end_days_ago=end_days_ago)
    trades = simulate_symbol(symbol, bars)
    return {"symbol": symbol, "bars_fetched": len(bars), "trades": trades}


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else None

    if action == "detail":
        symbol = sys.argv[2]
        lookback = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        print(json.dumps(detail_symbol(symbol, lookback_days=lookback), indent=2))
    else:
        lookback = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit() else 30
        end_days_ago = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].lstrip("-").isdigit() else 0
        print(json.dumps(run_backtest(lookback_days=lookback, end_days_ago=end_days_ago), indent=2))
