# crypto-scalper/scripts/backtest.py
#
# Backtests the exact signal logic in research.get_signal() (RSI(14)
# touched <30 within a lookback window + EMA20 cross-above on 5-minute
# bars + EMA20>EMA50 on 1-HOUR bars + no ATR volatility spike) and the
# exact exit logic in research.get_exit_flags() (+1.5% TP / -0.75% SL / 4h
# timeout), replayed bar-by-bar over historical candles with no
# look-ahead: at 5-minute bar i, only bars[0..i] (and only 1-hour bars
# that had actually closed by that point in time) are used to decide
# anything about bar i.
#
# Two fixes were needed to get here, both discovered by backtesting the
# PRIOR version and finding zero trades, not by guessing:
# 1. The original same-timeframe design (RSI + both EMAs all on 5-minute
#    bars) produced ZERO trades across 60 days / 136k bars — a real
#    5-minute RSI-oversold dip usually also drags the fast 5-min EMA(20)
#    below the 5-min EMA(50) at the same time. Moving the alignment check
#    to 1-hour bars fixed the trend-filter side of this.
# 2. That alone STILL produced zero trades. A diagnostic
#    (scripts/diagnose_signal.py) found the real bottleneck: RSI and the
#    EMA20 cross-above almost never land on the same bar — RSI recovers
#    to 50-65 well before price actually crosses back above its own
#    lagging 20-period EMA. Requiring same-bar simultaneity is what
#    produced zero trades, not the trend filter. Fixed by checking
#    whether RSI touched <30 at any point in the last
#    CRYPTO_RSI_LOOKBACK_BARS bars (default 12 = 1 hour), rather than on
#    the exact same bar as the cross.
#
# Deliberately scoped to "does the signal itself have edge" — it does NOT
# simulate the daily/weekly notional caps, max-positions, category caps, or
# RSI-weighted cross-pair allocation. Those affect how much capital gets
# deployed, not whether an individual trade wins or loses, so results here
# are reported as per-trade % return, not dollar P&L. See "Scope" in the
# output for exactly what is and isn't modeled.

import os
import sys
import json
import bisect
import importlib.util
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

_scripts_dir = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location("crypto_scalper_research_backtest", os.path.join(_scripts_dir, "research.py"))
research = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(research)

WATCHLIST_PATH = os.path.join(_scripts_dir, "..", "watchlist.json")
LIMIT_SLIPPAGE_BUFFER = float(os.getenv("CRYPTO_LIMIT_SLIPPAGE_BUFFER", "0.001"))
WARMUP_BARS = 55  # matches get_signal's own "need at least 55 bars" floor
EMA_WINDOW_BARS = 150  # rolling window fed into compute_* at each step, not full history — see "Approximation" note below


def _load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)["pairs"]


