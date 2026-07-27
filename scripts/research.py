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
    else:
        print(json.dumps(get_account()))
