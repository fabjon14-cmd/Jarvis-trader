# scripts/research.py

import os
import requests
import json
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")

REQUEST_TIMEOUT = 15


def _headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def get_bars(symbol, timeframe="1Day", limit=60):
    """Fetch the most recent `limit` historical price bars for a symbol.

    Alpaca returns null bars if start/end are omitted, so we always send an
    explicit range wide enough to cover `limit` bars (accounting for weekends/
    holidays). Within that range Alpaca returns oldest-first by default, so a
    wide range plus `limit` truncates from the oldest end, not the most recent
    one — we request sort=desc (most recent first) and then reverse the
    result back to chronological order so callers get the most recent `limit`
    bars, oldest to newest.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(limit * 2, 30))
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "desc",
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
    }
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get("bars"):
        data["bars"] = list(reversed(data["bars"]))
    return data


def get_account():
    """Get current portfolio status."""
    url = f"{BASE_URL}/v2/account"
    response = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_positions():
    """Get all open positions."""
    url = f"{BASE_URL}/v2/positions"
    response = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_news(symbol):
    """Get recent news for a symbol."""
    url = "https://data.alpaca.markets/v1beta1/news"
    params = {
        "symbols": symbol,
        "limit": 5,
        "sort": "desc"
    }
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_orders(status="all", limit=100):
    """Get historical orders (filled, canceled, expired, etc.), most recent first."""
    url = f"{BASE_URL}/v2/orders"
    params = {"status": status, "limit": limit, "direction": "desc"}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_portfolio_history(period="1W", timeframe="1D"):
    """Get account equity history over a period, e.g. period='1W' for the past week."""
    url = f"{BASE_URL}/v2/account/portfolio/history"
    params = {"period": period, "timeframe": timeframe}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_earnings_date(symbol):
    """Get a symbol's next upcoming earnings date via Finnhub, and whether it
    falls within the next 48 hours (for the earnings-blackout trading rule).
    Returns next_earnings_date=None if nothing is scheduled in the lookahead window.

    Finnhub only gives date-level granularity, not a timestamp, so this compares
    whole calendar days rather than hours: computing "hours until midnight UTC of
    that date" would make today's own earnings look like it already passed the
    moment any time has elapsed since midnight, which is exactly backwards — a
    same-day report is the most urgent case for the blackout rule, not one to
    exclude. within_48h is true for a report dated today, tomorrow, or the day
    after (a 3-calendar-day window standing in for "48 hours" given the
    date-only precision).
    """
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=120)
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {
        "from": today.strftime("%Y-%m-%d"),
        "to": horizon.strftime("%Y-%m-%d"),
        "symbol": symbol,
        "token": FINNHUB_KEY,
    }
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    calendar = response.json().get("earningsCalendar") or []

    if not calendar:
        return {"symbol": symbol, "next_earnings_date": None, "days_until": None, "within_48h": False}

    calendar.sort(key=lambda e: e.get("date", ""))
    next_date_str = calendar[0]["date"]
    next_date = datetime.strptime(next_date_str, "%Y-%m-%d").date()
    days_until = (next_date - today).days

    return {
        "symbol": symbol,
        "next_earnings_date": next_date_str,
        "days_until": days_until,
        "within_48h": 0 <= days_until <= 2,
    }


def get_movers(top=10):
    """Get today's top gaining and losing stocks market-wide (not limited to
    the watchlist) via Alpaca's screener API — used to find candidates for
    the watchlist, not to trade them directly."""
    url = "https://data.alpaca.markets/v1beta1/screener/stocks/movers"
    params = {"top": top}
    response = requests.get(url, headers=_headers(), params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _portfolio_pct_change(history):
    equity = [e for e in (history.get("equity") or []) if e is not None]
    if len(equity) < 2 or not equity[0]:
        return None
    return (equity[-1] - equity[0]) / equity[0] * 100


def get_circuit_breaker_status():
    """Portfolio-level circuit breaker: halts new buys (not sells/trims) if
    equity has dropped more than 4% intraday or 8% over the trailing 5
    trading days. Computed here rather than left to inference, same reason
    as the earnings check — a precise threshold needs a precise number."""
    intraday = get_portfolio_history(period="1D", timeframe="15Min")
    five_day = get_portfolio_history(period="5D", timeframe="1D")

    intraday_pct = _portfolio_pct_change(intraday)
    five_day_pct = _portfolio_pct_change(five_day)

    reasons = []
    if intraday_pct is not None and intraday_pct <= -4:
        reasons.append(f"intraday drawdown {intraday_pct:.2f}% (limit -4%)")
    if five_day_pct is not None and five_day_pct <= -8:
        reasons.append(f"5-trading-day drawdown {five_day_pct:.2f}% (limit -8%)")

    return {
        "halted": bool(reasons),
        "intraday_pct": round(intraday_pct, 2) if intraday_pct is not None else None,
        "five_day_pct": round(five_day_pct, 2) if five_day_pct is not None else None,
        "reasons": reasons,
    }


def get_stop_loss_flags():
    """Per-position mechanical stop-loss, independent of the research-based
    selling rule — fires on price alone. -15% from cost basis: trim to half.
    -25%: close entirely. Uses Alpaca's own unrealized_plpc, not a derived
    calculation, so there's no room for rounding disagreements."""
    flags = []
    for p in get_positions():
        plpc = float(p.get("unrealized_plpc", 0)) * 100
        action = "close" if plpc <= -25 else ("trim_half" if plpc <= -15 else None)
        if action:
            flags.append({
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty", 0)),
                "unrealized_plpc": round(plpc, 2),
                "action": action,
            })
    return flags


