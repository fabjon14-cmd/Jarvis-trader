# crypto-scalper/scripts/hourly_digest.py
#
# Hourly activity digest — emails a summary of buy/sell orders from the
# last 60 minutes, but ONLY if something actually happened (no "no trades
# this hour" heartbeat — see crypto-scalper/CLAUDE.md "Hourly digest" for
# why). Reads straight from Alpaca's own order history, not the journal
# file, same "source of truth over derived text" preference as the rest
# of this project.

import os
import sys
import json
import importlib.util
from datetime import datetime, timedelta, timezone

_scripts_dir = os.path.dirname(__file__)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


research = _load("crypto_scalper_research_digest", os.path.join(_scripts_dir, "research.py"))
notify = _load("shared_notify_digest", os.path.join(_scripts_dir, "..", "..", "scripts", "notify.py"))

LOOKBACK_MINUTES = int(os.getenv("CRYPTO_DIGEST_LOOKBACK_MINUTES", "60"))


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
        symbol = o.get("symbol") or ""
        if "/" not in symbol:
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
    lines = [f"Crypto Scalper — activity in the last {LOOKBACK_MINUTES} minutes (as of {now_str})", ""]
    for dt, o in recent:
        side = (o.get("side") or "").upper()
        symbol = o.get("symbol")
        qty = o.get("qty")
        price = o.get("filled_avg_price") or o.get("limit_price") or "market"
        order_type = o.get("type")
        status = o.get("status")
        lines.append(f"- {dt.strftime('%H:%M')} UTC  {side} {qty} {symbol} @ {price} ({order_type}, status={status})")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if "--test" in sys.argv:
        # One-off, bypasses the "only if something happened" gate (and
        # doesn't even touch Alpaca) — for confirming RESEND_API_KEY/
        # REPORT_TO_EMAIL actually work, not for scheduled use.
        subject = f"Crypto Scalper — test email — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        body = "This is a one-off test to confirm RESEND_API_KEY/REPORT_TO_EMAIL are correctly wired up for the crypto-scalper hourly digest. No real trade activity is referenced here."
        result = notify.send_email(subject, body)
        print(json.dumps(result))
        sys.exit(0)

    digest = build_digest()
    if digest is None:
        print(f"No crypto scalper order activity in the last {LOOKBACK_MINUTES} minutes — no email sent.")
        sys.exit(0)

    print(digest)
    subject = f"Crypto Scalper — hourly activity — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    result = notify.send_email(subject, digest)
    print(json.dumps(result))
