"""Email texts (layout agreed with the operator, Oct 2026) and Resend sender."""

import math
import os
from datetime import timedelta
from zoneinfo import ZoneInfo

import numpy as np
import requests

import config as C

UK = ZoneInfo("Europe/London")


def send(subject, body):
    key = os.getenv("RESEND_API_KEY")
    to = os.getenv("REPORT_TO_EMAIL")
    if not key or not to:
        print("---- (email not configured, printing instead) ----\n" + subject + "\n\n" + body)
        return {"printed": True}
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"from": "Gold Signals <onboarding@resend.dev>", "to": [to], "subject": subject, "text": body},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- helpers
def p(x):
    return f"{x:.2f}"


def p1(x):
    return f"{x:.1f}"


def uk_time(ts, close=True):
    t = ts + timedelta(minutes=30) if close else ts
    return t.tz_convert(UK).strftime("%a %d %b %H:%M")


def side(d):
    return "BUY" if d > 0 else "SELL"


def dirw(s):
    return "UP" if s > 0 else "DOWN" if s < 0 else "FLAT"


def tick(ok):
    return "✔" if ok else "✘"


def zone_label(s):
    return "Strong" if s >= 8 else "Good" if s >= 5 else "Weak"


def lots(risk):
    risk_cash = C.ACCOUNT_SIZE * C.RISK_PCT / 100.0
    raw = risk_cash / (risk * C.CONTRACT_OZ)
    lt = math.floor(raw / C.LOT_STEP) * C.LOT_STEP
    if lt < C.LOT_STEP:
        actual = C.LOT_STEP * C.CONTRACT_OZ * risk
        return f"{C.LOT_STEP:.2f} lots (minimum lot risks ${actual:.2f}, more than {C.RISK_PCT:g}% of ${C.ACCOUNT_SIZE:,.0f})"
    actual = lt * C.CONTRACT_OZ * risk
    return f"{lt:.2f} lots (risk ${actual:.2f} = {C.RISK_PCT:g}% of ${C.ACCOUNT_SIZE:,.0f})"


