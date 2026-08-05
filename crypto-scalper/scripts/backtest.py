# crypto-scalper/scripts/backtest.py
#
# Backtests the exact signal logic in research.get_signal() (RSI(14) < 30 +
# EMA20 cross-above + EMA20>EMA50 + no ATR volatility spike) and the exact
# exit logic in research.get_exit_flags() (+1.5% TP / -0.75% SL / 4h
# timeout), replayed bar-by-bar over historical 5-minute candles with no
# look-ahead: at bar i, only bars[0..i] are used to decide anything about
# bar i.
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


def fetch_historical_bars(pair, timeframe="5Min", lookback_days=60):
    """Paginated historical fetch. Unlike research.get_crypto_bars (built for
    "give me the most recent N bars" live polling, single request), a 60+ day
    5-minute history exceeds Alpaca's per-request bar limit, so this follows
    next_page_token until the full range is retrieved."""
    end = datetime.now(timezone.utc)
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


def simulate_pair(pair, bars):
    """Replay the exact live signal/exit logic bar-by-bar. Returns a list of
    completed trades (dicts with entry/exit price, pnl_pct, exit_reason,
    bars_held)."""
    closes = [b["c"] for b in bars]
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

        rsi = research.compute_rsi(window_closes, 14)
        ema20_series = research.compute_ema_series(window_closes, 20)
        ema50_series = research.compute_ema_series(window_closes, 50)
        atr_series = research.compute_atr_series(window_bars, 14)
        if rsi is None or len(ema20_series) < 2 or not ema50_series or len(atr_series) < 20:
            continue

        prev_close, curr_close = closes[i - 1], closes[i]
        crossed_above = prev_close <= ema20_series[-2] and curr_close > ema20_series[-1]
        rsi_oversold = rsi < 30
        ema_alignment_bullish = ema20_series[-1] > ema50_series[-1]
        atr_avg20 = sum(atr_series[-20:]) / 20
        atr_volatility_spike = atr_avg20 > 0 and atr_series[-1] > research.ATR_SPIKE_MULTIPLE * atr_avg20

        if rsi_oversold and crossed_above and ema_alignment_bullish and not atr_volatility_spike:
            entry_price = curr_close * (1 + LIMIT_SLIPPAGE_BUFFER)
            position = {
                "entry_idx": i,
                "entry_price": entry_price,
                "tp_price": entry_price * (1 + research.PROFIT_TARGET_PCT / 100),
                "sl_price": entry_price * (1 - research.STOP_LOSS_PCT / 100),
            }

    return trades


def run_backtest(lookback_days=60):
    pairs = _load_watchlist()
    all_trades = []
    per_pair_bar_counts = {}

    for pair in pairs:
        bars = fetch_historical_bars(pair, lookback_days=lookback_days)
        per_pair_bar_counts[pair] = len(bars)
        if len(bars) < WARMUP_BARS + 20:
            continue
        all_trades.extend(simulate_pair(pair, bars))

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

    per_pair = {}
    for pair in pairs:
        pair_trades = [t for t in all_trades if t["pair"] == pair]
        per_pair[pair] = {
            "trades": len(pair_trades),
            "win_rate_pct": round(len([t for t in pair_trades if t["pnl_pct"] > 0]) / len(pair_trades) * 100, 1) if pair_trades else None,
            "avg_return_pct": round(sum(t["pnl_pct"] for t in pair_trades) / len(pair_trades), 4) if pair_trades else None,
        }

    # BTC buy-and-hold benchmark over the same historical window
    btc_bars = fetch_historical_bars("BTC/USD", timeframe="1Day", lookback_days=lookback_days)
    btc_buy_and_hold_pct = round((btc_bars[-1]["c"] - btc_bars[0]["c"]) / btc_bars[0]["c"] * 100, 2) if len(btc_bars) >= 2 else None

    return {
        "lookback_days": lookback_days,
        "pairs_tested": pairs,
        "bars_fetched_per_pair": per_pair_bar_counts,
        "total_trades": len(all_trades),
        "win_rate_pct": win_rate,
        "avg_return_pct_per_trade": avg_return_pct,
        "cumulative_return_pct_compounded": cumulative_compounded_pct,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "exit_reason_counts": exit_reason_counts,
        "per_pair": per_pair,
        "btc_buy_and_hold_pct_same_window": btc_buy_and_hold_pct,
        "scope_note": (
            "Signal-only backtest: replays the exact RSI/EMA/ATR buy trigger and "
            "TP/SL/timeout exits bar-by-bar with no look-ahead. Does NOT simulate "
            "daily/weekly notional caps, max-positions, category caps, or "
            "RSI-weighted cross-pair allocation — those affect how much capital "
            "gets deployed, not whether an individual trade wins or loses. "
            "EMA/RSI/ATR computed from a rolling last-150-bar window per step, "
            "not the full history since account inception (a standard, "
            "negligible-error approximation given EMA's exponential decay)."
        ),
    }


if __name__ == "__main__":
    lookback = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    result = run_backtest(lookback)
    print(json.dumps(result, indent=2))
