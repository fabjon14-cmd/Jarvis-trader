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

A third, independent agent in this repo — separate from both the equities
Trader (hourly, hold-for-days, research-driven) and the crypto scalper
(5-minute, 24/7, RSI/EMA momentum scalp on crypto). Own Alpaca **paper
trading** account (own API keys, `DAYTRADER_APCA_*` env vars, own equity
curve), a third distinct strategy, its own risk budget, its own journal —
full blast-radius isolation from both other bots, same reasoning as
crypto-scalper/CLAUDE.md's isolation section.

## Tools available

- `scripts/research.py` — `account | positions | orders [STATUS] | clock | bars SYMBOL | signal SYMBOL | circuit-breaker | deployed | day-trades` (read-only). Talks to this agent's own Alpaca account via `DAYTRADER_APCA_API_KEY_ID` / `DAYTRADER_APCA_API_SECRET_KEY` / `DAYTRADER_APCA_BASE_URL`.
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

## Position sizing — the bug in the original spec (fixed 2026-08-07)

The original spec said: risk 1% of account balance per trade, with a
stop-loss 1.0% below entry. Those two numbers combined mathematically
force betting the **entire account** on every single trade:

```
risk_$ = position_notional × stop_distance_pct
0.01 × equity = position_notional × 0.01
position_notional = equity
```

Whenever the risk-% and stop-distance-% are numerically equal, position
size always comes out to 100% of equity — regardless of what the "1%"
was intended to mean. This is arithmetic, not a judgment call, and it's
worth writing down plainly so it isn't silently reintroduced later.

**The fix**, applied in `research.compute_risk_based_qty()` /
`agent/daytrader_agent.py`: exactly the same pattern the crypto scalper
already uses for the identical problem (see crypto-scalper/CLAUDE.md
"Risk-based sizing (informational ceiling, not the operative cap)") — the
risk-target quantity is computed and logged, but the *operative* size is
`min(risk_based_qty, notional_cap_qty)`, where `notional_cap_qty` comes
from `DAYTRADER_MAX_ORDER_NOTIONAL` (default $500) and
`DAYTRADER_PER_TRADE_PCT_CAP` (default 5% of buying power, matching the
equities Trader's own per-symbol cap). In practice the notional cap binds,
not the 1% risk target — actual risk per trade works out to roughly
`stop_loss_pct × per_trade_pct_cap` ≈ 0.05% of equity, not 1%. Every
`NEW_TRADE` journal entry logs both `risk_based_qty` and
`notional_cap_qty` with `binding_constraint`, so this is answerable
directly from the journal, not hidden.

## Caps added beyond the original spec (2026-08-07)

The operator's spec listed position sizing, stop-loss, take-profit, and a
daily drawdown limit under "Risk Management (Critical)" — it didn't
specify a cap on how many symbols could have an open position
simultaneously. Given the position-sizing fix above still allows up to
5% of buying power per trade, an unbounded number of simultaneous
positions across a 12-symbol watchlist could still deploy the account
faster than intended. Added, mirroring both other bots' established
pattern of "a well-reasoned trade shouldn't be able to talk its way past
a mechanical cap":
- `DAYTRADER_MAX_POSITIONS` (default 5).
- `DAYTRADER_MAX_ORDER_NOTIONAL` (default $500) / `DAYTRADER_PER_TRADE_PCT_CAP` (default 5%).
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

- Requires a **third, separate** Alpaca paper account — do not reuse
  either the equities Trader's `APCA_*` keys or the crypto scalper's
  `CRYPTO_APCA_*` keys. Create a new paper account (or new key pair) at
  https://app.alpaca.markets/paper/dashboard/overview and set
  `DAYTRADER_APCA_API_KEY_ID` / `DAYTRADER_APCA_API_SECRET_KEY` /
  `DAYTRADER_APCA_BASE_URL` as GitHub Actions repository secrets on
  `fabjon14-cmd/Jarvis-trader` (`gh secret set NAME --repo
  fabjon14-cmd/Jarvis-trader`), matching exactly how the crypto scalper
  was bootstrapped.
- Market-data calls (`get_bars`, used by both `research.py` and
  `backtest.py`) hit `data.alpaca.markets`, which accepts any valid
  Alpaca key pair regardless of which paper account it's tied to — so the
  same new key pair covers both backtesting and live trading, no separate
  data-only credential needed.
- `.github/workflows/daytrader.yml` has **no `schedule:` trigger yet** —
  `workflow_dispatch` only, deliberately, so this can't start live-trading
  (even on paper) the moment credentials exist. Add the schedule back in
  (commented-out block already in the file) only after the backtest above
  has run and been reviewed.
- Other env vars (see root `.env.example`): `DAYTRADER_UNATTENDED`,
  `DAYTRADER_FAST_EMA`, `DAYTRADER_SLOW_EMA`, `DAYTRADER_RSI_PERIOD`,
  `DAYTRADER_RSI_ENTRY_MIN`, `DAYTRADER_RSI_ENTRY_MAX`,
  `DAYTRADER_STOP_LOSS_PCT`, `DAYTRADER_TAKE_PROFIT_PCT`,
  `DAYTRADER_TARGET_RISK_PCT`, `DAYTRADER_DAILY_DRAWDOWN_LIMIT_PCT`,
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
