# Gold Signals (XAUUSD)

Email alerts from a Python port of the **GoldSignalsPro** TradingView indicator.
**Signals only. It never places orders.**

## What it does
- Every 5 minutes (GitHub Actions) it checks whether a new 30m candle has closed.
  If so it fetches XAU/USD candles, replays the strategy and emails anything new:
  - **New signal**: entry zone, SL, TP1/TP2, size, a trend check on Daily/H4/1H/30m, the reasons and a score breakdown, what would cancel the trade, and the plan.
  - **LOCK**: move the stop into profit. **TP1 HIT**: close half and move the stop. **CLOSED**: the result in R plus a 7-day tally.
  - **Daily status** once each weekday after 07:00 UK: trend, nearest zones, open trade, last 24h.
- **5m entry helper**: when a 30m candle trades into an H4 zone in the H4 trend direction (and no trade is open), you get a **WATCH** email.
  For the next 2 hours the bot checks every 5m candle. A rejection wick or reversal close inside the zone triggers a **5m ENTRY** email,
  with a tighter stop just past that candle's wick. That trade then gets its own LOCK / TP1 / CLOSED emails. The same zone isn't re-watched within 4 hours.
  5m data is only fetched while watching or while a 5m trade is open. Turn it off with `HELPER_5M = False` in `config.py`.
- Rules: 30m confirmed candle close at a zone, **H4 zone + 30m trigger** (the zone must contain an H4 swing or yesterday's/last week's high-low, and the 30m structure must agree), falling-knife filter, profit lock 50%/15%, partial TP 50% at 1R, no A-grade sells, liquidity-sweep bonus.

## Setup (one time)
Add these repository secrets (Settings → Secrets and variables → Actions):
- `TWELVEDATA_API_KEY`: free key from twelvedata.com for real XAU/USD, plus EUR/USD and XAG/USD for the dollar/silver check.
  If it's missing, the bot falls back to Alpaca **PAXG/USD** using the existing `CRYPTO_APCA_*` keys. That's a gold token a few dollars off spot, with no dollar/silver check.
- `RESEND_API_KEY`, `REPORT_TO_EMAIL`: already used by the other bots.

## Files
- `config.py`: all settings (zone sizes, filters, lock, account size and risk % for the lot size).
- `engine.py`: the strategy. 1h/4h/daily/weekly candles are built from 30m candles, using completed bars only.
- `data.py`: Twelve Data / Alpaca PAXG fetch.
- `emailer.py`: email layouts and the Resend sender.
- `run.py`: the live runner (`--test` sends sample emails).
- `helper5m.py`: the 5m watch / entry / trade management.
- `backtest.py`: `python3 gold-signals/backtest.py [file.csv] --compare --helper5m [--5m=file5m.csv] --list [--email]`.
- `state.json`: last processed candle plus the keys of emails already sent (committed by the workflow).

## Workflows
- **Gold Signals** (`.github/workflows/gold-signals.yml`): the 5-minute schedule. Run it manually with `test = true` to get sample emails.
- **Gold Signals Backtest**: manual. Emails backtest results on the latest ~3,000 30m candles, plus the 5m helper against the 30m signals over the last ~5,000 5m candles (~17 trading days).

## Differences from TradingView
No volume-profile levels (most XAU/USD feeds carry no volume), daily/4h candles roll at 17:00 New York,
and the dollar check uses EUR/USD inverted instead of DXY. Signals will be close to, but not always identical to, the chart.
