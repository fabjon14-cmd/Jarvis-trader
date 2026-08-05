# crypto-scalper/scripts/review.py
#
# Quantitative-only performance rollup — win rate, realized P&L, equity
# change, a BTC buy-and-hold benchmark, and trend across prior reviews.
# Deliberately NOT a full equities-style weekly review: this is a plain
# Python script run by a GitHub Actions cron, not an LLM cloud agent, so
# it can't do the equities review's qualitative "rule adherence" read of
# the journal — that needs a human or an LLM actually reading prose against
# CLAUDE.md's rules, which this script doesn't attempt. It computes numbers
# only, and says so plainly rather than faking a narrative section.

import os
import sys
import json
import glob
import importlib.util
from datetime import datetime, timedelta, timezone

_scripts_dir = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location("crypto_scalper_research_review", os.path.join(_scripts_dir, "research.py"))
research = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(research)

REVIEWS_DIR = os.path.join(_scripts_dir, "..", "reviews")


def _order_date(o):
    ts = o.get("submitted_at") or o.get("created_at")
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _order_ts(o):
    return o.get("filled_at") or o.get("submitted_at") or o.get("created_at") or ""


def closed_round_trips(orders, since_date):
    """Match each filled sell to the most recent prior filled buy of the
    same symbol. Safe as a simple pairing (not a FIFO queue) because the
    strategy enforces single-entry-per-pair — there's never more than one
    open buy per symbol to match against."""
    filled = [o for o in orders if o.get("status") == "filled" and "/" in (o.get("symbol") or "")]
    filled.sort(key=_order_ts)

    open_buys = {}
    trips = []
    for o in filled:
        symbol = o["symbol"]
        if o["side"] == "buy":
            open_buys[symbol] = o
        elif o["side"] == "sell" and symbol in open_buys:
            buy = open_buys.pop(symbol)
            buy_price = float(buy.get("filled_avg_price") or buy.get("limit_price") or 0)
            sell_price = float(o.get("filled_avg_price") or o.get("limit_price") or 0)
            qty = float(o.get("qty") or 0)
            trips.append({
                "symbol": symbol,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "qty": qty,
                "pnl": round((sell_price - buy_price) * qty, 2),
                "closed_at": _order_ts(o),
            })

    cutoff = since_date.isoformat()
    return [t for t in trips if t["closed_at"][:10] >= cutoff]


def _portfolio_pct_change(history):
    equity = [e for e in (history.get("equity") or []) if e is not None]
    if len(equity) < 2 or not equity[0]:
        return None
    return (equity[-1] - equity[0]) / equity[0] * 100


def build_review(period_days=7):
    since_date = (datetime.now(timezone.utc) - timedelta(days=period_days)).date()
    orders = research.get_orders(status="all", limit=500)
    period_orders = [o for o in orders if _order_date(o) and _order_date(o) >= since_date]

    placed = len(period_orders)
    filled = len([o for o in period_orders if o.get("status") == "filled"])
    rejected = len([o for o in period_orders if o.get("status") in ("rejected", "canceled", "cancelled", "expired")])

    trips = closed_round_trips(orders, since_date)
    wins = [t for t in trips if t["pnl"] > 0]
    win_rate = round(len(wins) / len(trips) * 100, 1) if trips else None
    realized_pnl = round(sum(t["pnl"] for t in trips), 2)
    largest_win = max(trips, key=lambda t: t["pnl"]) if trips else None
    largest_loss = min(trips, key=lambda t: t["pnl"]) if trips else None

    try:
        history = research.get_portfolio_history(period=f"{period_days}D", timeframe="1D")
        equity_change_pct = _portfolio_pct_change(history)
    except Exception:
        equity_change_pct = None

    try:
        btc_bars = research.get_crypto_bars("BTC/USD", timeframe="1Day", limit=period_days + 2)
        btc_change_pct = round((btc_bars[-1]["c"] - btc_bars[0]["c"]) / btc_bars[0]["c"] * 100, 2) if len(btc_bars) >= 2 else None
    except Exception:
        btc_change_pct = None

    return {
        "period_days": period_days,
        "since": since_date.isoformat(),
        "orders": {"placed": placed, "filled": filled, "rejected_or_canceled": rejected},
        "round_trips": {
            "count": len(trips),
            "win_rate_pct": win_rate,
            "realized_pnl": realized_pnl,
            "largest_win": largest_win,
            "largest_loss": largest_loss,
            "trips": trips,
        },
        "equity_change_pct": round(equity_change_pct, 2) if equity_change_pct is not None else None,
        "btc_buy_and_hold_change_pct": btc_change_pct,
        "outperformed_btc_pct_points": (
            round(equity_change_pct - btc_change_pct, 2)
            if equity_change_pct is not None and btc_change_pct is not None else None
        ),
    }


