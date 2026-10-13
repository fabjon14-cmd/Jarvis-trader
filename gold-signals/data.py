"""Price data for the gold bot.

Primary: Twelve Data (free key, real XAU/USD spot) -> TWELVEDATA_API_KEY.
Also pulls EUR/USD (dollar direction, inverted) and XAG/USD (silver) for the cross-check.
Fallback: Alpaca crypto PAXG/USD (gold-backed token, trades 24/7, a few dollars off spot)
using the CRYPTO_APCA_* (or APCA_*) keys the repo already has. No cross-check on the fallback.
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

TIMEOUT = 25


def _twelve(symbol, key, outputsize=3000, interval="30min"):
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": symbol, "interval": interval, "outputsize": outputsize,
                "timezone": "UTC", "order": "ASC", "apikey": key},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    js = r.json()
    if js.get("status") != "ok":
        raise RuntimeError(f"Twelve Data {symbol}: {js.get('message', js)}")
    df = pd.DataFrame(js["values"])
    df.index = pd.to_datetime(df.pop("datetime"), utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_index()


def _alpaca_paxg(days=75):
    key = os.getenv("CRYPTO_APCA_API_KEY_ID") or os.getenv("APCA_API_KEY_ID")
    sec = os.getenv("CRYPTO_APCA_API_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    headers = {"APCA-API-KEY-ID": key or "", "APCA-API-SECRET-KEY": sec or ""}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"symbols": "PAXG/USD", "timeframe": "30Min", "start": start, "limit": 10000, "sort": "asc"}
    rows, token = [], None
    while True:
        if token:
            params["page_token"] = token
        r = requests.get("https://data.alpaca.markets/v1beta3/crypto/us/bars", params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        rows += js.get("bars", {}).get("PAXG/USD", [])
        token = js.get("next_page_token")
        if not token:
            break
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("t"), utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def drop_forming(df, now=None, minutes=30):
    """Remove the candle that is still forming."""
    now = now or datetime.now(timezone.utc)
    if len(df) and df.index[-1] + pd.Timedelta(minutes=minutes) > pd.Timestamp(now):
        return df.iloc[:-1]
    return df


def get_data(now=None):
    """Returns (gold_30m_df, eurusd_closes or None, xagusd_closes or None, source_label)."""
    key = os.getenv("TWELVEDATA_API_KEY")
    if key:
        gold = _twelve("XAU/USD", key)
        try:
            eur = _twelve("EUR/USD", key, outputsize=600)["close"]
        except Exception:
            eur = None
        try:
            xag = _twelve("XAG/USD", key, outputsize=600)["close"]
        except Exception:
            xag = None
        src = "Twelve Data XAU/USD"
    else:
        gold = _alpaca_paxg()
        eur = xag = None
        src = "Alpaca PAXG/USD (gold token, a few $ off spot)"
    gold = drop_forming(gold, now)
    if "volume" in gold and gold["volume"].fillna(0).sum() == 0:
        gold = gold.drop(columns=["volume"])
    return gold, eur, xag, src


def get_5m(n=120, now=None):
    """Recent closed 5m candles (for the entry helper)."""
    return get_tf("5m", n, now)


def get_tf(tf, n=120, now=None):
    """Recent closed 1m or 5m candles (for the entry helper)."""
    mins = {"1m": 1, "5m": 5}[tf]
    key = os.getenv("TWELVEDATA_API_KEY")
    if key:
        df = _twelve("XAU/USD", key, outputsize=n, interval=f"{mins}min")
    else:
        df = _alpaca_paxg_tf(f"{mins}Min", days=1 if mins == 1 else 2)
    return drop_forming(df, now, minutes=mins)


def _alpaca_paxg_tf(tf, days):
    key = os.getenv("CRYPTO_APCA_API_KEY_ID") or os.getenv("APCA_API_KEY_ID")
    sec = os.getenv("CRYPTO_APCA_API_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    headers = {"APCA-API-KEY-ID": key or "", "APCA-API-SECRET-KEY": sec or ""}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"symbols": "PAXG/USD", "timeframe": tf, "start": start, "limit": 10000, "sort": "asc"}
    r = requests.get("https://data.alpaca.markets/v1beta3/crypto/us/bars", params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    df = pd.DataFrame(r.json().get("bars", {}).get("PAXG/USD", []))
    df.index = pd.to_datetime(df.pop("t"), utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[["open", "high", "low", "close", "volume"]].sort_index()
