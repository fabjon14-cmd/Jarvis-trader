"""GoldSignalsPro engine - Python port of the TradingView indicator (30m chart, Day mode).

Replays closed 30m candles one by one, exactly like the Pine script does on
history, and emits events (new signal, lock, TP1, exits). Higher timeframes
(1h, 4h, daily, weekly) are built from the 30m candles, using the previous
COMPLETED higher-timeframe bar only, so nothing repaints.

Differences from the TradingView version (documented on purpose):
- no volume profile levels (most XAU/USD feeds have no volume)
- dollar / silver cross-check only when that data is supplied
- 4h / daily candles start at 17:00 New York (the usual gold session roll)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config as C

NY = ZoneInfo("America/New_York")
UK = ZoneInfo("Europe/London")


# ----------------------------------------------------------------------------
# small indicator helpers
# ----------------------------------------------------------------------------
def rma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    a = 1.0 / n
    for i in range(n, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def atr(h, l, c, n=14):
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    tr[0] = h[0] - l[0]
    return rma(tr, n)


def ema(x, n):
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    a = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up = rma(np.maximum(d, 0), n)
    dn = rma(np.maximum(-d, 0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = up / dn
        r = 100 - 100 / (1 + rs)
    r[dn == 0] = 100.0
    return r


def sma(x, n):
    s = pd.Series(x).rolling(n).mean().to_numpy()
    return s


def pivots(h, l, s):
    """Pine-style pivots: index j is a pivot high if high[j] > the s bars before and >= the s bars after."""
    n = len(h)
    ph, pl = [], []
    for j in range(s, n - s):
        left_h, right_h = h[j - s:j], h[j + 1:j + s + 1]
        if h[j] > left_h.max() and h[j] >= right_h.max():
            ph.append(j)
        left_l, right_l = l[j - s:j], l[j + 1:j + s + 1]
        if l[j] < left_l.min() and l[j] <= right_l.min():
            pl.append(j)
    return ph, pl


def structure(h, l, c, s):
    """BOS / CHoCH structure per bar (state AFTER that bar closes).
    Returns st (+1 up / -1 down / 0), evt (1 BOS, 2 CHoCH, 0 none) and the broken level."""
    n = len(c)
    st = np.zeros(n, dtype=int)
    evt = np.zeros(n, dtype=int)
    lvl = np.full(n, np.nan)
    ph_idx, pl_idx = pivots(h, l, s)
    ph_conf = {j + s: h[j] for j in ph_idx}   # pivot known once s bars have closed after it
    pl_conf = {j + s: l[j] for j in pl_idx}
    sHi = sLo = np.nan
    cur = 0
    for i in range(n):
        if i in ph_conf:
            sHi = ph_conf[i]
        if i in pl_conf:
            sLo = pl_conf[i]
        if not np.isnan(sHi) and c[i] > sHi:
            evt[i] = 2 if cur == -1 else 1
            lvl[i] = sHi
            cur = 1
            sHi = np.nan
        elif not np.isnan(sLo) and c[i] < sLo:
            evt[i] = 2 if cur == 1 else 1
            lvl[i] = sLo
            cur = -1
            sLo = np.nan
        st[i] = cur
    return st, evt, lvl


# ----------------------------------------------------------------------------
# higher timeframes from 30m candles
# ----------------------------------------------------------------------------
def bucket_keys(times: pd.DatetimeIndex, tf: str) -> np.ndarray:
    """Bucket start for each 30m open time. Day / 4h / week roll at 17:00 New York."""
    ny = times.tz_convert(NY).tz_localize(None) + pd.Timedelta(hours=7)   # 17:00 NY -> 00:00
    if tf == "1h":
        k = times.tz_convert("UTC").floor("1h").tz_localize(None)
    elif tf == "4h":
        k = ny.floor("4h")
    elif tf == "D":
        k = ny.floor("1D")
    elif tf == "W":
        k = (ny - pd.to_timedelta(ny.dayofweek, unit="D")).floor("1D")
    else:
        raise ValueError(tf)
    return np.asarray(k)


@dataclass
class HTF:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    first: np.ndarray        # index of first 30m bar in each htf bar
    of_bar: np.ndarray       # htf bar index for each 30m bar


def make_htf(df: pd.DataFrame, tf: str) -> HTF:
    keys = bucket_keys(df.index, tf)
    change = np.concatenate([[True], keys[1:] != keys[:-1]])
    bid = np.cumsum(change) - 1
    g = pd.DataFrame({"o": df["open"].values, "h": df["high"].values, "l": df["low"].values,
                      "c": df["close"].values, "b": bid})
    agg = g.groupby("b").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"))
    first = np.flatnonzero(change)
    return HTF(agg.o.values, agg.h.values, agg.l.values, agg.c.values, first, bid)


# ----------------------------------------------------------------------------
# results
# ----------------------------------------------------------------------------
@dataclass
class Zone:
    p: float
    s: float
    lo: float
    hi: float
    htf: bool
    names: list


@dataclass
class Trade:
    dir: int
    entry: float
    sl0: float
    sl: float
    tp1: float
    tp2: float
    rr1: float
    rr2: float
    bar: int
    time: pd.Timestamp
    zlo: float
    zhi: float
    half: bool = False
    lock: bool = False
    grade: str = ""
    score: float = 0.0
    id: str = ""


@dataclass
class Event:
    kind: str                # SIGNAL / LOCK / TP1 / CLOSED
    bar: int
    time: pd.Timestamp
    trade: Trade
    info: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.trade.id}:{self.kind}:{self.time.isoformat()}"


# ----------------------------------------------------------------------------
# the engine
# ----------------------------------------------------------------------------
class Engine:
    def __init__(self, df: pd.DataFrame, dollar: pd.Series | None = None, silver: pd.Series | None = None):
        """df: 30m candles indexed by UTC open time (tz-aware), columns open/high/low/close[/volume].
        dollar: optional EUR/USD 30m closes (dollar direction = opposite), silver: XAG/USD 30m closes."""
        self.df = df
        self.o = df["open"].to_numpy(float)
        self.h = df["high"].to_numpy(float)
        self.l = df["low"].to_numpy(float)
        self.c = df["close"].to_numpy(float)
        self.v = df["volume"].to_numpy(float) if "volume" in df else np.full(len(df), np.nan)
        self.t = df.index
        self.n = len(df)
        self.has_vol = np.nansum(self.v) > 0

        # higher timeframes
        self.H1 = make_htf(df, "1h")
        self.H4 = make_htf(df, "4h")
        self.D = make_htf(df, "D")
        self.W = make_htf(df, "W")

        # ATR (1h, previous completed bar), chart ATR
        self.atr1h = atr(self.H1.h, self.H1.l, self.H1.c, 14)
        self.catr = atr(self.h, self.l, self.c, 14)

        # trend: 4h structure, EMA fallback
        self.st4, self.ev4, self.lv4 = structure(self.H4.h, self.H4.l, self.H4.c, C.STRUCT_LEN)
        self.ema20_4 = ema(self.H4.c, 20)
        self.ema50_4 = ema(self.H4.c, 50)
        self.stD, self.evD, self.lvD = structure(self.D.h, self.D.l, self.D.c, C.STRUCT_LEN)
        self.st1, self.ev1, self.lv1 = structure(self.H1.h, self.H1.l, self.H1.c, C.STRUCT_LEN)
        self.st30, self.ev30, self.lv30 = structure(self.h, self.l, self.c, C.STRUCT_LEN)

        # htf pivots -> raw levels (4h s=2 weight 3, 1h s=3 weight 2), available from the
        # start of the htf bar after the one that confirmed them
        self.pivot_feed = {}   # 30m bar index -> list of (price, weight, htf, name)
        for H, s, w, nm, ishtf in ((self.H4, 2, 3.0, "H4 swing", True), (self.H1, 3, 2.0, "H1 swing", False)):
            ph, pl = pivots(H.h, H.l, s)
            for j in ph:
                k = j + s + 1
                if k < len(H.first):
                    self.pivot_feed.setdefault(int(H.first[k]), []).append((H.h[j], w, ishtf, nm + " high"))
            for j in pl:
                k = j + s + 1
                if k < len(H.first):
                    self.pivot_feed.setdefault(int(H.first[k]), []).append((H.l[j], w, ishtf, nm + " low"))

        # momentum
        self.rsi = rsi(self.c, C.RSI_LEN)
        self.rng5 = sma(self.h - self.l, 5)
        self.rng20 = sma(self.h - self.l, 20)
        self.avgvol = pd.Series(self.v).rolling(20).mean().shift(1).to_numpy()

        # cross-check series aligned to the 30m index (1h closes)
        self.dollar = self._align(dollar)
        self.silver = self._align(silver)

        # sessions
        self._sessions()
        self._vwap()

        # state
        self.raw = []          # (price, weight, htf, name)
        self.sig = []          # (price, time) cooldown marks
        self.brk = []          # dicts: lo, hi, str, dir, bar, htf
        self.trade: Trade | None = None
        self.events: list[Event] = []
        self.stats = {"trades": 0, "wins": 0, "netR": 0.0, "grossW": 0.0, "grossL": 0.0}

    # ------------------------------------------------------------------ helpers
    def _align(self, s):
        if s is None or len(s) == 0:
            return None
        s = s.sort_index()
        return s.reindex(self.t, method="ffill").to_numpy(float)

    def _sessions(self):
        uk = self.t.tz_convert(UK)
        ny = self.t.tz_convert(NY)
        ukm = uk.hour * 60 + uk.minute
        nym = ny.hour * 60 + ny.minute
        tom = lambda s: int(s[:2]) * 60 + int(s[3:])
        inA = (ukm >= tom(C.ASIA[0])) & (ukm < tom(C.ASIA[1]))
        inL = (ukm >= tom(C.LONDON[0])) & (ukm < tom(C.LONDON[1]))
        inN = (nym >= tom(C.NEWYORK[0])) & (nym < tom(C.NEWYORK[1]))
        n = self.n
        self.asH = np.full(n, np.nan); self.asL = np.full(n, np.nan)
        self.lonO = np.full(n, np.nan); self.nyO = np.full(n, np.nan)
        cH = cL = np.nan; aH = aL = lo = no = np.nan
        for i in range(n):
            if inA[i] and (i == 0 or not inA[i - 1]):
                cH, cL = self.h[i], self.l[i]
            elif inA[i]:
                cH = max(cH, self.h[i]) if not np.isnan(cH) else self.h[i]
                cL = min(cL, self.l[i]) if not np.isnan(cL) else self.l[i]
            if (not inA[i]) and i > 0 and inA[i - 1]:
                aH, aL = cH, cL
            if inL[i] and (i == 0 or not inL[i - 1]):
                lo = self.o[i]
            if inN[i] and (i == 0 or not inN[i - 1]):
                no = self.o[i]
            self.asH[i], self.asL[i], self.lonO[i], self.nyO[i] = aH, aL, lo, no

    def _vwap(self):
        self.vwap = np.full(self.n, np.nan)
        if not self.has_vol:
            return
        tp = (self.h + self.l + self.c) / 3
        day = self.D.of_bar
        pv = vv = 0.0
        for i in range(self.n):
            if i == 0 or day[i] != day[i - 1]:
                pv = vv = 0.0
            vol = 0.0 if np.isnan(self.v[i]) else self.v[i]
            pv += tp[i] * vol
            vv += vol
            self.vwap[i] = pv / vv if vv > 0 else np.nan

    def htf_prev(self, H: HTF, i: int):
        """index of the previous COMPLETED htf bar for 30m bar i (or -1)."""
        return int(H.of_bar[i]) - 1

    def atr_at(self, i):
        k = self.htf_prev(self.H1, i)
        return self.atr1h[k] if k >= 0 else np.nan

    def trend_at(self, i):
        k = self.htf_prev(self.H4, i)
        if k < 0:
            return 0
        s = int(self.st4[k])
        if s != 0:
            return s
        f, sl, cc = self.ema20_4[k], self.ema50_4[k], self.H4.c[k]
        return 1 if (f > sl and cc > sl) else -1 if (f < sl and cc < sl) else 0

    def htf_struct(self, H: HTF, st, ev, lv, i):
        k = self.htf_prev(H, i)
        if k < 0:
            return 0, ""
        last = ""
        for j in range(k, max(-1, k - 400), -1):
            if ev[j]:
                last = f"{'CHoCH' if ev[j] == 2 else 'BOS'} {'up' if st[j] > 0 else 'down'} at {lv[j]:.2f}"
                break
        return int(st[k]), last

    def ref_dir(self, series, i):
        if series is None:
            return 0, False
        k = self.htf_prev(self.H1, i)
        if k < C.CROSS_BARS + 1:
            return 0, False
        # use 1h closes of the cross market at the same 1h boundaries
        idx_now = int(self.H1.first[k + 1]) - 1 if k + 1 < len(self.H1.first) else i
        idx_then = int(self.H1.first[k - C.CROSS_BARS + 1]) - 1
        if idx_then < 0:
            return 0, False
        a, b = series[idx_now], series[idx_then]
        if np.isnan(a) or np.isnan(b):
            return 0, False
        # threshold: average 1h move of that market, approximated from its own closes
        seg = series[max(0, idx_then - 14 * 2):idx_now + 1]
        moves = np.abs(np.diff(seg[::2])) if len(seg) > 4 else np.array([0.0])
        thr = C.CROSS_THRESH * (np.nanmean(moves) if len(moves) else 0.0)
        mv = a - b
        return (1 if mv > thr else -1 if mv < -thr else 0), True

    # ------------------------------------------------------------------ zones
    def levels(self, i):
        L = list(self.raw)
        dk = self.htf_prev(self.D, i)
        wk = self.htf_prev(self.W, i)
        if dk >= 0:
            L += [(self.D.h[dk], 3.0, True, "yesterday's high"), (self.D.l[dk], 3.0, True, "yesterday's low")]
        if wk >= 0:
            L += [(self.W.h[wk], 3.0, True, "last week's high"), (self.W.l[wk], 3.0, True, "last week's low")]
        # today so far (including this candle)
        d0 = int(self.D.first[self.D.of_bar[i]])
        L += [(self.h[d0:i + 1].max(), 2.0, False, "today's high"), (self.l[d0:i + 1].min(), 2.0, False, "today's low")]
        for v, w, nm in ((self.asH[i], 2.5, "Asian high"), (self.asL[i], 2.5, "Asian low"),
                         (self.lonO[i], 1.5, "London open"), (self.nyO[i], 1.5, "NY open")):
            if not np.isnan(v):
                L.append((v, w, False, nm))
        ref = self.c[i]
        r = math.floor((ref - C.LEVEL_RANGE) / C.ROUND_STEP) * C.ROUND_STEP
        while r <= ref + C.LEVEL_RANGE:
            w = 3.0 if abs(r % 100.0) < 1e-6 else 2.0 if abs(r % 50.0) < 1e-6 else 1.0
            L.append((r, w, False, "round number"))
            r += C.ROUND_STEP
        return L

    def zones(self, i):
        ref = self.c[i]
        L = sorted((x for x in self.levels(i) if x[0] and not np.isnan(x[0])), key=lambda x: x[0])
        Z = []
        k = 0
        n = len(L)
        while k < n:
            p = L[k][0]
            if abs(p - ref) > C.LEVEL_RANGE:
                k += 1
                continue
            sps = ss = 0.0
            last = p
            htf = False
            names = []
            j = k
            while j < n and L[j][0] - p <= C.CLUSTER_DIST:
                pj, wj, hj, nm = L[j]
                sps += pj * wj
                ss += wj
                last = pj
                htf = htf or hj
                if nm not in names:
                    names.append(nm)
                j += 1
            if ss > 0:
                Z.append(Zone(sps / ss, ss, p, last, htf, names))
            k = j
        return Z

    def role_from(self, zl, zh, i, k0):
        seg = self.c[max(0, i - k0 - 200):i - k0 + 1][::-1]
        hit = np.flatnonzero((seg > zh) | (seg < zl))
        if len(hit) == 0:
            return 0
        return 1 if seg[hit[0]] > zh else -1

    def zone_hist(self, lo, hi, i):
        a = max(0, i - C.FRESH_BARS)
        lw, hg, cl = self.l[a:i][::-1], self.h[a:i][::-1], self.c[a:i][::-1]
        inz = (lw <= hi) & (hg >= lo)
        touches = int(np.sum(inz & ~np.concatenate([[False], inz[:-1]])))
        return touches, bool(np.any(cl > hi)), bool(np.any(cl < lo))

    def recent(self, p, i):
        t = self.t[i]
        return any(abs(sp - p) <= C.CLUSTER_DIST and (t - st) < timedelta(minutes=C.COOLDOWN_MIN) for sp, st in self.sig)

    def targets(self, Z, entry, d, risk, a):
        a = a if a and a > 0 else risk
        front = C.TP_FRONT_ATR * a
        zpad = C.CLUSTER_DIST * 0.15
        nextEdge = majEdge = None
        seq = Z if d > 0 else list(reversed(Z))
        for z in seq:
            edge = z.lo - zpad - front if d > 0 else z.hi + zpad + front
            if (edge - entry) * d > 0.25 * risk:
                if nextEdge is None and z.s >= 2.0:
                    nextEdge = edge
                if z.s >= C.MAJOR_MIN_STR and (edge - entry) * d >= 1.5 * risk:
                    majEdge = edge
                    break
        tp2 = entry + d * 2.5 * risk if majEdge is None else majEdge
        if C.TP2_MAX_ATR > 0 and abs(tp2 - entry) > C.TP2_MAX_ATR * a:
            tp2 = entry + d * C.TP2_MAX_ATR * a
        tp1 = entry + d * risk
        if (tp2 - tp1) * d < 0.25 * risk:
            tp1 = entry + (tp2 - entry) / 2
        return tp1, tp2

    # ------------------------------------------------------------------ momentum
    def mom_read(self, i, d):
        if i < 25:
            return 0.0, ""
        r20 = self.rng20[i]
        push = self.rng5[i] / r20 if r20 and r20 > 0 else 1.0
        move5 = self.c[i] - self.c[i - 5]
        lowA, lowB = self.l[i - 9:i + 1].min(), self.l[i - 19:i - 9].min()
        hiA, hiB = self.h[i - 9:i + 1].max(), self.h[i - 19:i - 9].max()
        rA_lo, rB_lo = np.nanmin(self.rsi[i - 9:i + 1]), np.nanmin(self.rsi[i - 19:i - 9])
        rA_hi, rB_hi = np.nanmax(self.rsi[i - 9:i + 1]), np.nanmax(self.rsi[i - 19:i - 9])
        if d > 0:
            if lowA < lowB and rA_lo > rB_lo + 2:
                return C.MOM_PTS, "momentum fading (RSI divergence)"
            falling = r20 and move5 < -r20
            if falling and push >= 1.5:
                return -C.PUSH_PTS, "strong push in"
            if falling and push <= 0.7:
                return C.MOM_PTS, "momentum fading"
        else:
            if hiA > hiB and rA_hi < rB_hi - 2:
                return C.MOM_PTS, "momentum fading (RSI divergence)"
            rising = r20 and move5 > r20
            if rising and push >= 1.5:
                return -C.PUSH_PTS, "strong push in"
            if rising and push <= 0.7:
                return C.MOM_PTS, "momentum fading"
        return 0.0, ""

    def mom_confirm(self, i, d):
        o, c, h, l = self.o, self.c, self.h, self.l
        if i < 3:
            return False
        if d > 0:
            eng = c[i] > o[i] and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1] and (c[i] - o[i]) >= 0.5 * (h[i] - l[i])
            hook = self.rsi[i] > self.rsi[i - 1] and np.nanmin(self.rsi[i - 3:i]) <= C.RSI_OS
        else:
            eng = c[i] < o[i] and c[i - 1] > o[i - 1] and c[i] <= o[i - 1] and o[i] >= c[i - 1] and (o[i] - c[i]) >= 0.5 * (h[i] - l[i])
            hook = self.rsi[i] < self.rsi[i - 1] and np.nanmax(self.rsi[i - 3:i]) >= C.RSI_OB
        return bool(eng or hook)

    def knife(self, i):
        a = self.catr[i]
        if not C.KNIFE_ON or np.isnan(a) or i < C.KNIFE_BARS:
            return False, False, 0.0
        s = slice(i - C.KNIFE_BARS + 1, i + 1)
        drop = self.h[s].max() - self.c[i]
        rise = self.c[i] - self.l[s].min()
        bigDn = (self.o[s] - self.c[s]).max()
        bigUp = (self.c[s] - self.o[s]).max()
        dn = drop > C.KNIFE_ATR * a or bigDn > C.KNIFE_BIG * a
        up = rise > C.KNIFE_ATR * a or bigUp > C.KNIFE_BIG * a
        return dn, up, max(drop, rise) / a

    # ------------------------------------------------------------------ sweeps
    def sweeps(self, i, a):
        """liquidity sweep on this candle: (long_name, short_name) or ''."""
        if i < 2:
            return "", ""
        dk = self.htf_prev(self.D, i)
        wk = self.htf_prev(self.W, i)
        pools = []
        if dk >= 0:
            pools += [(self.D.l[dk], "yesterday's low"), (self.D.h[dk], "yesterday's high")]
        if wk >= 0:
            pools += [(self.W.l[wk], "last week's low"), (self.W.h[wk], "last week's high")]
        pools += [(self.asL[i], "Asian low"), (self.asH[i], "Asian high")]
        pools += self._swing_pools(i)
        pen = C.SWEEP_MIN_ATR * (a if a and not np.isnan(a) else self.catr[i])
        sl = ss = ""
        for L, nm in pools:
            if L is None or np.isnan(L):
                continue
            if not sl and self.l[i] < L - pen and self.c[i] > L and self.c[i - 1] > L:
                sl = nm
            if not ss and self.h[i] > L + pen and self.c[i] < L and self.c[i - 1] < L:
                ss = nm
        return sl, ss

    def _swing_pools(self, i):
        s = C.STRUCT_LEN
        if not hasattr(self, "_ph30"):
            self._ph30, self._pl30 = pivots(self.h, self.l, s)
        phs = [j for j in self._ph30 if j + s <= i - 1][-2:]
        pls = [j for j in self._pl30 if j + s <= i - 1][-2:]
        out = []
        tol = 0.15 * self.catr[i] if not np.isnan(self.catr[i]) else C.CLUSTER_DIST * 0.1
        if len(phs) == 2 and abs(self.h[phs[0]] - self.h[phs[1]]) <= tol:
            out.append((max(self.h[phs[0]], self.h[phs[1]]), "equal highs"))
        if len(pls) == 2 and abs(self.l[pls[0]] - self.l[pls[1]]) <= tol:
            out.append((min(self.l[pls[0]], self.l[pls[1]]), "equal lows"))
        if phs:
            out.append((self.h[phs[-1]], "swing high"))
        if pls:
            out.append((self.l[pls[-1]], "swing low"))
        return out

    # ------------------------------------------------------------------ scoring
    def score(self, z_str, d, trend, rr2, rej, extra_parts, dS, sS, vr):
        parts = {}
        parts["Zone strength"] = min(z_str, 8.0) / 8.0 * 35.0
        parts["Trend"] = 25.0 if trend == d else 12.0 if trend == 0 else 0.0
        parts["Candle"] = 15.0 * rej
        parts["Reward:risk"] = 15.0 if rr2 >= 3.0 else 10.0 if rr2 >= 2.0 else 5.0
        parts["Base"] = 5.0
        if dS or sS:
            parts["Cross-check"] = C.CROSS_PTS * (dS + sS)
        if self.has_vol and vr >= C.VOL_SPIKE:
            parts["Volume"] = C.VOL_PTS
        parts.update({k: v for k, v in extra_parts.items() if v})
        total = max(0.0, min(100.0, sum(parts.values())))
        return total, parts

    # ------------------------------------------------------------------ main loop
    def run(self, start=0, end=None):
        end = self.n if end is None else end
        for i in range(start, end):
            for lv in self.pivot_feed.get(i, []):
                self.raw.append(lv)
                if len(self.raw) > 400:
                    self.raw.pop(0)
            self.step(i)
        return self.events

    def step(self, i):
        a = self.atr_at(i)
        Z = self.zones(i)
        self._track(i)
        self._breaks(i, Z)
        if self.trade is None and not np.isnan(a) and a > 0 and Z and self.h[i] > self.l[i]:
            self._signals(i, Z, a)
        self._clean_breaks(i)

    # ------------------------------------------------------------------ trade tracker
    def _track(self, i):
        T = self.trade
        if T is None or i <= T.bar:
            return
        d = T.dir
        hitSL = self.l[i] <= T.sl if d > 0 else self.h[i] >= T.sl
        hitT1 = self.h[i] >= T.tp1 if d > 0 else self.l[i] <= T.tp1
        hitT2 = self.h[i] >= T.tp2 if d > 0 else self.l[i] <= T.tp2
        pf = C.PARTIAL_PCT / 100.0
        stopR = d * (T.sl - T.entry) / abs(T.entry - T.sl0)
        trig = T.entry + (T.tp1 - T.entry) * C.LOCK_TRIG / 100.0
        lock_at = min(C.LOCK_AT, C.LOCK_TRIG - 5.0)
        hitLk = C.LOCK_ON and not T.lock and (self.h[i] >= trig if d > 0 else self.l[i] <= trig)
        if not T.half:
            if hitSL:
                self._close(i, stopR, "LOCKED EXIT" if stopR > 0 else "STOPPED", T.sl)
            elif hitT1:
                T.half = True
                T.sl = max(T.sl, T.entry) if d > 0 else min(T.sl, T.entry)
                self.events.append(Event("TP1", i, self.t[i], T, {"new_sl": T.sl}))
                if hitT2:
                    self._close(i, pf * T.rr1 + (1 - pf) * T.rr2, "TARGET", T.tp2)
            elif hitLk:
                T.lock = True
                T.sl = T.entry + (T.tp1 - T.entry) * lock_at / 100.0
                self.events.append(Event("LOCK", i, self.t[i], T, {"new_sl": T.sl, "locked_r": T.rr1 * lock_at / 100.0}))
        else:
            if hitSL:
                self._close(i, pf * T.rr1 + (1 - pf) * max(stopR, 0.0), "BE EXIT", T.sl)
            elif hitT2:
                self._close(i, pf * T.rr1 + (1 - pf) * T.rr2, "TARGET", T.tp2)

    def _close(self, i, r, how, price):
        T = self.trade
        st = self.stats
        st["trades"] += 1
        st["netR"] += r
        if r > 0:
            st["wins"] += 1
            st["grossW"] += r
        else:
            st["grossL"] += -r
        self.events.append(Event("CLOSED", i, self.t[i], T, {"result": how, "r": r, "price": price}))
        self.trade = None

    # ------------------------------------------------------------------ flips
    def _breaks(self, i, Z):
        if i < 1:
            return
        pad = C.CLUSTER_DIST * 0.15
        for z in Z:
            if z.s < C.MIN_LEVEL_STR:
                continue
            bl, bh = z.lo - pad, z.hi + pad
            up = self.c[i] > bh and self.c[i - 1] <= bh
            dn = self.c[i] < bl and self.c[i - 1] >= bl
            if not (up or dn):
                continue
            before = self.role_from(bl, bh, i, 1)
            if (up and before == -1) or (dn and before == 1):
                self.brk.append({"lo": bl, "hi": bh, "str": z.s, "dir": 1 if up else -1, "bar": i, "htf": z.htf, "names": z.names})
                if len(self.brk) > 10:
                    self.brk.pop(0)

    def _clean_breaks(self, i):
        keep = []
        for b in self.brk:
            age = i - b["bar"]
            touched = age >= 1 and (self.l[i] <= b["hi"] if b["dir"] == 1 else self.h[i] >= b["lo"])
            failed = self.c[i] < b["lo"] if b["dir"] == 1 else self.c[i] > b["hi"]
            if touched or (age >= 1 and failed) or age > C.RETEST_BARS:
                continue
            keep.append(b)
        self.brk = keep

    # ------------------------------------------------------------------ signals
    def _signals(self, i, Z, a):
        o, h, l, c = self.o[i], self.h[i], self.l[i], self.c[i]
        rng = h - l
        lw = min(o, c) - l
        uw = h - max(o, c)
        sMax = C.SWEEP_MAX_ATR * a
        buf = C.SL_BUF_ATR * a
        minR = C.MIN_RISK_ATR * a
        maxR = C.MAX_RISK_ATR * a
        trend = self.trend_at(i)
        st30 = int(self.st30[i])
        kDn, kUp, kMove = self.knife(i)
        swL, swS = self.sweeps(i, a)
        vr = (self.v[i] / self.avgvol[i]) if self.has_vol and self.avgvol[i] and self.avgvol[i] > 0 else 0.0
        dDir, dOK = self.ref_dir(self.dollar, i)
        sDir, sOK = self.ref_dir(self.silver, i)
        dDir = -dDir   # EUR/USD up = dollar down
        best = None

        def consider(kind, d, zl, zh, zstr, zhtf, names, level, extra_base, fresh_tag, mom_tag, sl):
            nonlocal best
            risk = (c - sl) * d
            if risk < minR or risk > maxR:
                return
            if trend == -d:
                return
            if (d > 0 and kDn) or (d < 0 and kUp):
                return
            if C.REQUIRE_HTF_ZONE and not zhtf:
                return
            if C.REQUIRE_30M_AGREE and st30 != d:
                return
            tp1, tp2 = self.targets(Z, c, d, risk, a)
            rr1, rr2 = abs(tp1 - c) / risk, abs(tp2 - c) / risk
            if rr2 < C.MIN_RR:
                return
            dS = -dDir * d if dOK else 0      # stronger dollar = weaker gold
            sS = sDir * d if sOK else 0
            rej = min((max(lw, c - o) if d > 0 else max(uw, o - c)) / rng, 1.0)
            swept = swL if d > 0 else swS
            extra = dict(extra_base)
            if swept:
                extra["Liquidity sweep"] = C.SWEEP_BONUS
            sc, parts = self.score(zstr, d, trend, rr2, rej, extra, dS, sS, vr)
            if sc < C.MIN_SCORE:
                return
            if d < 0 and C.NO_A_SELLS and sc >= 75:
                return
            if best is None or sc > best["score"]:
                best = dict(kind=kind, dir=d, zl=zl, zh=zh, zstr=zstr, htf=zhtf, names=names, level=level, sl=sl, tp1=tp1, tp2=tp2,
                            rr1=rr1, rr2=rr2, score=sc, parts=parts, rej=rej, swept=swept, dS=dS, sS=sS, dOK=dOK, sOK=sOK,
                            dDir=dDir, sDir=sDir, fresh=fresh_tag, mom=mom_tag, vr=vr, risk=risk, trend=trend, st30=st30,
                            knife=kMove)

        # SETUP 1: bounce off a zone that already held its role
        for z in Z:
            if z.s < C.MIN_LEVEL_STR or self.recent(z.p, i):
                continue
            zp = C.CLUSTER_DIST * 0.15
            zl, zh = z.lo - zp, z.hi + zp
            nearL = l <= zh and l >= zl - sMax and c > zl
            nearS = h >= zl and h <= zh + sMax and c < zh
            if not (nearL or nearS):
                continue
            prev = self.role_from(zl, zh, i, 1)
            wickL, wickS = lw >= 0.4 * rng, uw >= 0.4 * rng
            revL = c > o and c > self.c[i - 1]
            revS = c < o and c < self.c[i - 1]
            isL = prev == 1 and nearL and (wickL or revL)
            isS = prev == -1 and nearS and (wickS or revS)
            for d, ok in ((1, isL), (-1, isS)):
                if not ok:
                    continue
                sl = min(zl, l) - buf if d > 0 else max(zh, h) + buf
                touches, cA, cB = self.zone_hist(zl, zh, i)
                flip = cB if d > 0 else cA
                fAdj = (5.0 if touches <= 1 else -8.0 if touches >= 4 else 0.0) + (5.0 if flip else 0.0)
                ftag = ("fresh" if touches <= 1 else f"tested {touches}x") + (", flipped" if flip else "")
                mAdj, mTag = self.mom_read(i, d)
                conf = self.mom_confirm(i, d)
                extra = {"Freshness": fAdj, "Momentum": mAdj, "Confirmation (engulf / RSI hook)": 5.0 if conf else 0.0}
                how = "rejection wick" if (wickL if d > 0 else wickS) else "reversal close"
                consider(f"bounce off {'support' if d > 0 else 'resistance'} ({how})", d, zl, zh, z.s, z.htf, z.names, z.p,
                         extra, ftag, mTag, sl)

        # SETUP 2: first pullback retest of a freshly flipped zone
        for b in self.brk:
            age = i - b["bar"]
            if age < 1 or age > C.RETEST_BARS:
                continue
            bl, bh, bd = b["lo"], b["hi"], b["dir"]
            mid = (bl + bh) / 2
            if self.recent(mid, i):
                continue
            wickL, wickS = lw >= 0.4 * rng, uw >= 0.4 * rng
            revL = c > o and c > self.c[i - 1]
            revS = c < o and c < self.c[i - 1]
            rtL = bd == 1 and l <= bh and l >= bl - sMax and c > bl and (wickL or revL)
            rtS = bd == -1 and h >= bl and h <= bh + sMax and c < bh and (wickS or revS)
            for d, ok in ((1, rtL), (-1, rtS)):
                if not ok:
                    continue
                sl = min(bl, l) - buf if d > 0 else max(bh, h) + buf
                mAdj, mTag = self.mom_read(i, d)
                extra = {"Retest bonus": C.RETEST_PTS, "Momentum": mAdj}
                consider("retest of new " + ("support (flipped from resistance)" if d > 0 else "resistance (flipped from support)"),
                         d, bl, bh, b["str"], b["htf"], b["names"], mid, extra, "flip retest (first pullback)", mTag, sl)

        if best is None:
            return
        g = "A" if best["score"] >= 75 else "B" if best["score"] >= 62 else "C"
        T = Trade(best["dir"], c, best["sl"], best["sl"], best["tp1"], best["tp2"], best["rr1"], best["rr2"], i, self.t[i],
                  best["zl"], best["zh"], grade=g, score=best["score"])
        T.id = f"{'B' if T.dir > 0 else 'S'}{self.t[i].strftime('%Y%m%d%H%M')}"
        self.trade = T
        self.sig.append((best["level"], self.t[i]))
        self.sig = self.sig[-64:]
        info = dict(best)
        info["grade"] = g
        info["context"] = self.context(i)
        self.events.append(Event("SIGNAL", i, self.t[i], T, info))

    # ------------------------------------------------------------------ context for emails
    def context(self, i):
        sD, lD = self.htf_struct(self.D, self.stD, self.evD, self.lvD, i)
        s4, l4 = self.htf_struct(self.H4, self.st4, self.ev4, self.lv4, i)
        s1, l1 = self.htf_struct(self.H1, self.st1, self.ev1, self.lv1, i)
        s30 = int(self.st30[i])
        last30 = ""
        for j in range(i, max(-1, i - 400), -1):
            if self.ev30[j]:
                last30 = f"{'CHoCH' if self.ev30[j] == 2 else 'BOS'} {'up' if self.st30[j] > 0 else 'down'} at {self.lv30[j]:.2f}"
                break
        return {
            "trend4h_used": self.trend_at(i),
            "rows": [("Daily  (big picture)", sD, lD), ("H4     (main trend)", s4, l4),
                     ("1H", s1, l1), ("30m    (signal chart)", s30, last30)],
            "vwap": self.vwap[i],
            "rsi": self.rsi[i],
            "atr": self.atr_at(i),
            "close": self.c[i],
        }

    def nearest_zones(self, i):
        Z = [z for z in self.zones(i) if z.s >= C.MIN_LEVEL_STR]
        c = self.c[i]
        below = [z for z in Z if z.hi < c]
        above = [z for z in Z if z.lo > c]
        return (below[-1] if below else None), (above[0] if above else None)