_SECTOR_MAP = None


def _load_sectors():
    global _SECTOR_MAP
    if _SECTOR_MAP is None:
        path = os.path.join(os.path.dirname(__file__), "..", "sectors.json")
        with open(path) as f:
            _SECTOR_MAP = json.load(f)
    return _SECTOR_MAP


def get_sector(symbol):
    return _load_sectors().get(symbol, "Unknown")


def check_sector_cap(symbol):
    """Would opening a NEW position in `symbol` push its sector above 2 of
    the open positions? Only relevant for symbols not already held — adding
    to an existing position doesn't change the sector count."""
    sector = get_sector(symbol)
    positions = get_positions()
    held_symbols = {p["symbol"] for p in positions}
    if symbol in held_symbols:
        return {"symbol": symbol, "sector": sector, "blocked": False, "reason": None}
    same_sector_count = sum(1 for p in positions if get_sector(p["symbol"]) == sector)
    blocked = same_sector_count >= 2
    return {
        "symbol": symbol,
        "sector": sector,
        "current_same_sector_positions": same_sector_count,
        "blocked": blocked,
        "reason": f"already {same_sector_count} open position(s) in {sector}" if blocked else None,
    }


def get_deployed_notional():
    """Sum of BUY order notional today and over the trailing 7 calendar days,
    for the daily/weekly aggregate deployment cap. Computed from Alpaca's own
    order history (accepted/filled only, not rejected/canceled/expired) —
    the real source of truth for what was actually committed, rather than a
    separately-maintained counter that could drift out of sync across runs."""
    today = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=7)

    daily_total = 0.0
    weekly_total = 0.0
    for o in get_orders(status="all", limit=500):
        if o.get("side") != "buy" or o.get("status") in ("canceled", "cancelled", "rejected", "expired"):
            continue
        submitted = o.get("submitted_at") or o.get("created_at")
        if not submitted:
            continue
        try:
            order_date = datetime.strptime(submitted[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        qty = float(o.get("qty") or 0)
        price = float(o.get("limit_price") or o.get("filled_avg_price") or 0)
        notional = qty * price
        if order_date == today:
            daily_total += notional
        if order_date >= week_start:
            weekly_total += notional

    return {"daily_deployed": round(daily_total, 2), "weekly_deployed": round(weekly_total, 2)}


def get_day_trade_count(lookback_business_days=5):
    """Count same-symbol same-day round trips (buy+sell or sell+buy on the
    same calendar day) over the trailing N business days, for PDT
    live-readiness tracking. Paper accounts don't enforce PDT, so this is
    informational only — not a gate — but it means a pattern is visible in
    the journal before it would ever matter on a live account."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback_business_days * 2)
    by_symbol_day = {}
    for o in get_orders(status="all", limit=500):
        if o.get("status") in ("canceled", "cancelled", "rejected", "expired"):
            continue
        submitted = o.get("submitted_at") or o.get("created_at")
        symbol = o.get("symbol")
        side = o.get("side")
        if not submitted or not symbol or not side:
            continue
        try:
            order_date = datetime.strptime(submitted[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if order_date < cutoff:
            continue
        key = (symbol, order_date)
        by_symbol_day.setdefault(key, set()).add(side)

    day_trades = [
        {"symbol": symbol, "date": date.isoformat()}
        for (symbol, date), sides in by_symbol_day.items()
        if {"buy", "sell"}.issubset(sides)
    ]
    return {"count": len(day_trades), "trades": day_trades}


def would_be_day_trade(symbol, side):
    """Would placing `side` for `symbol` right now complete a same-day round
    trip (the opposite side already happened today for this symbol)?"""
    today = datetime.now(timezone.utc).date()
    opposite = "sell" if side == "buy" else "buy"
    for o in get_orders(status="all", limit=200):
        if o.get("symbol") != symbol or o.get("side") != opposite:
            continue
        if o.get("status") in ("canceled", "cancelled", "rejected", "expired"):
            continue
        submitted = o.get("submitted_at") or o.get("created_at")
        if not submitted:
            continue
        try:
            order_date = datetime.strptime(submitted[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if order_date == today:
            return True
    return False


if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "account"
    symbol = sys.argv[2] if len(sys.argv) > 2 else None

    if action == "bars" and symbol:
        print(json.dumps(get_bars(symbol)))
    elif action == "news" and symbol:
        print(json.dumps(get_news(symbol)))
    elif action == "positions":
        print(json.dumps(get_positions()))
    elif action == "orders":
        status = symbol or "all"
        print(json.dumps(get_orders(status=status)))
    elif action == "portfolio":
        period = symbol or "1W"
        print(json.dumps(get_portfolio_history(period=period)))
    elif action == "earnings" and symbol:
        print(json.dumps(get_earnings_date(symbol)))
    elif action == "movers":
        top = int(symbol) if symbol else 10
        print(json.dumps(get_movers(top=top)))
    elif action == "circuit-breaker":
        print(json.dumps(get_circuit_breaker_status()))
    elif action == "stop-loss":
        print(json.dumps(get_stop_loss_flags()))
    elif action == "sector" and symbol:
        print(json.dumps(check_sector_cap(symbol)))
    elif action == "deployed":
        print(json.dumps(get_deployed_notional()))
    elif action == "day-trades":
        print(json.dumps(get_day_trade_count()))
    else:
        print(json.dumps(get_account()))
