"""5m entry helper.

1. WATCH: on a closed 30m candle that trades into an H4 zone in the H4 trend direction
   (support in an uptrend / resistance in a downtrend), with no trade open, start watching.
2. TRIGGER: while watching (up to WATCH_HOURS), a closed 5m candle inside the zone with a
   rejection wick (>= 40% of the candle) or a reversal close = 5m ENTRY. Stop just past that
   candle's wick (+ buffer), targets from the same zones as the 30m engine.
3. The 5m trade is then managed with the same rules (lock, TP1 half + BE, TP2, SL).

Everything here is plain dicts so it can live in state.json between runs.
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd

import config as C
from engine import Engine


def candidate(eng: Engine, i: int):
    """Watch setup on 30m bar i, or None."""
    if eng.trade is not None:
        return None
    d = eng.trend_at(i)
    a = eng.atr_at(i)
    if d == 0 or np.isnan(a) or a <= 0:
        return None
    pad = C.CLUSTER_DIST * 0.15
    Z = eng.zones(i)
    best = None
    for z in Z:
        if not z.htf or z.s < C.MIN_LEVEL_STR:
            continue
        zl, zh = z.lo - pad, z.hi + pad
        if d > 0:
            touched = eng.l[i] <= zh and eng.c[i] >= zl
        else:
            touched = eng.h[i] >= zl and eng.c[i] <= zh
        if not touched:
            continue
        if eng.role_from(zl, zh, i, 1) != d:
            continue
        dist = abs(eng.c[i] - (zh if d > 0 else zl))
        if best is None or dist < best[0]:
            best = (dist, z, zl, zh)
    if best is None:
        return None
    _, z, zl, zh = best
    opp = [{"lo": q.lo, "hi": q.hi, "s": q.s} for q in Z]
    start = eng.t[i] + pd.Timedelta(minutes=30)          # 30m candle close
    return {
        "dir": d, "zl": zl, "zh": zh, "strength": z.s, "names": z.names,
        "atr": float(a), "zones": opp,
        "start": start.isoformat(),
        "expires": (start + pd.Timedelta(hours=C.WATCH_HOURS)).isoformat(),
        "context": eng.context(i),
    }


def _targets(w, entry, d, risk):
    Z = [SimpleNamespace(**z) for z in w["zones"]]
    return Engine.targets(None, Z, entry, d, risk, w["atr"])


def trigger(b5: pd.DataFrame, k: int, w: dict):
    """Check closed 5m candle k against watch w. Returns a trade dict or None."""
    if k < 1:
        return None
    o, h, l, c = (float(b5[x].iloc[k]) for x in ("open", "high", "low", "close"))
    pc = float(b5["close"].iloc[k - 1])
    rng = h - l
    if rng <= 0:
        return None
    d, zl, zh, a = w["dir"], w["zl"], w["zh"], w["atr"]
    lw, uw = min(o, c) - l, h - max(o, c)
    sMax = C.SWEEP_MAX_ATR * a
    if d > 0:
        ok = l <= zh and l >= zl - sMax and c > zl and (lw >= 0.4 * rng or (c > o and c > pc))
        sl = l - C.SL5_BUF_ATR * a
    else:
        ok = h >= zl and h <= zh + sMax and c < zh and (uw >= 0.4 * rng or (c < o and c < pc))
        sl = h + C.SL5_BUF_ATR * a
    if not ok:
        return None
    risk = (c - sl) * d
    if risk < C.MIN_RISK5_ATR * a:                 # never a hair-trigger stop
        risk = C.MIN_RISK5_ATR * a
        sl = c - d * risk
    if risk > C.MAX_RISK_ATR * a:
        return None
    tp1, tp2 = _targets(w, c, d, risk)
    rr1, rr2 = abs(tp1 - c) / risk, abs(tp2 - c) / risk
    if rr2 < C.MIN_RR:
        return None
    t = b5.index[k]
    wick = (lw if d > 0 else uw) / rng
    return {
        "id": f"5{'B' if d > 0 else 'S'}{t.strftime('%Y%m%d%H%M')}",
        "dir": d, "entry": c, "sl0": sl, "sl": sl, "tp1": tp1, "tp2": tp2, "rr1": rr1, "rr2": rr2,
        "risk": risk, "time": t.isoformat(), "zl": zl, "zh": zh, "half": False, "lock": False,
        "how": "rejection wick" if wick >= 0.4 else "reversal close", "wick": wick,
    }


def step(t: dict, bar) -> list:
    """Advance a 5m trade by one closed 5m candle. Returns [(kind, info)]; sets t['closed']."""
    d = t["dir"]
    hi, lo = float(bar["high"]), float(bar["low"])
    hitSL = lo <= t["sl"] if d > 0 else hi >= t["sl"]
    hitT1 = hi >= t["tp1"] if d > 0 else lo <= t["tp1"]
    hitT2 = hi >= t["tp2"] if d > 0 else lo <= t["tp2"]
    pf = C.PARTIAL_PCT / 100.0
    stopR = d * (t["sl"] - t["entry"]) / abs(t["entry"] - t["sl0"])
    lock_at = min(C.LOCK_AT, C.LOCK_TRIG - 5.0)
    trig = t["entry"] + (t["tp1"] - t["entry"]) * C.LOCK_TRIG / 100.0
    hitLk = C.LOCK_ON and not t["lock"] and (hi >= trig if d > 0 else lo <= trig)
    out = []

    def close(r, how, price):
        t["closed"] = True
        out.append(("CLOSED", {"result": how, "r": r, "price": price}))

    if not t["half"]:
        if hitSL:
            close(stopR, "LOCKED EXIT" if stopR > 0 else "STOPPED", t["sl"])
        elif hitT1:
            t["half"] = True
            t["sl"] = max(t["sl"], t["entry"]) if d > 0 else min(t["sl"], t["entry"])
            out.append(("TP1", {"new_sl": t["sl"]}))
            if hitT2:
                close(pf * t["rr1"] + (1 - pf) * t["rr2"], "TARGET", t["tp2"])
        elif hitLk:
            t["lock"] = True
            t["sl"] = t["entry"] + (t["tp1"] - t["entry"]) * lock_at / 100.0
            out.append(("LOCK", {"new_sl": t["sl"], "locked_r": t["rr1"] * lock_at / 100.0}))
    else:
        if hitSL:
            close(pf * t["rr1"] + (1 - pf) * max(stopR, 0.0), "BE EXIT", t["sl"])
        elif hitT2:
            close(pf * t["rr1"] + (1 - pf) * t["rr2"], "TARGET", t["tp2"])
    return out


def broken(w: dict, close30: float) -> bool:
    """A 30m close through the far side of the zone ends the watch."""
    return close30 < w["zl"] if w["dir"] > 0 else close30 > w["zh"]
