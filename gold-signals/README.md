# Gold Signals (XAUUSD)

Email alerts from a Python port of the **GoldSignalsPro** TradingView indicator.
**Signals only. It never places orders.**

## What it does
- Every 5 minutes (GitHub Actions) it checks whether a new 30m candle has closed.
  If so it fetches XAU/USD candles, replays the strategy and emails anything new:
  - **New signal**: entry zone, SL, TP1/TP2, size, a trend check on Daily/H4/1H/30m, the reasons and a score breakdown, what would cancel the trade, and the plan.
  - **LOCK**: move the stop into profit. **TP1 HIT**: close half and move the stop. **CLOSED**: the result in R plus a 7-day tally.
  - **Daily status** once each weekday after 07:00 UK: trend, nearest zones, open trade, last 24h.
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
- `backtest.py`: `python3 gold-signals/backtest.py [file.csv] --compare --list [--email]`.
- `state.json`: last processed candle plus the keys of emails already sent (committed by the workflow).

## Workflows
- **Gold Signals** (`.github/workflows/gold-signals.yml`): the 5-minute schedule. Run it manually with `test = true` to get sample emails.
- **Gold Signals Backtest**: manual. Emails backtest results on the latest ~3,000 candles.

## Differences from TradingView
No volume-profile levels (most XAU/USD feeds carry no volume), daily/4h candles roll at 17:00 New York,
and the dollar check uses EUR/USD inverted instead of DXY. Signals will be close to, but not always identical to, the chart.
