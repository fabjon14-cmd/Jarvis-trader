# crypto-scalper/scripts/list_assets.py
#
# One-off diagnostic: lists every crypto pair actually tradable on this
# Alpaca account, so watchlist.json only ever contains real, tradable
# symbols instead of guessed ones. Not part of the scheduled pipeline.

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

ALPACA_KEY = os.getenv("CRYPTO_APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("CRYPTO_APCA_API_SECRET_KEY")
BASE_URL = os.getenv("CRYPTO_APCA_BASE_URL", "https://paper-api.alpaca.markets")


def main():
    url = f"{BASE_URL}/v2/assets"
    params = {"asset_class": "crypto", "status": "active"}
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    response = requests.get(url, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    assets = response.json()

    tradable = [a for a in assets if a.get("tradable")]
    tradable.sort(key=lambda a: a["symbol"])
    print(f"Total active crypto assets: {len(assets)}, tradable: {len(tradable)}")
    for a in tradable:
        print(f"{a['symbol']}\tfractionable={a.get('fractionable')}\tmarginable={a.get('marginable')}")


if __name__ == "__main__":
    sys.exit(main())
