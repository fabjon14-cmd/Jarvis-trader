# daytrader/scripts/test_order.py
#
# One-off manual verification tool — places a single small bracket order
# to confirm Alpaca actually accepts equity bracket orders on this
# account (never separately tested before) and that it round-trips
# through the journal correctly. Deliberately bypasses get_signal()'s
# buy_signal check (EMA cross / RSI / ATR-above-average / entry-window)
# entirely — this is NOT a strategy trade, just a pipeline check, so it
# must only ever be run manually (workflow_dispatch), never scheduled.
# Once placed, the normal agent picks up and manages the resulting
# position (crossunder exit / forced EOD flatten) exactly like any other.

import sys
import os
import json

sys.path.insert(0, os.path.dirname(__file__))

import research
import trade


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "DIS"
    qty = sys.argv[2] if len(sys.argv) > 2 else "1"

    signal = research.get_signal(symbol)
    if signal.get("error"):
        print(json.dumps({"error": signal["error"]}))
        return 1

    limit_price = round(signal["last_price"] * 1.001, 2)
    result = trade.place_bracket_order(symbol, qty, limit_price, signal["stop_price"], signal["tp_price"])
    print(json.dumps({
        "symbol": symbol,
        "qty": qty,
        "limit_price": limit_price,
        "stop_price": signal["stop_price"],
        "tp_price": signal["tp_price"],
        "atr14": signal["atr14"],
        "result": result,
    }, indent=2))
    return 0 if result.get("placed") else 1


if __name__ == "__main__":
    sys.exit(main())
