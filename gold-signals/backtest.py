"""Backtest the Python gold engine on recent data (or a CSV) and print the results.

  python3 gold-signals/backtest.py                 # fetch from the live data feed
  python3 gold-signals/backtest.py data.csv        # CSV with time,open,high,low,close[,volume]
  python3 gold-signals/backtest.py --compare       # also run without the H4-zone / 30m-agree rules
  python3 gold-signals/backtest.py --helper5m      # also test the 5m entry helper (needs 5m data: live feed or --5m=file.csv)

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


def helper_backtest(df, b5, eur, xag):
    """30m engine bar by bar; 5m WATCH -> 5m ENTRY -> managed on 5m candles (one 5m trade at a time)."""
    import helper5m as H
    eng = Engine(df, eur, xag)
    t5_index = b5.index
    busy_until = pd.Timestamp.min.tz_localize("UTC")
    trades = []
    for i in range(eng.n):
        for lv in eng.pivot_feed.get(i, []):
            eng.raw.append(lv)
            if len(eng.raw) > 400:
                eng.raw.pop(0)
        eng.step(i)
        start = eng.t[i] + pd.Timedelta(minutes=30)
        if start < t5_index[0] or start <= busy_until:
            continue
        w = H.candidate(eng, i)
        if not w:
            continue
        exp = pd.Timestamp(w["expires"])
        ks = [k for k in range((t5_index >= start).argmax(), len(b5)) if t5_index[k] < exp]
        tr = None
        for k in ks:
            if b5["close"].iloc[k] < w["zl"] and w["dir"] > 0 or b5["close"].iloc[k] > w["zh"] and w["dir"] < 0:
                break
            tr = H.trigger(b5, k, w)
            if tr:
                break
        if not tr:
            busy_until = exp if not ks else t5_index[ks[-1]]
            continue
        j = k + 1
        while j < len(b5) and not tr.get("closed"):
            for kind, info in H.step(tr, b5.iloc[j]):
                if kind == "CLOSED":
                    tr["r"], tr["result"] = info["r"], info["result"]
            j += 1
        busy_until = t5_index[min(j, len(b5) - 1)]
        if tr.get("closed"):
            trades.append(tr)
    ev30 = [e for e in eng.events if e.kind == "CLOSED" and e.trade.time >= t5_index[0]]
    def line(name, rs):
        w = sum(1 for r in rs if r > 0)
        gw = sum(r for r in rs if r > 0)
        gl = -sum(r for r in rs if r <= 0)
        pf = gw / gl if gl > 0 else float("inf")
        print(f"{name}: trades {len(rs)} · win {100 * w / len(rs) if rs else 0:.1f}% · net {sum(rs):+.2f}R · profit factor {pf:.2f}")
    print(f"\n== 5m entry helper vs 30m signals ({t5_index[0]:%Y-%m-%d} -> {t5_index[-1]:%Y-%m-%d}) ==")
    line("30m signals    ", [e.info["r"] for e in ev30])
    line("5m entry helper", [t["r"] for t in trades])
    print("note: R on the 5m helper is a smaller stop, so 1R there is less money than 1R on the 30m trade")


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

    if "--helper5m" in sys.argv:
        f5 = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--5m=")), None)
        if f5:
            b5 = pd.read_csv(f5, index_col=0, parse_dates=True)
            if b5.index.tz is None:
                b5.index = b5.index.tz_localize("UTC")
        else:
            b5 = data.get_5m(n=5000)
        helper_backtest(df, b5, eur, xag)

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