# ---------------------------------------------------------------- new signal
def signal_email(ev, source):
    T, I = ev.trade, ev.info
    d = T.dir
    S = side(d)
    ctx = I["context"]
    kind_zone = "H4 " + ("support" if d > 0 else "resistance") if I.get("htf", True) else ("support" if d > 0 else "resistance")
    srcs = ", ".join(n for n in I["names"] if n != "round number") or "round number"

    subj = (f"GOLD {S} ({I['grade']}, {I['score']:.0f}/100) · Zone {p1(I['zl'])}-{p1(I['zh'])} · "
            f"SL {p1(T.sl0)} · TP1 {T.tp1:.0f} / TP2 {T.tp2:.0f}")

    L = []
    L.append(f"NEW SIGNAL: {S} {C.SYMBOL_LABEL} — {uk_time(ev.time)} (UK)")
    L.append("")
    L.append("ENTRY")
    L.append(f"  Entry zone:   {p1(I['zl'])} – {p1(I['zh'])}  ({kind_zone}: {srcs})")
    L.append(f"  Signal price: {p(T.entry)} (30m candle close)")
    L.append(f"  Stop loss:    {p(T.sl0)}  ({'below' if d > 0 else 'above'} the zone + buffer) · risk {p1(I['risk'])}")
    L.append(f"  TP1:          {p(T.tp1)}  ({T.rr1:.1f}R) → close {C.PARTIAL_PCT:g}%, then lock/BE")
    L.append(f"  TP2:          {p(T.tp2)}  ({T.rr2:.1f}R) → next major {'resistance' if d > 0 else 'support'} zone")
    L.append(f"  Size:         {lots(I['risk'])}")
    L.append("")

    L.append("TREND CHECK                     Direction   Agrees?")
    agree = 0
    for name, s, last in ctx["rows"]:
        ok = s == d
        agree += ok
        L.append(f"  {name:<30}{dirw(s):<12}{tick(ok)}" + (f"  last: {last}" if last else ""))
    L.append(f"  → {agree}/{len(ctx['rows'])} aligned with the {S}")
    L.append("")

    L.append("WHY THIS TRADE")
    L.append(f"  ✔ Price at {kind_zone} {p1(I['zl'])}–{p1(I['zh'])} (strength {I['zstr']:.0f}, {zone_label(I['zstr'])}; {I['fresh']})")
    wick_pct = I["rej"] * 100
    L.append(f"  ✔ 30m {I['kind']} — {wick_pct:.0f}% of the candle, closed back {'above' if d > 0 else 'below'} the zone edge")
    if I["swept"]:
        L.append(f"  ✔ Liquidity sweep: wicked through {I['swept']} and reclaimed it")
    else:
        L.append("  ✘ No liquidity sweep on the signal candle")
    vw = ctx["vwap"]
    if vw is not None and not np.isnan(vw):
        ok = (T.entry > vw) if d > 0 else (T.entry < vw)
        L.append(f"  {tick(ok)} {'Above' if T.entry > vw else 'Below'} session VWAP ({p(vw)})")
    if I["dOK"]:
        dd = I["dDir"]
        ok = dd * -d > 0
        txt = {1: "rising", -1: "falling", 0: "flat"}[dd]
        L.append(f"  {tick(ok) if dd else '•'} Dollar {txt} ({'supports' if ok else 'works against' if dd else 'neutral for'} gold {'up' if d > 0 else 'down'})")
    if I["sOK"]:
        sd = I["sDir"]
        ok = sd * d > 0
        txt = {1: "up", -1: "down", 0: "flat"}[sd]
        L.append(f"  {tick(ok) if sd else '•'} Silver {txt} ({'agrees' if ok else 'disagrees' if sd else 'neutral'})")
    if not I["dOK"] and not I["sOK"]:
        L.append("  • Dollar / silver check unavailable on this data feed")
    if I.get("vr"):
        L.append(f"  {tick(I['vr'] >= C.VOL_SPIKE)} Volume {I['vr']:.1f}x average on the signal candle")
    L.append(f"  ✔ No falling-knife: last {C.KNIFE_BARS} candles moved {I['knife']:.1f} ATR (limit {C.KNIFE_ATR:g})")
    if I["mom"]:
        good = "fading" in I["mom"]
        L.append(f"  {tick(good)} Momentum: {I['mom']}")
    r = ctx["rsi"]
    if r is not None and not np.isnan(r):
        if d > 0:
            L.append(f"  {tick(r <= C.RSI_OS)} RSI {r:.0f} ({'oversold' if r <= C.RSI_OS else 'not oversold'})")
        else:
            L.append(f"  {tick(r >= C.RSI_OB)} RSI {r:.0f} ({'overbought' if r >= C.RSI_OB else 'not overbought'})")
    L.append("")

    L.append(f"SCORE {I['score']:.0f}/100 ({I['grade']})")
    L.append("  " + " · ".join(f"{k} {v:+.0f}" if v < 0 else f"{k} {v:.0f}" for k, v in I["parts"].items()))
    L.append("")

    L.append("WHAT WOULD CANCEL IT")
    L.append(f"  • A 30m close {'below' if d > 0 else 'above'} {p(T.sl0)} (SL), or an H4 close {'below' if d > 0 else 'above'} "
             f"{p1(I['zl'] if d > 0 else I['zh'])} = zone broken")
    L.append("  • If price runs to TP1 without you in, don't chase")
    L.append("")

    lock_at = min(C.LOCK_AT, C.LOCK_TRIG - 5.0)
    trig = T.entry + (T.tp1 - T.entry) * C.LOCK_TRIG / 100.0
    lock_px = T.entry + (T.tp1 - T.entry) * lock_at / 100.0
    L.append("PLAN")
    L.append(f"  1. Enter inside {p1(I['zl'])}–{p1(I['zh'])} (or at market near {p(T.entry)})")
    if C.LOCK_ON:
        L.append(f"  2. At {C.LOCK_TRIG:g}% of the way to TP1 (≈{p1(trig)}) the stop moves to {p1(lock_px)} (locked profit)")
    L.append(f"  3. At TP1: close {C.PARTIAL_PCT:g}%, stop to break-even or better")
    L.append(f"  4. Let the rest run to TP2 {p(T.tp2)}")
    L.append("")
    L.append(f"Data: {source}. Signals only — no orders are placed. Not financial advice.")
    return subj, "\n".join(L)


