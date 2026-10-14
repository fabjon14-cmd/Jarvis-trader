"""Save recent XAU/USD 1m / 5m / 30m candles (+ EUR/USD, XAG/USD 30m closes) to data/ for backtests."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import data  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
out.mkdir(parents=True, exist_ok=True)
key = os.environ["TWELVEDATA_API_KEY"]
for name, sym, n, iv in (("xau_1m", "XAU/USD", 5000, "1min"), ("xau_5m", "XAU/USD", 5000, "5min"),
                         ("xau_30m", "XAU/USD", 5000, "30min"), ("eur_30m", "EUR/USD", 5000, "30min"),
                         ("xag_30m", "XAG/USD", 5000, "30min")):
    for attempt in range(4):              # free plan: 8 requests/minute, shared with the live bot
        try:
            df = data._twelve(sym, key, outputsize=n, interval=iv)
            break
        except Exception as exc:
            print(name, "retry after:", exc)
            time.sleep(65)
    else:
        raise SystemExit(f"{name}: gave up")
    time.sleep(15)
    df.to_csv(out / f"{name}.csv")
    print(name, len(df), df.index[0], "->", df.index[-1])
