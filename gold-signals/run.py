"""Live runner for the gold signal bot (GitHub Actions, every 5 minutes).

Each run:
1. Skips cheaply when the market is closed, or when there is no new 30m candle AND
   nothing to watch on 5m (keeps Twelve Data usage inside the free tier).
2. New 30m candle: fetch recent 30m candles, replay the engine (deterministic, so the
   open trade is rebuilt from history) and email every new event: signal, lock, TP1, close.
   It also starts a 5m WATCH when price trades into an H4 zone in the H4 trend direction.
3. While watching, or while a 5m trade is open: fetch recent 5m candles, look for the
   5m ENTRY and manage that trade (lock / TP1 / close emails).
4. Once a weekday after 07:00 UK: a short daily status email.
State lives in state.json, committed back by the workflow.

Usage: python3 gold-signals/run.py [--test]   (--test emails the latest signal + a daily status)
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
import data  # noqa: E402
import emailer  # noqa: E402
import helper5m as H  # noqa: E402
from engine import Engine  # noqa: E402

STATE = Path(__file__).parent / "state.json"
NY = ZoneInfo("America/New_York")
UK = ZoneInfo("Europe/London")


def load_state():
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    for k, v in {"last_bar": None, "sent": [], "last_daily": None, "last_fetch": None,
                 "watch": None, "t5": None, "last5": None}.items():
        st.setdefault(k, v)
    return st


def save_state(st):
    st["sent"] = st["sent"][-300:]
    STATE.write_text(json.dumps(st, indent=2, default=str) + "\n")


def market_closed(now):
    ny = now.astimezone(NY)
    wd, hm = ny.weekday(), ny.hour * 60 + ny.minute
    if wd == 5:                       # Saturday
        return True
    if wd == 4 and hm >= 17 * 60:     # Friday after 17:00 NY
        return True
    if wd == 6 and hm < 18 * 60:      # Sunday before 18:00 NY
        return True
    return False


def tally(events, since):
    closed = [e for e in events if e.kind == "CLOSED" and e.time >= since]
    if not closed:
        return "no closed trades"
    r = sum(e.info["r"] for e in closed)
    w = sum(1 for e in closed if e.info["r"] > 0)
    return f"{len(closed)} closed · {w} won · {r:+.2f}R"


# ---------------------------------------------------------------- 30m
def process_30m(now, st, daily_due, uk_now):
    lf = st.get("last_fetch")
    if lf and now - datetime.fromisoformat(lf) < timedelta(minutes=4) and not daily_due:
        print("30m: fetched less than 4 minutes ago - waiting for the data feed.")
        return
    gold, eur, xag, source = data.get_data(now)
    st["last_fetch"] = now.isoformat()
    st["source"] = source
    if len(gold) < 500:
        print(f"30m: only {len(gold)} candles returned - skipping.")
        return

    eng = Engine(gold, dollar=eur, silver=xag)
    events = eng.run()
    last_bar = gold.index[-1]
    i_last = len(gold) - 1
    week_ago = last_bar - pd.Timedelta(days=7)
    prev = pd.Timestamp(st["last_bar"]) if st.get("last_bar") else None
    new_bar = prev is None or last_bar > prev

    if new_bar:
        cutoff = prev if prev is not None else last_bar - pd.Timedelta(minutes=1)
        sent = set(st["sent"])
        for ev in events:
            if ev.time <= cutoff or ev.key in sent:
                continue
            if ev.kind == "SIGNAL":
                subj, body = emailer.signal_email(ev, source)
            else:
                subj, body = emailer.follow_email(ev, tally(events, week_ago))
            emailer.send(subj, body)
            print("sent:", subj)
            st["sent"].append(ev.key)

        # 5m helper: end a broken / expired watch, maybe start a new one
        if C.HELPER_5M:
            w = st.get("watch")
            if w and (H.broken(w, float(gold["close"].iloc[-1])) or pd.Timestamp(w["expires"]) <= last_bar + pd.Timedelta(minutes=30)
                      or eng.trade is not None):
                print("5m: watch ended.")
                st["watch"] = None
            if st.get("watch") is None and st.get("t5") is None:
                cand = H.candidate(eng, i_last)
                if cand:     # don't re-watch the same zone within 4 hours
                    mid = (cand["zl"] + cand["zh"]) / 2
                    recent = [x for x in st.get("watched", []) if last_bar - pd.Timestamp(x[1]) < pd.Timedelta(hours=4)]
                    st["watched"] = recent
                    if any(abs(m - mid) <= C.CLUSTER_DIST for m, _ in recent):
                        cand = None
                if cand:
                    st["watched"].append([(cand["zl"] + cand["zh"]) / 2, last_bar.isoformat()])
                    subj, body = emailer.watch_email(cand)
                    emailer.send(subj, body)
                    print("sent:", subj)
                    st["watch"] = cand
                    st["last5"] = (pd.Timestamp(cand["start"]) - pd.Timedelta(minutes=5)).isoformat()
        st["last_bar"] = last_bar.isoformat()
    else:
        print(f"30m: feed has no new candle yet (latest {last_bar}).")

    if daily_due:
        subj, body = emailer.daily_email(eng, i_last, [e for e in events if e.time >= last_bar - pd.Timedelta(hours=24)],
                                         tally(events, week_ago), source)
        emailer.send(subj, body)
        st["last_daily"] = uk_now.date().isoformat()
        print("sent:", subj)
    print(f"30m: processed up to {last_bar} ({source}). Open trade: {eng.trade.id if eng.trade else 'none'}")


# ---------------------------------------------------------------- 5m
def process_5m(now, st):
    b5 = data.get_5m(now=now)
    if len(b5) < 3:
        return
    last5 = pd.Timestamp(st["last5"]) if st.get("last5") else b5.index[-2]
    new = [k for k in range(1, len(b5)) if b5.index[k] > last5]     # last5 = open time of the last processed 5m candle
    for k in new:
        t = b5.index[k]
        if st.get("t5"):
            for kind, info in H.step(st["t5"], b5.iloc[k]):
                subj, body = emailer.follow5_email(st["t5"], kind, info, t)
                emailer.send(subj, body)
                print("sent:", subj)
            if st["t5"].get("closed"):
                st["t5"] = None
        elif st.get("watch"):
            w = st["watch"]
            if t >= pd.Timestamp(w["expires"]):
                print("5m: watch expired.")
                st["watch"] = None
                continue
            tr = H.trigger(b5, k, w)
            if tr:
                subj, body = emailer.entry5_email(tr, w)
                emailer.send(subj, body)
                print("sent:", subj)
                st["t5"] = tr
                st["watch"] = None
        st["last5"] = t.isoformat()
    print(f"5m: processed up to {b5.index[-1]}. Watching: {bool(st.get('watch'))}. 5m trade: {st['t5']['id'] if st.get('t5') else 'none'}")


# ---------------------------------------------------------------- main
def main():
    test = "--test" in sys.argv
    now = datetime.now(timezone.utc)
    st = load_state()
    uk_now = now.astimezone(UK)

    if test:
        gold, eur, xag, source = data.get_data(now)
        eng = Engine(gold, dollar=eur, silver=xag)
        events = eng.run()
        last_bar, i_last = gold.index[-1], len(gold) - 1
        sig = [e for e in events if e.kind == "SIGNAL"]
        if sig:
            s, b = emailer.signal_email(sig[-1], source)
            emailer.send("[TEST] " + s, b)
        s, b = emailer.daily_email(eng, i_last, [e for e in events if e.time >= last_bar - pd.Timedelta(hours=24)],
                                   tally(events, last_bar - pd.Timedelta(days=7)), source)
        emailer.send("[TEST] " + s, b)
        print("Test emails sent.")
        return

    daily_due = uk_now.weekday() < 5 and uk_now.hour >= 7 and st.get("last_daily") != uk_now.date().isoformat()
    if market_closed(now) and not daily_due:
        print("Market closed - nothing to do.")
        return

    latest_closed_open = pd.Timestamp(now).floor("30min") - pd.Timedelta(minutes=30)
    need30 = not (st.get("last_bar") and pd.Timestamp(st["last_bar"]) >= latest_closed_open) or daily_due
    need5 = C.HELPER_5M and (st.get("watch") or st.get("t5"))
    if not need30 and not need5:
        print("No new 30m candle and nothing to watch on 5m.")
        return

    if need30:
        process_30m(now, st, daily_due, uk_now)
    if C.HELPER_5M and (st.get("watch") or st.get("t5")):
        try:
            process_5m(now, st)
        except Exception as exc:          # a 5m hiccup must never stop the 30m bot
            print("5m helper error:", exc)
    save_state(st)


if __name__ == "__main__":
    main()
