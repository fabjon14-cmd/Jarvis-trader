# daytrader/scripts/hourly_digest.py
#
# Hourly activity digest — emails a summary of buy/sell orders from the
# last 60 minutes, but ONLY if something actually happened (no "no trades
# this hour" heartbeat — same convention as the crypto scalper's own
# hourly_digest.py). Reads straight from Alpaca's own order history, not
# the journal file.
#
# Filtered to WATCHLIST_SYMBOLS, not "any order on this account" — the
# account is shared with the crypto scalper (see CLAUDE.md "Sharing the
# crypto scalper's Alpaca account"), so unfiltered order history would
# include crypto activity that has nothing to do with this bot.

import os
import sys
import json
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

import research
from trade import WATCHLIST_SYMBOLS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import notify

LOOKBACK_MINUTES = int(os.getenv("DAYTRADER_DIGEST_LOOKBACK_MINUTES", "60"))


def _order_dt(o):
    ts = o.get("filled_at") or o.get("submitted_at") or o.get("created_at")
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_digest():
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    orders = research.get_orders(status="all", limit=200)

    recent = []
    for o in orders:
        if o.get("symbol") not in WATCHLIST_SYMBOLS:
            continue
        if o.get("status") in ("canceled", "cancelled", "expired", "rejected"):
            continue  # a rejection means it did NOT buy or sell — that's not the "did it trade" signal this digest is for
        dt = _order_dt(o)
        if not dt or dt < cutoff:
            continue
        recent.append((dt, o))

    if not recent:
        return None

    recent.sort(key=lambda pair: pair[0])
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Day Trader — activity in the last {LOOKBACK_MINUTES} minutes (as of {now_str})", ""]
    for dt, o in recent:
        side = (o.get("side") or "").upper()
        symbol = o.get("symbol")
        qty = o.get("qty")
        price = o.get("filled_avg_price") or o.get("limit_price") or "market"
        order_type = o.get("type")
        order_class = o.get("order_class")
        status = o.get("status")
        lines.append(f"- {dt.strftime('%H:%M')} UTC  {side} {qty} {symbol} @ {price} ({order_type}/{order_class}, status={status})")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if "--test" in sys.argv:
        # One-off, bypasses the "only if something happened" gate (and
        # doesn't even touch Alpaca) — for confirming RESEND_API_KEY/
        # REPORT_TO_EMAIL actually work, not for scheduled use.
        subject = f"Day Trader — test email — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        body = "This is a one-off test to confirm RESEND_API_KEY/REPORT_TO_EMAIL are correctly wired up for the daytrader hourly digest. No real trade activity is referenced here."
        result = notify.send_email(subject, body)
        print(json.dumps(result))
        sys.exit(0)

    digest = build_digest()
    if digest is None:
        print(f"No daytrader order activity in the last {LOOKBACK_MINUTES} minutes — no email sent.")
        sys.exit(0)

    print(digest)
    subject = f"Day Trader — hourly activity — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    result = notify.send_email(subject, digest)
    print(json.dumps(result))