def _prior_reviews():
    paths = sorted(glob.glob(os.path.join(REVIEWS_DIR, "*.md")))
    prior = []
    for path in paths:
        with open(path) as f:
            content = f.read()
        start = content.find("```json")
        end = content.find("```", start + 7)
        if start == -1 or end == -1:
            continue
        try:
            data = json.loads(content[start + 7:end])
        except json.JSONDecodeError:
            continue
        prior.append({"file": os.path.basename(path), "data": data})
    return prior


def render_markdown(review):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# Crypto Scalper — {review['period_days']}-day review — {today}", ""]

    lines.append("## Performance")
    o = review["orders"]
    lines.append(f"- Orders placed / filled / rejected-or-canceled: {o['placed']} / {o['filled']} / {o['rejected_or_canceled']}")
    rt = review["round_trips"]
    if rt["count"]:
        lines.append(f"- Closed round-trips: {rt['count']}, win rate {rt['win_rate_pct']}%")
        lines.append(f"- Realized P&L this period: ${rt['realized_pnl']:,.2f}")
        if rt["largest_win"]:
            w = rt["largest_win"]
            lines.append(f"- Largest win: {w['symbol']} ${w['pnl']:,.2f}")
        if rt["largest_loss"]:
            l = rt["largest_loss"]
            lines.append(f"- Largest loss: {l['symbol']} ${l['pnl']:,.2f}")
    else:
        lines.append("- No closed round-trips this period (N/A win rate / realized P&L).")
    lines.append(f"- Account equity change over period: {review['equity_change_pct']}%" if review["equity_change_pct"] is not None else "- Account equity change over period: unavailable")

    lines.append("")
    lines.append("## Benchmark comparison (BTC buy-and-hold)")
    if review["btc_buy_and_hold_change_pct"] is not None:
        lines.append(f"- BTC/USD change over the same period: {review['btc_buy_and_hold_change_pct']}%")
        if review["outperformed_btc_pct_points"] is not None:
            verdict = "outperformed" if review["outperformed_btc_pct_points"] > 0 else ("underperformed" if review["outperformed_btc_pct_points"] < 0 else "matched")
            lines.append(f"- Account {verdict} BTC buy-and-hold by {abs(review['outperformed_btc_pct_points'])} percentage points.")
    else:
        lines.append("- BTC benchmark unavailable this run.")

    lines.append("")
    lines.append("## Trend across reviews")
    prior = _prior_reviews()
    if not prior:
        lines.append("- No prior reviews on this branch yet — this is the first.")
    else:
        lines.append(f"- {len(prior)} prior review(s) on file.")
        win_rates = [p["data"].get("round_trips", {}).get("win_rate_pct") for p in prior if p["data"].get("round_trips", {}).get("win_rate_pct") is not None]
        if win_rates and rt["win_rate_pct"] is not None:
            trend = "up" if rt["win_rate_pct"] > win_rates[-1] else ("down" if rt["win_rate_pct"] < win_rates[-1] else "flat")
            lines.append(f"- Win rate vs. most recent prior review: {trend} ({win_rates[-1]}% -> {rt['win_rate_pct']}%).")

    lines.append("")
    lines.append("## Notes")
    lines.append(
        "This review is computed directly from Alpaca order/portfolio history — "
        "no qualitative rule-adherence check is performed here (unlike the equities "
        "bot's weekly review), since this runs as a plain script, not an LLM agent "
        "reading the journal against CLAUDE.md. Read crypto-scalper/journal/ directly "
        "for the per-decision reasoning behind any trade referenced above."
    )

    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(review, indent=2))
    lines.append("```")

    return "\n".join(lines) + "\n"


def write_review(period_days=7):
    os.makedirs(REVIEWS_DIR, exist_ok=True)
    review = build_review(period_days)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(REVIEWS_DIR, f"{today}.md")
    with open(path, "w") as f:
        f.write(render_markdown(review))
    return path


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "write"
    period = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    if action == "write":
        path = write_review(period)
        print(f"Wrote {path}")
    elif action == "show":
        print(json.dumps(build_review(period), indent=2))
    else:
        print("Usage: review.py write [PERIOD_DAYS] | show [PERIOD_DAYS]")