def fetch_historical_bars(pair, timeframe="5Min", lookback_days=60, end_days_ago=0):
    """Paginated historical fetch. Unlike research.get_crypto_bars (built for
    "give me the most recent N bars" live polling, single request), a 60+ day
    5-minute history exceeds Alpaca's per-request bar limit, so this follows
    next_page_token until the full range is retrieved.

    end_days_ago shifts the whole window backward — e.g. lookback_days=60,
    end_days_ago=60 fetches days 60-120 ago, a window that doesn't overlap
    at all with the default (most recent 60 days). Used for out-of-sample
    validation: testing a config on a period that had no part in tuning it."""
    end = datetime.now(timezone.utc) - timedelta(days=end_days_ago)
    start = end - timedelta(days=lookback_days)
    url = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
    all_bars = []
    page_token = None
    while True:
        params = {
            "symbols": pair,
            "timeframe": timeframe,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
        }
        if page_token:
            params["page_token"] = page_token
        response = requests.get(url, headers=research._headers(), params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        all_bars.extend((data.get("bars") or {}).get(pair) or [])
        page_token = data.get("next_page_token")
        if not page_token:
            break
    all_bars.sort(key=lambda b: b.get("t", ""))
    return all_bars


def _bar_time(bar):
    return datetime.strptime(bar["t"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def build_1h_alignment_lookup(hour_bars):
    """Mirrors research.get_1h_trend_alignment(), replayed historically. For
    each 1-hour bar with enough history behind it (index >= 49, needed for
    EMA50), its EMA20/50 only become "known" at bar_start + 1 hour — the
    bar hasn't closed before then. Returns a list of (available_from, ema20,
    ema50) sorted by available_from, for bisecting against a 5-minute bar's
    own timestamp — this is what prevents the backtest from using an hourly
    candle's close before it would actually have existed live."""
    closes = [b["c"] for b in hour_bars]
    ema20_series = research.compute_ema_series(closes, 20)
    ema50_series = research.compute_ema_series(closes, 50)
    lookup = []
    for bar_idx in range(49, len(hour_bars)):
        ema20 = ema20_series[bar_idx - 19]
        ema50 = ema50_series[bar_idx - 49]
        available_from = _bar_time(hour_bars[bar_idx]) + timedelta(hours=1)
        lookup.append((available_from, ema20, ema50))
    return lookup


def lookup_1h_alignment(lookup, bar_time):
    """Latest (ema20, ema50) whose available_from <= bar_time, or None if
    no 1-hour bar had closed yet at that point in history."""
    if not lookup:
        return None
    times = [entry[0] for entry in lookup]
    idx = bisect.bisect_right(times, bar_time) - 1
    if idx < 0:
        return None
    return lookup[idx][1], lookup[idx][2]


def simulate_pair(pair, bars, hour_bars):
    """Replay the exact live signal/exit logic bar-by-bar. Returns a list of
    completed trades (dicts with entry/exit price, pnl_pct, exit_reason,
    bars_held). `hour_bars` backs the 1-hour EMA20/50 trend-alignment
    check — see build_1h_alignment_lookup()."""
    closes = [b["c"] for b in bars]
    alignment_lookup = build_1h_alignment_lookup(hour_bars)
    max_hold_bars = int(research.MAX_HOLD_HOURS * 60 / 5)
    trades = []
    position = None

    for i in range(len(bars)):
        bar = bars[i]

        if position:
            bars_held = i - position["entry_idx"]
            hit_tp = bar["h"] >= position["tp_price"]
            hit_sl = bar["l"] <= position["sl_price"]
            timed_out = bars_held >= max_hold_bars

            if hit_sl:
                # Both-touched-in-one-bar is unresolvable from OHLC alone —
                # conservatively assume the worse outcome (stop) hit first,
                # standard backtesting practice to avoid overstating results.
                exit_price, exit_reason = position["sl_price"], "stop_loss"
            elif hit_tp:
                # Take-profit exits as a limit order in live trading too —
                # same slippage buffer applied here for consistency.
                exit_price = position["tp_price"] * (1 - LIMIT_SLIPPAGE_BUFFER)
                exit_reason = "take_profit"
            elif timed_out:
                exit_price, exit_reason = bar["c"], "timeout"
            else:
                continue

            pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
            trades.append({
                "pair": pair,
                "entry_time": bars[position["entry_idx"]]["t"],
                "exit_time": bar["t"],
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "pnl_pct": round(pnl_pct, 4),
                "exit_reason": exit_reason,
                "bars_held": bars_held,
            })
            position = None
            continue

        if i < WARMUP_BARS:
            continue

        window_bars = bars[max(0, i + 1 - EMA_WINDOW_BARS):i + 1]
        window_closes = [b["c"] for b in window_bars]

        rsi_series = research.compute_rsi_series(window_closes, 14)
        ema20_series = research.compute_ema_series(window_closes, 20)
        atr_series = research.compute_atr_series(window_bars, 14)
        if not rsi_series or len(ema20_series) < 2 or len(atr_series) < 20:
            continue

        alignment = lookup_1h_alignment(alignment_lookup, _bar_time(bar))
        if alignment is None:
            continue  # no 1-hour bar had closed yet at this point in history
        ema20_1h, ema50_1h = alignment

        rsi_lookback_window = rsi_series[-research.RSI_LOOKBACK_BARS:]
        rsi_recently_oversold = min(rsi_lookback_window) < 30

        prev_close, curr_close = closes[i - 1], closes[i]
        crossed_above = prev_close <= ema20_series[-2] and curr_close > ema20_series[-1]
        ema_alignment_bullish = ema20_1h > ema50_1h
        atr_avg20 = sum(atr_series[-20:]) / 20
        atr_volatility_spike = atr_avg20 > 0 and atr_series[-1] > research.ATR_SPIKE_MULTIPLE * atr_avg20

        if rsi_recently_oversold and crossed_above and ema_alignment_bullish and not atr_volatility_spike:
            entry_price = curr_close * (1 + LIMIT_SLIPPAGE_BUFFER)
            position = {
                "entry_idx": i,
                "entry_price": entry_price,
                "tp_price": entry_price * (1 + research.PROFIT_TARGET_PCT / 100),
                "sl_price": entry_price * (1 - research.STOP_LOSS_PCT / 100),
            }

    return trades


def run_backtest(lookback_days=60, exclude_pairs=None, end_days_ago=0):
    """exclude_pairs: optional list of pairs to skip — for testing "what if
    we dropped this pair" without touching the live watchlist.json.
    end_days_ago: shift the whole window backward — see
    fetch_historical_bars()'s docstring. Used for out-of-sample validation."""
    pairs = [p for p in _load_watchlist() if p not in (exclude_pairs or [])]
    all_trades = []
    per_pair_bar_counts = {}

    for pair in pairs:
        bars = fetch_historical_bars(pair, lookback_days=lookback_days, end_days_ago=end_days_ago)
        per_pair_bar_counts[pair] = len(bars)
        if len(bars) < WARMUP_BARS + 20:
            continue
        # +5 days padding so the 1-hour EMA(50) has enough history behind
        # the very first 5-minute bar being tested, not just from lookback start.
        hour_bars = fetch_historical_bars(pair, timeframe="1Hour", lookback_days=lookback_days + 5, end_days_ago=end_days_ago)
        if len(hour_bars) < 50:
            continue
        all_trades.extend(simulate_pair(pair, bars, hour_bars))

    wins = [t for t in all_trades if t["pnl_pct"] > 0]
    win_rate = round(len(wins) / len(all_trades) * 100, 1) if all_trades else None
    avg_return_pct = round(sum(t["pnl_pct"] for t in all_trades) / len(all_trades), 4) if all_trades else None

    compounded = 1.0
    for t in all_trades:
        compounded *= (1 + t["pnl_pct"] / 100)
    cumulative_compounded_pct = round((compounded - 1) * 100, 2)

    # Simple running-peak drawdown on the compounded equity curve (in trade
    # sequence order across all pairs combined, not calendar time — a
    # documented approximation, not a true multi-asset equity curve).
    equity_curve = [1.0]
    for t in all_trades:
        equity_curve.append(equity_curve[-1] * (1 + t["pnl_pct"] / 100))
    peak = equity_curve[0]
    max_drawdown_pct = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_drawdown_pct = min(max_drawdown_pct, (e - peak) / peak * 100)

    exit_reason_counts = {}
    for t in all_trades:
        exit_reason_counts[t["exit_reason"]] = exit_reason_counts.get(t["exit_reason"], 0) + 1

    # Breaks out avg return BY exit reason — a stop-loss's return is fixed
    # by definition (-STOP_LOSS_PCT, minus the market-order assumption), so
    # this is really asking "how much is the timeout bucket dragging the
    # average down" vs "is take-profit pulling its weight".
    avg_return_by_exit_reason = {}
    for reason in exit_reason_counts:
        reason_trades = [t for t in all_trades if t["exit_reason"] == reason]
        avg_return_by_exit_reason[reason] = {
            "count": len(reason_trades),
            "avg_return_pct": round(sum(t["pnl_pct"] for t in reason_trades) / len(reason_trades), 4),
        }

    per_pair = {}
    for pair in pairs:
        pair_trades = [t for t in all_trades if t["pair"] == pair]
        pair_exit_breakdown = {}
        for reason in set(t["exit_reason"] for t in pair_trades):
            reason_trades = [t for t in pair_trades if t["exit_reason"] == reason]
            pair_exit_breakdown[reason] = {
                "count": len(reason_trades),
                "avg_return_pct": round(sum(t["pnl_pct"] for t in reason_trades) / len(reason_trades), 4),
            }
        per_pair[pair] = {
            "trades": len(pair_trades),
            "win_rate_pct": round(len([t for t in pair_trades if t["pnl_pct"] > 0]) / len(pair_trades) * 100, 1) if pair_trades else None,
            "avg_return_pct": round(sum(t["pnl_pct"] for t in pair_trades) / len(pair_trades), 4) if pair_trades else None,
            "exit_breakdown": pair_exit_breakdown,
        }

    # BTC buy-and-hold benchmark over the same historical window
    btc_bars = fetch_historical_bars("BTC/USD", timeframe="1Day", lookback_days=lookback_days, end_days_ago=end_days_ago)
    btc_buy_and_hold_pct = round((btc_bars[-1]["c"] - btc_bars[0]["c"]) / btc_bars[0]["c"] * 100, 2) if len(btc_bars) >= 2 else None

    return {
        "lookback_days": lookback_days,
        "end_days_ago": end_days_ago,
        "window_description": f"{lookback_days + end_days_ago} to {end_days_ago} days ago" if end_days_ago else f"most recent {lookback_days} days",
        "pairs_tested": pairs,
        "bars_fetched_per_pair": per_pair_bar_counts,
        "total_trades": len(all_trades),
        "win_rate_pct": win_rate,
        "avg_return_pct_per_trade": avg_return_pct,
        "cumulative_return_pct_compounded": cumulative_compounded_pct,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "exit_reason_counts": exit_reason_counts,
        "avg_return_by_exit_reason": avg_return_by_exit_reason,
        "per_pair": per_pair,
        "btc_buy_and_hold_pct_same_window": btc_buy_and_hold_pct,
        "scope_note": (
            "Signal-only backtest: replays the exact RSI(14)+EMA20-cross (5-min) "
            "+ EMA20>EMA50 (1-hour) + ATR volatility filter and TP/SL/timeout "
            "exits bar-by-bar with no look-ahead — 1-hour EMAs are only used "
            "from the point they had actually closed. Does NOT simulate "
            "daily/weekly notional caps, max-positions, category caps, or "
            "RSI-weighted cross-pair allocation — those affect how much capital "
            "gets deployed, not whether an individual trade wins or loses. "
            "5-min RSI/EMA/ATR computed from a rolling last-150-bar window per "
            "step, not the full history since account inception (a standard, "
            "negligible-error approximation given EMA's exponential decay)."
        ),
    }


def detail_pair(pair, lookback_days=60):
    """Full trade list for one pair — for inspecting an outlier's actual
    entries/exits, not just its aggregate stats."""
    bars = fetch_historical_bars(pair, lookback_days=lookback_days)
    hour_bars = fetch_historical_bars(pair, timeframe="1Hour", lookback_days=lookback_days + 5)
    trades = simulate_pair(pair, bars, hour_bars)
    trades_sorted = sorted(trades, key=lambda t: t["pnl_pct"])
    return {
        "pair": pair,
        "lookback_days": lookback_days,
        "total_trades": len(trades),
        "worst_10": trades_sorted[:10],
        "best_10": trades_sorted[-10:],
        "all_trades": trades,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "detail":
        pair = sys.argv[2] if len(sys.argv) > 2 else "BTC/USD"
        lookback = int(sys.argv[3]) if len(sys.argv) > 3 else 60
        print(json.dumps(detail_pair(pair, lookback), indent=2))
    else:
        lookback = int(sys.argv[1]) if len(sys.argv) > 1 else 60
        exclude = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else None
        end_days_ago = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else 0
        result = run_backtest(lookback, exclude_pairs=exclude, end_days_ago=end_days_ago)
        print(json.dumps(result, indent=2))
