# Day Trader — Project Notes

## ⚠ Not yet backtested — do not treat this as validated (2026-08-07)

This agent was built from the operator's own strategy spec (2026-08-07),
with two real corrections applied during the build (see "Position sizing —
the bug in the original spec" and "Long-only, no shorting" below) and one
addition beyond the spec (see "Forced end-of-day flatten"). **It has not
yet been backtested against real historical data.** Per this project's own
established standard (see crypto-scalper/CLAUDE.md's entire backtesting
history), a strategy that "looks right" in code is not the same claim as a
strategy that has edge — every one of the crypto scalper's five tested
configurations looked reasonable on paper and several looked great
in-sample before failing out-of-sample. Do not enable the live schedule
(`.github/workflows/daytrader.yml`'s `schedule:` trigger) or treat any
paper-trading result from this agent as meaningful until
`scripts/backtest.py` has actually been run — see "Backtesting" below for
how, and "Setup notes" for the credential this is blocked on right now.

---

A third, independent strategy in this repo — separate from both the
equities Trader (hourly, hold-for-days, research-driven) and the crypto
scalper (5-minute, 24/7, RSI/EMA momentum scalp on crypto). Not a fourth
separate Alpaca account, though — see the next section.

## Sharing the crypto scalper's Alpaca account (operator's choice, 2026-08-07)

Every other pair of bots in this repo gets full account isolation (see
crypto-scalper/CLAUDE.md's isolation section) — this one deliberately
doesn't, at the operator's explicit request, to avoid a third paper
account signup. `DAYTRADER_APCA_API_KEY_ID` / `_SECRET_KEY` / `_BASE_URL`
are populated in both `.github/workflows/daytrader.yml` and
`daytrader-backtest.yml` from `secrets.CRYPTO_APCA_*` — the exact same
Alpaca account the crypto scalper trades on, not a copy.

**Why this is safe here specifically, when it wouldn't be with the
equities Trader:** equities and crypto symbols can never collide —
crypto pairs are always `"BTC/USD"`-shaped, this trades bare tickers like
`"AAPL"`. There's no scenario where both strategies end up holding
"the same" position and fighting over whose it is, unlike sharing with
the equities Trader (whose watchlist genuinely overlaps this one).

**What still had to be fixed for this to be safe:**
- `research.get_deployed_notional()` and `get_day_trade_count()` both
  gained an optional `symbols` filter — `trade.py` and `research.py`'s
  CLI both pass this agent's own watchlist. Without it, the crypto
  scalper's buy orders would count against daytrader's daily notional
  cap (and vice versa), since both strategies' orders live in the same
  account order history.
- `agent/daytrader_agent.py` filters `research.get_positions()` down to
  watchlist symbols before doing anything else — otherwise the exit loop
  would try to run an equities EMA-crossover check against a symbol like
  `"BTC/USD"` (crash), and `open_count` (used for the max-positions cap)
  would include the crypto scalper's own open positions, wrongly
  shrinking how many equity positions daytrader thinks it can open.
- `_find_duplicate_order()` needed no change — it already matches on the
  exact symbol string, so a crypto order can never accidentally look like
  a duplicate of an equity one.

**What can't be fixed, and is an accepted tradeoff of sharing:** account
`equity` / `buying_power` (used for the 1% position-size cap) and the
daily-drawdown circuit breaker both reflect the **combined** account —
Alpaca has no concept of a sub-account or strategy-scoped equity. A bad
day for the crypto scalper shrinks daytrader's position sizing and could
trip its circuit breaker, and vice versa, even though neither strategy's
own performance caused it. If this turns out to matter in practice (e.g.
the circuit breaker trips from crypto volatility on a day daytrader did
nothing wrong), the fix is a real separate account at that point, not
more filtering — this is a genuine limit of sharing, not a bug to code
around.

## Tools available

- `scripts/research.py` — `account | positions | orders [STATUS] | clock | bars SYMBOL | signal SYMBOL | circuit-breaker | deployed | day-trades` (read-only). Talks to the shared Alpaca account via `DAYTRADER_APCA_API_KEY_ID` / `DAYTRADER_APCA_API_SECRET_KEY` / `DAYTRADER_APCA_BASE_URL` (same underlying account as `CRYPTO_APCA_*` — see above).
- `scripts/trade.py` — `status | order SYMBOL QTY SIDE [LIMIT_PRICE] | cancel`.
- `agent/daytrader_agent.py` — the run loop: checks the market clock, force-flattens near close, checks exits (stop-loss/take-profit/opposite-crossover) on open positions, then evaluates new-entry signals across the watchlist, places orders, journals every decision. Scheduled every 5 minutes during market hours.
- `scripts/backtest.py` — `[LOOKBACK_DAYS] [END_DAYS_AGO]` (aggregate report) or `detail SYMBOL [LOOKBACK_DAYS]` (trade-by-trade). See "Backtesting" below — **run this before trusting anything else in this file.**
- `watchlist.json` — liquid large-caps: NVDA, AAPL, MSFT, AMD, GOOGL, META, AMZN, TSLA, JPM, V, XOM, DIS. A subset of the equities Trader's own watchlist, chosen for tight spreads / heavy volume, which matters more for a tight-stop intraday strategy than for the equities bot's multi-day holds.

`scripts/trade.py`'s `place_order` pauses for a human y/N confirmation when
run interactively. Under `DAYTRADER_UNATTENDED=1` (scheduled/cloud runs) it
auto-approves instead, but only accepts limit orders (except the two
mandatory-exit cases below) and enforces every cap in "Risk controls"
below in code.

## Strategy — precisely

This is the operator's own spec (2026-08-07), reproduced exactly except
where noted below.

**Buy trigger (long only):** ALL of:
- Fast EMA(9) crosses above Slow EMA(21) on **5-minute** bars — a cross
  event (previous bar's fast <= slow, this bar's fast > slow), not a level
  check. Matches the "cross detector, not a level check" precision
  standard used throughout this project (e.g. the crypto scalper's EMA
  cross, the equities bot's stabilization-signal definition).
- RSI(14), same 5-minute bars, is between 40 and 65 (a trend-continuation
  filter — deliberately not an extreme reading, unlike the crypto
  scalper's RSI-oversold gate).

**Exit** (checked in this order — first match wins):
1. Forced end-of-day flatten (see below) — always takes priority.
2. Stop-loss: **-1.0%** unrealized P/L from entry (Alpaca's own
   `unrealized_plpc`), market order for a guaranteed fill.
3. Take-profit: **+2.0%** unrealized P/L from entry, limit order.
4. Opposite crossover: fast EMA(9) crosses back below slow EMA(21).

**No averaging in:** an open position on a symbol blocks any new signal
on that same symbol — one entry per symbol at a time, same rule as both
other bots.

**No leverage.** Cash buying power only.

## Long-only, no shorting (deliberate scope decision, 2026-08-07)

The operator's spec included a short-sell rule (EMA9<EMA21 cross + RSI
35-60 -> sell short). This was intentionally left out of the first build:
every other bot in this repo is deliberately cash-only, no margin — going
short structurally requires a margin account, which is a real philosophy
change, not a code detail, and wasn't a decision this build should make
silently on the operator's behalf. If the long side backtests well enough
to be worth the added complexity, adding shorting is a deliberate follow-up
(new account setting, new risk math for margin/assignment-style exposure),
not a default.

## Position sizing (corrected 2026-08-07, after a misread was caught)

The spec says: *"Risk a maximum of 1% of the total account balance per
trade."* The first build of this file misread that as a formal
risk-target formula — "if the 1% stop-loss is hit, the dollar loss should
equal 1% of equity" — which, combined with a 1% stop distance,
mathematically forces the position size to equal the **entire account**
(`risk_$ = position_notional × stop_distance_pct`, and when both
percentages are equal, `position_notional = equity`). That reading was
wrong, and the operator caught it: the plain sentence is a **position-size
cap** — don't put more than 1% of the account into any one trade — the
same way this project phrases every other per-trade cap (e.g. the
equities Trader's "5% of buying power per symbol"). It isn't tied to the
stop-loss distance at all.

Under the correct reading there's no contradiction: position size = 1% of
account balance, stop-loss = 1% below entry, so realized loss if stopped
out is ≈1% × 1% = 0.01% of the account — conservative, not aggressive.

**Implementation**: `research.compute_position_qty()` computes
`(PER_TRADE_PCT_CAP / 100) × account_balance / entry_price` directly — no
stop-distance term. `DAYTRADER_MAX_ORDER_NOTIONAL` (default $500) still
applies as a secondary dollar ceiling, since 1% of a large account could
still be a large single trade — whichever of the two is smaller actually
binds, logged per trade as `binding_constraint` in the `NEW_TRADE`
journal entry (`per_trade_pct_cap` or `max_order_notional`), so it's
answerable from the journal which one governed a given trade's size.

## Caps added beyond the original spec (2026-08-07)

The operator's spec listed position sizing, stop-loss, take-profit, and a
daily drawdown limit under "Risk Management (Critical)" — it didn't
specify a cap on how many symbols could have an open position
simultaneously. Even at a conservative 1% of account per trade, a run
where every symbol on a 12-symbol watchlist signals at once would still
open several positions in one go. Added, mirroring both other bots'
established pattern of "a well-reasoned trade shouldn't be able to talk
its way past a mechanical cap":
- `DAYTRADER_MAX_POSITIONS` (default 5).
- `DAYTRADER_MAX_ORDER_NOTIONAL` (default $500) — a secondary dollar
  ceiling alongside `DAYTRADER_PER_TRADE_PCT_CAP` (default 1%, see
  "Position sizing" above), in case 1% of a large account would still be
  a large single trade.
- `DAYTRADER_DAILY_NOTIONAL_CAP` (default $1,000), `DAYTRADER_MAX_ORDERS_PER_RUN` (default 5).
- Duplicate-order protection — same exact-match-today dedup against
  Alpaca's own order history as both other bots.

No sector/category cap was added (unlike the other two bots) — not asked
for, and the 12-symbol watchlist spans enough sectors that it wasn't
judged necessary for a first build. Revisit if the backtest or live
results show concentration risk in practice.

## Forced end-of-day flatten (added 2026-08-07)

Not in the original spec. A strategy called "day trading" that can carry
a position overnight isn't day trading — it's an intraday entry with
swing-trade gap risk nobody asked for (an overnight or weekend gap can
blow through the 1% stop-loss instantly, since a stop-loss only protects
against price *during* market hours). `agent/daytrader_agent.py` checks
Alpaca's own market clock (`research.get_market_clock()`) every run and,
within `DAYTRADER_EOD_FLATTEN_MINUTES` (default 10) of `next_close`,
force-closes every open position with a market order and skips new-entry
evaluation for that run, regardless of what the EMA/RSI signals say. The
backtest mirrors this exactly — `simulate_symbol()` force-exits any open
position on the last bar of each trading day in the historical data, so
backtest results don't quietly assume free overnight carry that live
trading won't actually get.

## Daily drawdown circuit breaker

Matches the operator's spec exactly: halts all new buy orders for the
rest of the day if account equity has dropped more than
`DAYTRADER_DAILY_DRAWDOWN_LIMIT_PCT` (default 3%) from today's opening
equity. Simpler than the other two bots' circuit breakers (no trailing
multi-day leg) since that's all that was asked for here. Existing
positions' stop-loss/take-profit/EOD-flatten exits are never blocked by
this — same convention as both other bots.

## Day-trade tracking (live-readiness, not yet enforced)

This strategy closes same-day essentially every time by design (that's
what "day trading" means) — on a real account under $25,000, this would
trip the Pattern Day Trader rule (max 3 day trades / rolling 5 business
days) almost immediately. Paper accounts have no PDT rule, so nothing
here blocks on it, but `research.get_day_trade_count()` tracks the
trailing count for visibility, same "live-readiness" pattern as the
equities Trader's own day-trade tracking. If this account is ever made
live with under $25,000 equity, this strategy as specified would need a
real redesign around that cap, not just a warning — worth remembering
before that conversation happens, not during it.

## Backtesting — run this before trusting any of the above

`scripts/backtest.py`, on demand via
`.github/workflows/daytrader-backtest.yml`. Replays the exact live signal
(EMA9 cross above EMA21 + RSI in [40,65]) and exit logic (-1% SL / +2% TP
/ opposite crossunder / forced EOD flatten) bar-by-bar over historical
5-minute bars, with no look-ahead (bar i's decision only uses
`bars[0..i]`) — same discipline as crypto-scalper/scripts/backtest.py,
including the `end_days_ago` out-of-sample knob (test a window that had
no part in tuning anything) and the "assume the worse outcome if SL and
TP are both touched in the same bar" convention.

**Scope — signal-only**, same caveat as the crypto scalper's backtest:
per-trade % return, not dollar P&L; does not simulate the daily notional
cap, max-positions cap, or cross-symbol allocation.

**As of 2026-08-07, this has not been run yet** — see "Setup notes" for
what it's blocked on. The honest, evidence-backed standard this project
holds itself to (see crypto-scalper/CLAUDE.md in full) is: don't trust a
signal because the code runs cleanly; trust it because a real historical
test, ideally across more than one window, says it has positive expected
value. Nothing in this file should be read as "this strategy works" until
that's actually been done.

## Setup notes

- No separate credentials needed — `DAYTRADER_APCA_*` env vars are
  populated from the existing `CRYPTO_APCA_*` GitHub secrets at the
  workflow level (see "Sharing the crypto scalper's Alpaca account"
  above). Equities **market-data** access on this account was confirmed
  2026-08-07 by actually fetching real AAPL 5-minute bars through it (342
  bars, realistic prices) via `scripts/backtest.py`. Equities **order
  placement** specifically has not been separately tested — Alpaca paper
  accounts support stock trading by default (crypto is additive, doesn't
  replace it), so this is expected to work, but the first real proof
  point will be whenever this agent actually places a live order, gated
  behind the backtest review either way.
- `.github/workflows/daytrader.yml` has **no `schedule:` trigger yet** —
  `workflow_dispatch` only, deliberately, so this can't start live-trading
  (even on paper) the moment credentials exist. Add the schedule back in
  (commented-out block already in the file) only after the backtest above
  has run and been reviewed.
- Other env vars (see root `.env.example`): `DAYTRADER_UNATTENDED`,
  `DAYTRADER_FAST_EMA`, `DAYTRADER_SLOW_EMA`, `DAYTRADER_RSI_PERIOD`,
  `DAYTRADER_RSI_ENTRY_MIN`, `DAYTRADER_RSI_ENTRY_MAX`,
  `DAYTRADER_STOP_LOSS_PCT`, `DAYTRADER_TAKE_PROFIT_PCT`,
  `DAYTRADER_DAILY_DRAWDOWN_LIMIT_PCT`,
  `DAYTRADER_MAX_ORDER_NOTIONAL`, `DAYTRADER_MAX_ORDERS_PER_RUN`,
  `DAYTRADER_DAILY_NOTIONAL_CAP`, `DAYTRADER_MAX_POSITIONS`,
  `DAYTRADER_PER_TRADE_PCT_CAP`, `DAYTRADER_EOD_FLATTEN_MINUTES`.

## Journal format

One file per day: `journal/YYYY-MM-DD.md`, separate from both other
bots' journals. Each run appends a `## Run YYYY-MM-DD HH:MM UTC` section
(handled automatically by `daytrader_agent.py`). Every decision — buy,
hold, or exit — gets logged with the specific number behind it, plus a
fenced JSON decision-envelope block per run, same audit-trail standard as
both other bots.

If this runs as a scheduled cloud routine / GitHub Actions job, commit
and push the journal file at the end of each run — each run starts from a
fresh clone.
