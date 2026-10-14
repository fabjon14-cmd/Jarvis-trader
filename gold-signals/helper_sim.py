"""Backtest the 1m / 5m entry helper exactly as the live bot runs it (minute by minute).

python3 gold-signals/helper_sim.py DATA_DIR [--list]
DATA_DIR holds xau_30m.csv, xau_1m.csv (and optionally eur_30m.csv / xag_30m.csv) from the
"Gold Data Snapshot" workflow. Compares the old helper settings with the new ones.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
import helper5m as H  # noqa: E402
from engine import Engine  # noqa: E402


def load(p):
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def sim(df30, b1, eur=None, xag=None, verbose=False):
    eng = Engine(df30, eur, xag)
    S = {"watch": None, "t5": None, "arm": None, "watched": []}
    trades, watches, live_watches = [], 0, 0
    first = b1.index[0] + pd.Timedelta(minutes=10)
    for i in range(eng.n):
        for lv in eng.pivot_feed.get(i, []):
            eng.raw.append(lv)
            if len(eng.raw) > 400:
                eng.raw.pop(0)
        eng.step(i)
        T0 = eng.t[i] + pd.Timedelta(minutes=30)
        if T0 < first or T0 > b1.index[-1]:
            continue
        for k, x in H.on_30m(S, eng, i):
            watches += k == "WATCH"
        win = b1.loc[T0 - pd.Timedelta(minutes=6): T0 + pd.Timedelta(minutes=30)]
        for t, evs in H.feed(S, win, after=T0 - pd.Timedelta(minutes=1), until=T0 + pd.Timedelta(minutes=30)):
            for k, x in evs:
                if k == "WATCH":
                    watches += 1
                    live_watches += 1
                elif k == "ENTRY" and verbose:
                    hit = x[0]
                    print(f"  {hit['time'][:16]} {hit['tf']} {'BUY ' if hit['dir'] > 0 else 'SELL'} {hit['entry']:.2f} "
                          f"SL {hit['sl0']:.2f} TP1 {hit['tp1']:.2f} TP2 {hit['tp2']:.2f} zone {hit['zl']:.1f}-{hit['zh']:.1f}")
                elif k == "CLOSED":
                    tr, info = x
                    trades.append((tr, info["r"], info["result"]))
                    if verbose:
                        print(f"      -> {info['result']} {info['r']:+.2f}R at {t:%m-%d %H:%M}")
    return trades, watches, live_watches


def line(name, trades, watches, live):
    rs = [r for _, r, _ in trades]
    w = sum(1 for r in rs if r > 0)
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    n1 = sum(1 for t, _, _ in trades if t["tf"] == "1m")
    print(f"{name:<34} watches {watches:>3} (live {live:>3}) · trades {len(rs):>3} (1m {n1:>3}) · "
          f"win {100 * w / len(rs) if rs else 0:5.1f}% · net {sum(rs):+6.2f}R · PF {pf:.2f}")


VARIANTS = {
    "old (30m watch only, stop 0.20)": dict(LIVE_WATCH=False, WATCH_DROP_ATR=1e9, MIN_RISK5_ATR=0.20),
    "+ min stop 0.35": dict(LIVE_WATCH=False, WATCH_DROP_ATR=1e9, MIN_RISK5_ATR=0.35),
    "+ drop run-away watch": dict(LIVE_WATCH=False, WATCH_DROP_ATR=1.5, MIN_RISK5_ATR=0.35),
    "+ live 1m zone watch (new)": dict(LIVE_WATCH=True, WATCH_DROP_ATR=1.5, MIN_RISK5_ATR=0.35),
}


def main():
    d = Path(sys.argv[1])
    df30, b1 = load(d / "xau_30m.csv"), load(d / "xau_1m.csv")
    eur = load(d / "eur_30m.csv")["close"] if (d / "eur_30m.csv").exists() else None
    xag = load(d / "xag_30m.csv")["close"] if (d / "xag_30m.csv").exists() else None
    b1 = b1[["open", "high", "low", "close"]]
    print(f"1m data {b1.index[0]:%Y-%m-%d %H:%M} -> {b1.index[-1]:%Y-%m-%d %H:%M} UTC ({len(b1)} candles)")
    keep = {k: getattr(C, k) for k in ("LIVE_WATCH", "WATCH_DROP_ATR", "MIN_RISK5_ATR")}
    for name, v in VARIANTS.items():
        for k, x in v.items():
            setattr(C, k, x)
        line(name, *sim(df30, b1, eur, xag, verbose="--list" in sys.argv and name.endswith("(new)")))
    for k, x in keep.items():
        setattr(C, k, x)
    print("R on the helper uses its own (smaller) stop.")


if __name__ == "__main__":
    main()
