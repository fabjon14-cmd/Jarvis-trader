"""Backtest the Python gold engine on recent data (or a CSV) and print the results.

  python3 gold-signals/backtest.py                 # fetch from the live data feed
  python3 gold-signals/backtest.py data.csv        # CSV with time,open,high,low,close[,volume]
  python3 gold-signals/backtest.py --compare       # also run without the H4-zone / 30m-agree rules

Results are in R (1R = the stop distance), after partial take-profit, lock and break-even.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
import data  # noqa: E402
from engine import Engine  # noqa: E402


def report(name, eng, events):
    s = eng.stats
    closed = [e for e in events if e.kind == "CLOSED"]
    pf = s["grossW"] / s["grossL"] if s["grossL"] > 0 else float("inf")
    win = 100.0 * s["wins"] / s["trades"] if s["trades"] else 0.0
    buys = sum(e.info["r"] for e in closed if e.trade.dir > 0)
    sells = sum(e.info["r"] for e in closed if e.trade.dir < 0)
    print(f"\n== {name} ==")
    print(f"trades {s['trades']} · win {win:.1f}% · net {s['netR']:+.2f}R · profit factor {pf:.2f} · buys {buys:+.2f}R · sells {sells:+.2f}R")
    by = {}
    for e in closed:
        g = e.trade.grade + (" BUY" if e.trade.dir > 0 else " SELL")
        by[g] = by.get(g, 0.0) + e.info["r"]
    print("by grade:", ", ".join(f"{k} {v:+.2f}R" for k, v in sorted(by.items())))
    return closed


def main():
    if "--email" in sys.argv:
        import contextlib, io
        import emailer
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _main()
        print(buf.getvalue())
        emailer.send("GOLD · BACKTEST RESULTS", buf.getvalue())
    else:
        _main()


def _main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        df = pd.read_csv(args[0], index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        eur = xag = None
        src = args[0]
    else:
        df, eur, xag, src = data.get_data()
    print(f"{src}: {len(df)} candles, {df.index[0]} -> {df.index[-1]}")

    eng = Engine(df, eur, xag)
    ev = eng.run()
    closed = report("Email-bot rules (H4 zone + 30m agree)", eng, ev)
    if "--list" in sys.argv:
        for e in closed:
            t = e.trade
            print(f"  {t.time:%Y-%m-%d %H:%M} {'BUY ' if t.dir > 0 else 'SELL'} {t.grade} {t.entry:.2f} -> {e.info['result']:<11} {e.info['r']:+.2f}R")

    if "--compare" in sys.argv:
        C.REQUIRE_HTF_ZONE = False
        C.REQUIRE_30M_AGREE = False
        e2 = Engine(df, eur, xag)
        report("TradingView rules (no H4-zone / 30m-agree filter)", e2, e2.run())
        C.REQUIRE_HTF_ZONE = True
        e3 = Engine(df, eur, xag)
        report("H4 zone only (30m agree off)", e3, e3.run())


if __name__ == "__main__":
    main()
