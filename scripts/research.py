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
    else:
        print(json.dumps(get_account()))
