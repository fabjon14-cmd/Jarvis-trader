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
    if ran_away({"dir": d, "zl": zl, "zh": zh, "atr": float(a)}, float(eng.c[i])):
        return None                                      # the candle already ran far past the zone
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


def trigger(b5: pd.DataFrame, k: int, w: dict, tf: str = "5m"):
    """Check closed 5m candle k against watch w. Returns a trade dict or None."""
    if k < 1:
        return None
    o, h, l, c = (float(b5[x].iloc[k]) for x in ("open", "high", "low", "close"))
    return trigger_bar(o, h, l, c, float(b5["close"].iloc[k - 1]), b5.index[k], w, tf)


def trigger_bar(o, h, l, c, pc, t, w: dict, tf: str):
    """Check one closed candle (o/h/l/c, previous close pc, open time t) against watch w."""
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
    if ok and tf == "1m" and C.WICK_ONLY_1M and (lw if d > 0 else uw) < 0.4 * rng:
        ok = False                                   # 1m: only a real rejection wick, not just a lower/higher close
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
    wick = (lw if d > 0 else uw) / rng
    return {
        "id": f"{tf}{'B' if d > 0 else 'S'}{t.strftime('%Y%m%d%H%M')}", "tf": tf,
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


def already_moved(t: dict, bars: pd.DataFrame, after) -> str:
    """'' if price has not yet reached the stop or TP1 since the trigger candle, else what it hit."""
    later = bars[bars.index > after]
    for _, b in later.iterrows():
        if (b["low"] <= t["sl0"]) if t["dir"] > 0 else (b["high"] >= t["sl0"]):
            return "stop"
        if (b["high"] >= t["tp1"]) if t["dir"] > 0 else (b["low"] <= t["tp1"]):
            return "TP1"
    return ""


def broken(w: dict, close30: float) -> bool:
    """A 30m close through the far side of the zone ends the watch."""
    return close30 < w["zl"] if w["dir"] > 0 else close30 > w["zh"]


# ---------------------------------------------------------------- live zone arming + 1m state machine
def arm(eng: Engine, i: int):
    """At a 30m close: every H4 zone near price that could start a WATCH before the next 30m close."""
    if eng.trade is not None:
        return None
    d = eng.trend_at(i)
    a = eng.atr_at(i)
    if d == 0 or np.isnan(a) or a <= 0:
        return None
    pad = C.CLUSTER_DIST * 0.15
    Z = eng.zones(i)
    c = float(eng.c[i])
    cands = []
    for z in Z:
        if not z.htf or z.s < C.MIN_LEVEL_STR:
            continue
        zl, zh = z.lo - pad, z.hi + pad
        if min(abs(c - zl), abs(c - zh)) > C.ARM_RANGE_ATR * a:
            continue
        side = 1 if c > zh else -1 if c < zl else eng.role_from(zl, zh, i, 1)
        cands.append({"zl": zl, "zh": zh, "s": z.s, "names": z.names, "side": side})
    if not cands:
        return None
    start = eng.t[i] + pd.Timedelta(minutes=30)
    return {"dir": d, "atr": float(a), "cands": cands, "zones": [{"lo": q.lo, "hi": q.hi, "s": q.s} for q in Z],
            "from": start.isoformat(), "until": (start + pd.Timedelta(minutes=C.ARM_MINUTES)).isoformat(),
            "context": eng.context(i)}


def ran_away(w: dict, c: float) -> bool:
    """Price has left the zone in the trade direction by WATCH_DROP_ATR x ATR: the entry is gone."""
    x = C.WATCH_DROP_ATR * w["atr"]
    return c > w["zh"] + x if w["dir"] > 0 else c < w["zl"] - x


def _recent_zone(S, mid, t):
    S["watched"] = [x for x in S.get("watched", []) if t - pd.Timestamp(x[1]) < pd.Timedelta(hours=C.REWATCH_BLOCK_HOURS)]
    return any(abs(m - mid) <= C.CLUSTER_DIST for m, _ in S["watched"])


def on_30m(S: dict, eng: Engine, i: int) -> list:
    """Call once per closed 30m candle (after the engine has stepped it). Returns [(kind, payload)]."""
    out = []
    t_close = eng.t[i] + pd.Timedelta(minutes=30)
    w = S.get("watch")
    if w and (broken(w, float(eng.c[i])) or pd.Timestamp(w["expires"]) <= t_close or eng.trade is not None
              or ran_away(w, float(eng.c[i]))):
        S["watch"] = None
        out.append(("WATCH_END", w))
    pw = S.get("prev_watch")
    if pw and (broken(pw, float(eng.c[i])) or pd.Timestamp(pw["expires"]) <= t_close or ran_away(pw, float(eng.c[i]))):
        S.pop("prev_watch", None)
    S["arm"] = arm(eng, i) if C.LIVE_WATCH else None
    if S.get("watch") is None and S.get("t5") is None:
        cand = candidate(eng, i)
        if cand and not _recent_zone(S, (cand["zl"] + cand["zh"]) / 2, eng.t[i]):
            S.setdefault("watched", []).append([(cand["zl"] + cand["zh"]) / 2, eng.t[i].isoformat()])
            S["watch"] = cand
            out.append(("WATCH", cand))
    return out


def on_bar(S: dict, t, bar, pc: float, bar5=None, pc5=None) -> list:
    """Advance the helper by one closed 1m candle (open time t). bar5/pc5: the 5m candle that closes
    with this minute, if any. Returns [(kind, payload)] - WATCH, ENTRY, LOCK/TP1/CLOSED, WATCH_END."""
    out = []
    o, h, l, c = (float(bar[x]) for x in ("open", "high", "low", "close"))
    tr = S.get("t5")
    if tr:
        if tr.get("tf", "5m") == "1m":
            ev = step(tr, bar)
        elif bar5 is not None:
            ev = step(tr, bar5)
        else:
            ev = []
        out += [(k, (tr, info)) for k, info in ev]
        if tr.get("closed"):
            S["t5"] = None
            pw = S.pop("prev_watch", None)
            win = any(k == "CLOSED" and info["r"] > 0 for k, info in ev)
            if pw and C.REWATCH != "off" and (C.REWATCH == "any" or win) and t < pd.Timestamp(pw["expires"]):
                pw["retest"] = pw.get("retest", 1) + 1
                S["watch"] = pw                       # same zone, same window: a second retest can trigger again
                out.append(("REWATCH", pw))
        return out

    A = S.get("arm")
    w = S.get("watch")
    if w is None and A and pd.Timestamp(A["from"]) <= t < pd.Timestamp(A["until"]):
        d = A["dir"]
        for z in A["cands"]:
            zl, zh = z["zl"], z["zh"]
            if z["side"] == d:
                touched = (l <= zh and c >= zl) if d > 0 else (h >= zl and c <= zh)
                if touched and not _recent_zone(S, (zl + zh) / 2, t):
                    start = t.floor("5min")
                    w = {"dir": d, "zl": zl, "zh": zh, "strength": z["s"], "names": z["names"], "atr": A["atr"],
                         "zones": A["zones"], "start": start.isoformat(),
                         "expires": (start + pd.Timedelta(hours=C.WATCH_HOURS)).isoformat(),
                         "context": A["context"], "via": "1m", "touch": t.isoformat()}
                    S.setdefault("watched", []).append([(zl + zh) / 2, t.isoformat()])
                    S["watch"] = w
                    out.append(("WATCH", w))
                    break
    if A:
        for z in A["cands"]:
            if c > z["zh"]:
                z["side"] = 1
            elif c < z["zl"]:
                z["side"] = -1
    if w is None:
        return out

    if t >= pd.Timestamp(w["expires"]):
        S["watch"] = None
        return out + [("WATCH_END", w)]
    if ran_away(w, c):
        S["watch"] = None
        return out + [("WATCH_END", w)]
    if t < pd.Timestamp(w["start"]):
        return out
    hit = None
    if C.HELPER_1M:
        hit = trigger_bar(o, h, l, c, pc, t, w, "1m")
    if hit is None and bar5 is not None and pd.Timestamp(bar5.name) >= pd.Timestamp(w["start"]):
        hit = trigger_bar(*(float(bar5[x]) for x in ("open", "high", "low", "close")), pc5, bar5.name, w, "5m")
    if hit:
        S["watch"] = None
        S["prev_watch"] = w
        S["t5"] = hit
        out.append(("ENTRY", (hit, w)))
    return out


def to_5m(b1: pd.DataFrame) -> pd.DataFrame:
    return b1.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def feed(S: dict, b1: pd.DataFrame, after=None, until=None):
    """Run on_bar over closed 1m candles with after < t (< until). Yields (t, events)."""
    b5 = to_5m(b1)
    pos5 = {t: k for k, t in enumerate(b5.index)}
    idx = b1.index
    for k in range(1, len(b1)):
        t = idx[k]
        if after is not None and t <= after:
            continue
        if until is not None and t >= until:
            break
        bar5 = pc5 = None
        if (t + pd.Timedelta(minutes=1)).minute % 5 == 0:
            s5 = (t - pd.Timedelta(minutes=4))
            k5 = pos5.get(s5)
            if k5 is not None and k5 > 0:
                bar5, pc5 = b5.iloc[k5], float(b5["close"].iloc[k5 - 1])
        yield t, on_bar(S, t, b1.iloc[k], float(b1["close"].iloc[k - 1]), bar5, pc5)
