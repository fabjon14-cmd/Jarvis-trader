# crypto-scalper/scripts/diagnose_signal.py
#
# One-off diagnostic: counts how often EACH individual buy-trigger condition
# is true across historical bars, separately, so we can see which specific
# condition is the bottleneck instead of just knowing the AND of all four
# never fires. Not part of the live trading pipeline.

import sys
import json
import importlib.util
from datetime import timezone

_spec = importlib.util.spec_from_file_location("crypto_scalper_backtest_diag", __file__.replace("diagnose_signal.py", "backtest.py"))
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

research = bt.research


def diagnose(pair, lookback_days=60):
    bars = bt.fetch_historical_bars(pair, lookback_days=lookback_days)
    hour_bars = bt.fetch_historical_bars(pair, timeframe="1Hour", lookback_days=lookback_days + 5)
    alignment_lookup = bt.build_1h_alignment_lookup(hour_bars)
    closes = [b["c"] for b in bars]

    counts = {
        "bars_evaluated": 0,
        "rsi_oversold": 0,
        "crossed_above_ema20": 0,
        "rsi_oversold_and_crossed": 0,
        "ema_alignment_bullish_1h": 0,
        "no_atr_spike": 0,
        "all_four": 0,
    }
    rsi_min, rsi_at_cross = 100.0, []

    for i in range(len(bars)):
        if i < bt.WARMUP_BARS:
            continue
        window_bars = bars[max(0, i + 1 - bt.EMA_WINDOW_BARS):i + 1]
        window_closes = [b["c"] for b in window_bars]

        rsi = research.compute_rsi(window_closes, 14)
        ema20_series = research.compute_ema_series(window_closes, 20)
        atr_series = research.compute_atr_series(window_bars, 14)
        if rsi is None or len(ema20_series) < 2 or len(atr_series) < 20:
            continue

        alignment = bt.lookup_1h_alignment(alignment_lookup, bt._bar_time(bars[i]))
        if alignment is None:
            continue
        ema20_1h, ema50_1h = alignment

        counts["bars_evaluated"] += 1
        rsi_min = min(rsi_min, rsi)

        prev_close, curr_close = closes[i - 1], closes[i]
        crossed_above = prev_close <= ema20_series[-2] and curr_close > ema20_series[-1]
        rsi_oversold = rsi < 30
        ema_alignment_bullish = ema20_1h > ema50_1h
        atr_avg20 = sum(atr_series[-20:]) / 20
        no_atr_spike = not (atr_avg20 > 0 and atr_series[-1] > research.ATR_SPIKE_MULTIPLE * atr_avg20)

        if rsi_oversold:
            counts["rsi_oversold"] += 1
        if crossed_above:
            counts["crossed_above_ema20"] += 1
        if rsi_oversold and crossed_above:
            counts["rsi_oversold_and_crossed"] += 1
        if crossed_above:
            rsi_at_cross.append(round(rsi, 1))
        if ema_alignment_bullish:
            counts["ema_alignment_bullish_1h"] += 1
        if no_atr_spike:
            counts["no_atr_spike"] += 1
        if rsi_oversold and crossed_above and ema_alignment_bullish and no_atr_spike:
            counts["all_four"] += 1

    counts["rsi_min_seen"] = round(rsi_min, 2)
    counts["rsi_at_cross_events_sample"] = rsi_at_cross[:20]
    return counts


if __name__ == "__main__":
    pair = sys.argv[1] if len(sys.argv) > 1 else "BTC/USD"
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    print(json.dumps(diagnose(pair, lookback), indent=2))
