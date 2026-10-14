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
                 "watch": None, "t5": None, "arm": None, "last_1m": None, "watched": []}.items():
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
        return None
    gold, eur, xag, source = data.get_data(now)
    st["last_fetch"] = now.isoformat()
    st["source"] = source
    if len(gold) < 500:
        print(f"30m: only {len(gold)} candles returned - skipping.")
        return None

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
        stale = []
        fresh_after = pd.Timestamp(now) - pd.Timedelta(minutes=C.STALE_MIN) - pd.Timedelta(minutes=30)
        for ev in events:
            if ev.time <= cutoff or ev.key in sent:
                continue
            if ev.time < fresh_after:          # bot was not running when this happened: summarise, don't alert
                stale.append(ev)
                st["sent"].append(ev.key)
                continue
            if ev.kind == "SIGNAL":
                subj, body = emailer.signal_email(ev, source)
            else:
                subj, body = emailer.follow_email(ev, tally(events, week_ago))
            emailer.send(subj, body)
            print("sent:", subj)
            st["sent"].append(ev.key)

        if stale:
            lines = [f"The bot last checked the market at {emailer.uk_time(prev) if prev is not None else '?'} (UK) — runs did not start after that.",
                     "These happened in the meantime. They are NOT live alerts — don't act on them now:", ""]
            for e in stale:
                lines.append(f"  {emailer.uk_time(e.time)}  {e.kind:<7} {emailer.side(e.trade.dir)} {e.trade.entry:.2f}"
                             + (f"  {e.info['result']} {e.info['r']:+.2f}R" if e.kind == "CLOSED" else ""))
            T = eng.trade
            lines += ["", f"Open trade now: {emailer.side(T.dir)} from {T.entry:.2f} · SL {T.sl:.2f} · TP1 {T.tp1:.2f} · TP2 {T.tp2:.2f}" if T else "Open trade now: none"]
            emailer.send(f"GOLD · CATCH-UP · {len(stale)} event(s) while the bot was offline", "\n".join(lines))
            print("sent catch-up for", len(stale), "events")

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
    return (eng, i_last) if new_bar else None


# ---------------------------------------------------------------- 1m / 5m helper
def helper_events(st, evs, b1, now):
    """Email the helper's events (WATCH / ENTRY / LOCK / TP1 / CLOSED)."""
    for kind, x in evs:
        if kind == "WATCH":
            subj, body = emailer.watch_email(x)
            emailer.send(subj, body)
            print("sent:", subj)
        elif kind == "WATCH_END":
            print(f"helper: watch {x['zl']:.1f}-{x['zh']:.1f} ended")
        elif kind == "ENTRY":
            hit, w = x
            mins = 1 if hit["tf"] == "1m" else 5
            closed_at = pd.Timestamp(hit["time"]) + pd.Timedelta(minutes=mins)
            moved = H.already_moved(hit, b1, closed_at - pd.Timedelta(minutes=1))
            if moved:
                st["t5"] = None
                print(f"helper: {hit['tf']} entry at {hit['entry']:.2f} skipped - price already reached the {moved} before this run")
                continue
            hit["price_now"] = float(b1["close"].iloc[-1])
            hit["now"] = b1.index[-1].isoformat()
            subj, body = emailer.entry5_email(hit, w)
            emailer.send(subj, body)
            print("sent:", subj)
        else:
            tr, info = x
            bt = pd.Timestamp(info.get("_t")) if info.get("_t") else None
            subj, body = emailer.follow5_email(tr, kind, info, bt)
            emailer.send(subj, body)
            print("sent:", subj)


def run_1m(st, b1, after, until, now):
    for t, evs in H.feed(st, b1, after=after, until=until):
        for kind, x in evs:
            if kind in ("LOCK", "TP1", "CLOSED"):
                tr = x[0]
                x[1]["_t"] = (t if tr.get("tf") == "1m" else t - pd.Timedelta(minutes=4)).isoformat()
        helper_events(st, evs, b1, now)
        st["last_1m"] = t.isoformat()


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
    need1 = C.HELPER_5M and bool(st.get("watch") or st.get("t5") or st.get("arm"))
    if not need30 and not need1:
        print("No new 30m candle and nothing to watch on 1m/5m.")
        return

    b1 = None
    if need1:
        try:
            b1 = data.get_tf("1m", n=300, now=now)
        except Exception as exc:          # a 1m hiccup must never stop the 30m bot
            print("1m fetch error:", exc)
    last = pd.Timestamp(st["last_1m"]) if st.get("last_1m") else None
    if b1 is not None and len(b1) > 2 and (last is None or last < b1.index[-1] - pd.Timedelta(minutes=60)):
        last = b1.index[-1] - pd.Timedelta(minutes=10)     # first run / long gap: don't replay old candles

    if need30:
        res = process_30m(now, st, daily_due, uk_now)
        if res and C.HELPER_5M:
            eng, i = res
            t_close = eng.t[i] + pd.Timedelta(minutes=30)
            try:
                if b1 is not None and last is not None and last < t_close - pd.Timedelta(minutes=1):
                    run_1m(st, b1, last, t_close, now)          # candles before this 30m close, old watch
                evs = H.on_30m(st, eng, i)
                helper_events(st, evs, b1, now)
                if any(k == "WATCH" for k, _ in evs) and st.get("last_1m"):
                    st["last_1m"] = min(pd.Timestamp(st["last_1m"]), t_close - pd.Timedelta(minutes=1)).isoformat()
                if st.get("watch") or st.get("arm"):
                    if b1 is None:
                        b1 = data.get_tf("1m", n=300, now=now)
                    if not st.get("last_1m") or pd.Timestamp(st["last_1m"]) < t_close - pd.Timedelta(minutes=60):
                        st["last_1m"] = (t_close - pd.Timedelta(minutes=1)).isoformat()
            except Exception as exc:
                print("helper error:", exc)
    if C.HELPER_5M and b1 is not None and len(b1) > 2 and (st.get("watch") or st.get("t5") or st.get("arm")):
        try:
            after = pd.Timestamp(st["last_1m"]) if st.get("last_1m") else last
            run_1m(st, b1, after, None, now)
            w = st.get("watch")
            print("helper:", f"trade {st['t5']['id']} open" if st.get("t5") else
                  f"watching {w['zl']:.1f}-{w['zh']:.1f} until {w['expires'][11:16]} UTC" if w else
                  f"armed {len(st['arm']['cands'])} H4 zone(s)" if st.get("arm") else "idle")
        except Exception as exc:
            print("helper error:", exc)
    save_state(st)


if __name__ == "__main__":
    main()