# ---------------------------------------------------------------- follow-ups
def follow_email(ev, tally):
    T, I = ev.trade, ev.info
    S = side(T.dir)
    head = f"{S} from {p(T.entry)} (signal {uk_time(T.time)})"
    if ev.kind == "LOCK":
        subj = f"GOLD {S} · LOCK · move SL to {p1(I['new_sl'])}"
        body = (f"{head}\n\nPrice reached {C.LOCK_TRIG:g}% of the way to TP1 ({p(T.tp1)}).\n"
                f"Move your stop loss to {p(I['new_sl'])} (+{I['locked_r']:.2f}R locked).\n\n"
                f"TP1 {p(T.tp1)} · TP2 {p(T.tp2)}\nTime: {uk_time(ev.time)} (UK)")
    elif ev.kind == "TP1":
        subj = f"GOLD {S} · TP1 HIT · close {C.PARTIAL_PCT:g}%, SL to {p1(I['new_sl'])}"
        body = (f"{head}\n\nTP1 {p(T.tp1)} reached ({T.rr1:.1f}R).\n"
                f"Close {C.PARTIAL_PCT:g}% of the position and move the stop to {p(I['new_sl'])}.\n"
                f"Remaining target: TP2 {p(T.tp2)} ({T.rr2:.1f}R).\n\nTime: {uk_time(ev.time)} (UK)")
    else:
        res, r = I["result"], I["r"]
        names = {"TARGET": "TP2 hit", "STOPPED": "stopped out", "BE EXIT": "rest closed at break-even / locked stop",
                 "LOCKED EXIT": "closed at the locked stop"}
        subj = f"GOLD {S} · CLOSED · {names.get(res, res)} · {r:+.2f}R"
        body = (f"{head}\n\nResult: {names.get(res, res)} at {p(I['price'])}\nTrade result: {r:+.2f}R\n\n"
                f"Last 7 days: {tally}\n\nTime: {uk_time(ev.time)} (UK)")
    return subj, body


# ---------------------------------------------------------------- daily status
def daily_email(eng, i, events_24h, tally, source):
    ctx = eng.context(i)
    sup, res = eng.nearest_zones(i)
    L = [f"GOLD DAILY STATUS — {uk_time(eng.t[i])} (UK) · price {p(eng.c[i])}", "", "TREND"]
    for name, s, last in ctx["rows"]:
        L.append(f"  {name:<30}{dirw(s):<8}" + (f"  last: {last}" if last else ""))
    t4 = ctx["trend4h_used"]
    bias = "look for BUYS at H4 support" if t4 > 0 else "look for SELLS at H4 resistance" if t4 < 0 else "no clear H4 trend — be selective"
    L.append(f"  → Bias: {bias}")
    L.append("")
    L.append("KEY ZONES")
    L.append(f"  Resistance: {p1(res.lo)}–{p1(res.hi)} (strength {res.s:.0f}{', H4' if res.htf else ''})" if res else "  Resistance: none nearby")
    L.append(f"  Support:    {p1(sup.lo)}–{p1(sup.hi)} (strength {sup.s:.0f}{', H4' if sup.htf else ''})" if sup else "  Support: none nearby")
    L.append("")
    T = eng.trade
    if T:
        L.append(f"OPEN TRADE: {side(T.dir)} from {p(T.entry)} · SL {p(T.sl)} · TP1 {p(T.tp1)}{' (hit)' if T.half else ''} · TP2 {p(T.tp2)}")
    else:
        L.append("OPEN TRADE: none")
    L.append("")
    L.append("LAST 24 HOURS")
    if events_24h:
        for e in events_24h:
            L.append(f"  {uk_time(e.time)}  {e.kind:<7} {side(e.trade.dir)} {p(e.trade.entry)}"
                     + (f"  {e.info['result']} {e.info['r']:+.2f}R" if e.kind == "CLOSED" else ""))
    else:
        L.append("  No signals.")
    L.append("")
    L.append(f"Last 7 days: {tally}")
    L.append(f"\nBot is running. Data: {source}.")
    return f"GOLD · DAILY STATUS · {eng.t[i].tz_convert(UK).strftime('%a %d %b')}", "\n".join(L)
