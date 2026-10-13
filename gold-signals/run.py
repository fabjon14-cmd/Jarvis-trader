"""Live runner for the gold signal bot (GitHub Actions, every 5 minutes).

Each run:
1. Skips cheaply when the market is closed or no new 30m candle can have closed
   since the last processed one (keeps Twelve Data usage well inside the free tier).
2. Fetches recent 30m candles and replays the engine over them (deterministic,
   so the open trade is rebuilt from history - nothing else needs storing).
3. Emails every event on candles not processed before: new signal, lock, TP1, close.
4. Once a weekday after 07:00 UK, emails a short daily status.
State (last processed candle, sent event keys) lives in state.json, committed back by the workflow.

Usage: python3 gold-signals/run.py [--test]   (--test emails the latest signal in history + a daily status)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import config as C  # noqa: E402
import data  # noqa: E402
import emailer  # noqa: E402
from engine import Engine  # noqa: E402

STATE = Path(__file__).parent / "state.json"
NY = ZoneInfo("America/New_York")
UK = ZoneInfo("Europe/London")


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_bar": None, "sent": [], "last_daily": None, "last_fetch": None}


def save_state(st):
    st["sent"] = st["sent"][-300:]
    STATE.write_text(json.dumps(st, indent=2) + "\n")


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


def main():
    test = "--test" in sys.argv
    now = datetime.now(timezone.utc)
    st = load_state()

    uk_now = now.astimezone(UK)
    daily_due = uk_now.weekday() < 5 and uk_now.hour >= 7 and st.get("last_daily") != uk_now.date().isoformat()

    latest_closed_open = pd.Timestamp(now).floor("30min") - pd.Timedelta(minutes=30)
    have_latest = st.get("last_bar") and pd.Timestamp(st["last_bar"]) >= latest_closed_open
    if not test:
        if market_closed(now) and not daily_due:
            print("Market closed - nothing to do.")
            return
        if have_latest and not daily_due:
            print("No new 30m candle since the last run.")
            return
        lf = st.get("last_fetch")
        if lf and now - datetime.fromisoformat(lf) < timedelta(minutes=4) and not daily_due:
            print("Fetched less than 4 minutes ago - waiting for the data feed.")
            return

    gold, eur, xag, source = data.get_data(now)
    st["last_fetch"] = now.isoformat()
    if len(gold) < 500:
        print(f"Only {len(gold)} candles returned - skipping.")
        save_state(st)
        return

    eng = Engine(gold, dollar=eur, silver=xag)
    events = eng.run()
    last_bar = gold.index[-1]
    i_last = len(gold) - 1
    week_ago = last_bar - pd.Timedelta(days=7)

    if test:
        sig = [e for e in events if e.kind == "SIGNAL"]
        if sig:
            s, b = emailer.signal_email(sig[-1], source)
            emailer.send("[TEST] " + s, b)
        s, b = emailer.daily_email(eng, i_last, [e for e in events if e.time >= last_bar - pd.Timedelta(hours=24)],
                                   tally(events, week_ago), source)
        emailer.send("[TEST] " + s, b)
        print("Test emails sent.")
        return

    prev = pd.Timestamp(st["last_bar"]) if st.get("last_bar") else None
    if prev is not None and last_bar <= prev and not daily_due:
        print(f"Feed has no new candle yet (latest {last_bar}).")
        save_state(st)
        return

    # first ever run: only alert on the newest candle, not the whole history
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

    if daily_due:
        subj, body = emailer.daily_email(eng, i_last, [e for e in events if e.time >= last_bar - pd.Timedelta(hours=24)],
                                         tally(events, week_ago), source)
        emailer.send(subj, body)
        st["last_daily"] = uk_now.date().isoformat()
        print("sent:", subj)

    st["last_bar"] = max(last_bar, prev).isoformat() if prev is not None else last_bar.isoformat()
    save_state(st)
    print(f"Processed up to {last_bar} ({source}). Open trade: {eng.trade.id if eng.trade else 'none'}")


if __name__ == "__main__":
    main()
