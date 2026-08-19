# Day Trader — Project Notes

## ⚠ Live since 2026-08-07 on the operator's explicit instruction, after this backtest

**Original backtest** (flat -1%/+2% SL/TP, no ATR or time-of-day filter),
6 independent 30-day windows, 2,316 total trades:

| Window (days ago) | Return | Trades | Win rate |
|---|---|---|---|
| 0-30 | +9.48% | 397 | 35.3% |
| 30-60 | -16.38% | 383 | 30.5% |
| 60-90 | +0.10% | 382 | 34.6% |
| 90-120 | +18.04% | 392 | 37.5% |
| 120-150 | -26.36% | 350 | 27.7% |
| 150-180 | -8.44% | 412 | 35.4% |

Average -3.93%/window, 3/6 profitable — underperformed SPY buy-and-hold
(+10.76% over the same 180 days, ≈+1.79%/window-equivalent) by a wide
margin. Same shape of result as the crypto scalper's own study.

**Re-run on the SAME 6 windows** after adding ATR-based stop/TP, decoupled
position sizing, bracket orders, an ATR-above-average volatility filter,
and a 9:45-11:30/15:00-15:45 ET time-of-day filter (see the sections
below for each):

| Window (days ago) | Return | Trades | Win rate |
|---|---|---|---|
| 0-30 | +3.34% | 73 | 39.7% |
| 30-60 | +4.72% | 89 | 38.2% |
| 60-90 | +6.13% | 72 | 43.1% |
| 90-120 | +1.57% | 58 | 37.9% |
| 120-150 | -0.49% | 80 | 35.0% |
| 150-180 | +5.53% | 69 | 36.2% |

**Average +3.47%/window, 5 of 6 windows profitable** (the one loser was
nearly flat, -0.49%) — now *beating* the SPY benchmark's
≈+1.79%/window-equivalent, on 441 total trades (down from 2,316 — the
filters cut volume roughly 5x while improving win rate on every single
window). This is a real, substantial turnaround, not a marginal one.

**Read before treating this as settled, though:**
- **Five changes went in at once.** This confirms the *combination* has
  a better historical result than the original spec — it does not tell
  you which individual filter(s) are doing the work, or whether some
  subset alone would do as well or better. Not isolated, because the
  operator specified all five as one coherent hypothesis, not as
  variants to compare — worth remembering if any one of them is changed
  later, since there's no per-filter attribution to fall back on.
- **Smaller per-window sample.** 58-89 trades per window here vs
  350-412 before — more room for variance in any single window's number,
  even though the *pattern* (positive in 5/6 independent windows) is
  itself meaningful evidence, not a single lucky window.
- **This is one parameter set, tested once, not tuned against these
  windows.** Unlike the crypto scalper's ATR-multiplier search (which
  tried several multiplier values and could be accused of picking the
  one that happened to look best), these exact multipliers/thresholds
  were the operator's stated hypothesis going in — this backtest is a
  single honest test of that hypothesis across 6 independent windows,
  not a search over configurations. That's a meaningfully cleaner
  validation than a swept parameter, but it's still a first test, not a
  long track record.

**Enabled for live (paper) trading 2026-08-07** — the operator was shown
this exact result, including the three caveats above, and explicitly
chose to enable it. The `schedule:` trigger is now live in
`.github/workflows/daytrader.yml`. This is a deliberate, informed choice
made with the numbers and caveats in front of the operator — not a
default, and not a claim that the caveats above stopped mattering. If
live results start to diverge meaningfully from this backtest, re-read
this section before assuming the backtest was wrong rather than the live
sample just being small so far.

**To pause again:** remove the `schedule:` block from
`.github/workflows/daytrader.yml` (revert to `workflow_dispatch` only) —
`workflow_dispatch` stays available either way for manual runs/testing.

Raw run history: original backtest — 31160917572, 31160995335,
31161057871, 31161099402, 31161148010, 31161186487, SPY benchmark
31161344324. Re-run after the 5 changes — 31162753956, 31162945744,
31163003409, 31163059634, 31163115516, 31163168447.

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

**Implementation (as originally built 2026-08-07)**: `research.
compute_position_qty()` computed `(PER_TRADE_PCT_CAP / 100) ×
account_balance / entry_price` directly — no stop-distance term.
`DAYTRADER_MAX_ORDER_NOTIONAL` (default $500) applied as a secondary
dollar ceiling — whichever of the two was smaller actually bound. See
"Fixed £100 per-trade sizing" below for what replaced this on 2026-08-18.

## Fixed £100 per-trade sizing (2026-08-18, operator's explicit choice, replaces the 1% cap above)

The operator asked to size trades at a fixed **£100 per trade** instead
of the 1%-of-balance cap above. Since this account is USD-denominated
(Alpaca doesn't offer GBP-denominated US equity accounts), this became a
fixed **$126** target (the GBP/USD rate on 2026-08-18) — a static dollar
figure, not a live FX conversion, so it will drift from true £100
equivalence as the exchange rate moves over time; revisit
`DAYTRADER_FIXED_TRADE_NOTIONAL` periodically if that drift matters.

**This is a real, confirmed ~12x increase in risk per trade** — 1% of
this account's ~$1,000 balance was ~$10; $126 is ~12-13% of it. Flagged
explicitly before making the change (via a clarifying question covering
both the USD-conversion question and the risk-size question), and the
operator confirmed proceeding with both eyes open: up to ~half the
account can now be deployed across 5 concurrent positions, versus ~5%
under the old 1% cap. Not a mistake to "fix" later — a deliberate choice
made with the numbers in front of the operator, same standard as every
other risk-parameter change in this project.

**Implementation**: `research.compute_position_qty()` now computes
`FIXED_TRADE_NOTIONAL / entry_price` directly (the `account_balance`
parameter is still accepted for call-site compatibility but no longer
used). `DAYTRADER_MAX_ORDER_NOTIONAL` ($500) still applies as a secondary
ceiling and doesn't bind under normal conditions (126 < 500) — kept as a
layered backstop, not removed. `binding_constraint` in the journal is now
`fixed_trade_notional` or `max_order_notional` (renamed from
`per_trade_pct_cap` to match).

**Side effect worth knowing**: at $126/trade, DIS (~$105-110) now floors
to a whole share (1.2 shares → 1) and uses a bracket order — the first
watchlist symbol where that happens routinely instead of only via the
one-off qty=1 test order. Every other watchlist symbol ($150+) still
computes fractional and uses the plain-order path. See "Fractional-share
fallback" above for why that split exists and what it means for
notification timing (bracket-managed exits don't get an immediate email,
only the hourly digest catches those).

## ATR-based stop/take-profit (2026-08-07, operator's suggestion)

Replaced the flat `-1.0%` / `+2.0%` stop/take-profit with each trade's own
5-minute ATR(14) at entry time — `ATR_STOP_MULTIPLIER` (1.5x) /
`ATR_TP_MULTIPLIER` (3.0x), preserving the original 1:2 risk:reward
ratio. Same fix the crypto scalper already needed for the same reason: a
flat percentage doesn't account for the fact that different stocks (and
the same stock at different times) have different natural volatility —
sizing the stop/TP off that pair's own recent movement means a calmer
stock gets a tighter stop and a choppier one gets more room, instead of
the same distance regardless.

`research.compute_atr_based_stop_tp(entry_price, atr14)` computes this;
`get_signal()` returns `stop_price`/`tp_price` alongside the rest of the
technical validation so the agent can submit them directly on the bracket
order (see below) without a second lookup.

## Why position sizing stays decoupled from the ATR stop

The operator's initial proposal combined this with position sizing —
`Shares = (Balance × 0.01) / (1.5 × ATR)` — which was checked against
real market data before building it (`scripts/backtest.py atr-check`,
still in the repo as a standing diagnostic) and found to blow up the same
way the original flat-percentage risk formula did, only worse:

| Symbol | ATR as % of price | Notional this formula produces |
|---|---|---|
| AAPL | 0.12% | 578% of account |
| JPM | 0.11% | 618% of account |
| V | 0.10% | 650% of account |
| AMD (most volatile tested) | 0.35% | 192% of account |

Every symbol came out between 192% and 650% of the account on one trade
— worse than the original "risk 1% + 1% stop = 100% of account" bug,
because a 5-minute ATR is *always* a small fraction of price (here,
0.1-0.35%), and `shares = risk_$ / stop_distance` blows up whenever the
stop distance is small relative to price. Coupling risk-target sizing to
a tight, volatility-derived stop distance makes the blowup worse, not
better.

**The fix keeps the two decisions independent**, same principle as the
"Position sizing" section above: `research.compute_position_qty()` (flat
1% of account balance ÷ price) decides *how many shares*; the ATR-based
stop/TP above decides *where the exit prices sit*. Neither one is derived
from the other. If the position-sizing cap is ever raised well past 1%,
re-run `atr-check` before trusting the new number — the blowup shown
above is inherent to the risk/stop-distance formula, not specific to 1%.

## Bracket orders (2026-08-07, operator's suggestion)

New entries are now submitted as Alpaca `order_class: "bracket"` orders
— the entry limit order plus the ATR-based stop-loss and take-profit as
broker-managed child legs, via `trade.place_bracket_order()`. This is a
real execution-quality improvement over the original design: a stop or
TP now fires immediately when the broker sees a matching price, not only
when this agent happens to poll again (up to 5 minutes later). Shares
the exact same duplicate-order check and buy-side caps as a plain order
(`_check_buy_allowed()`, factored out so the two order paths can't drift
apart).

**The wrinkle**: the strategy also exits on the opposite EMA crossover
and the forced end-of-day flatten, neither of which is a price level a
bracket order can express — those two exits still require this agent's
5-minute poll. When either fires, `trade.cancel_symbol_orders()` cancels
the bracket's still-resting SL/TP legs *before* the manual sell is
submitted — otherwise the resting child orders would hold qty against
(or conflict with) the manual exit, since Alpaca has no way to know a
signal-based exit supersedes the bracket.

## Fractional-share fallback (fixed 2026-08-08 — real trades were silently failing)

**Every real signal that fired on 2026-08-07 (DIS and V, both during the
afternoon entry window) was rejected by Alpaca** — not held, not
skipped, actually rejected: `"fractional orders must be simple orders"`
(code 42210000). Root cause: this account's equity is **$1,000.32**
(shared with the crypto scalper), so `PER_TRADE_PCT_CAP` (1%) computes
to about $10 per trade — a fraction of a share for every $100+ symbol on
the watchlist (0.0953 shares of DIS, 0.0275 of V). Bracket orders don't
support fractional quantities on Alpaca at all; only plain orders do.
This meant every trade this account could actually afford was
guaranteed to fail, silently from the strategy's perspective (it logged
`NEW_TRADE` and a clean rejection reason, so nothing crashed — it just
never actually traded, which is why it looked like "no signal fired"
until the journal was read closely).

**Fix**: `agent/daytrader_agent.py`'s entry logic now branches on the
computed quantity:
- `qty >= 1` → unchanged: floor to a whole share count, bracket order,
  broker-managed stop/TP.
- `0 < qty < 1` → plain (non-bracket) order at the fractional quantity,
  with the ATR-derived stop/TP packed into `client_order_id` via
  `research.encode_trade_params()` (`dt-sl10458-tp10514` = stop $104.58,
  TP $105.14) — the exact same technique the crypto scalper already uses
  for the identical "no broker-side place to persist per-trade exit
  levels" problem, just with prices instead of percentages since these
  are already ATR-derived absolute prices.

The exit loop now checks `research.has_open_bracket_legs(symbol)` first:
if true, unchanged (crossunder/EOD only, bracket handles SL/TP). If
false, it also manually checks the position's current price against the
decoded stop/TP from its entry order's `client_order_id` — restoring the
polling-based check bracket orders were meant to replace, but only for
positions that couldn't use a bracket order in the first place.

**Given this account's size, expect most real trades here to use the
fractional/plain path, not bracket orders** — the watchlist is all
$100+ large-caps, and $1,000 × 1% ≈ $10 rarely buys a whole share of any
of them. Bracket orders only actually apply on this account for the
cheaper symbols (XOM, DIS) if the position-size cap is ever raised, or
if account equity grows. Worth knowing before assuming "it's using
bracket orders" without checking `order_kind` in the journal.

## ATR volatility filter (2026-08-07, operator's suggestion)

Added to `get_signal()`'s buy conditions: the current bar's ATR(14) must
be strictly greater than its own 20-period SMA (`ATR_SMA_PERIOD`) — only
trade in above-average-movement conditions. This directly targets what
the first backtest's exit-reason breakdown showed: most losing trades
exited via `opposite_crossover` (a small reversal, avg -0.17%) rather
than the hard stop (avg -1.0%), meaning most losses were whipsaws from
trading a fast EMA cross during flat, directionless stretches where
there was no real trend to catch. Requiring above-average ATR is meant to
filter those stretches out.

Note this is the **opposite condition** from the crypto scalper's ATR
spike filter, which *blocks* trades above a volatility threshold (because
its RSI-oversold reversal signal gets unreliable during a shock). Not a
contradiction — different strategy, different failure mode: crypto's
signal breaks when volatility spikes; this one's breaks when volatility
is absent.

## Time-of-day filter (2026-08-07, operator's suggestion)

New entries restricted to two US/Eastern windows: **9:45-11:30** (skips
the 9:30-9:45 opening-range chop) and **15:00-15:45** (a narrow pre-close
window, skips both the midday 11:30-15:00 lull and the final 15:45-16:00
close-out scramble). `research.is_in_entry_window()` converts via
`zoneinfo.ZoneInfo("US/Eastern")` (correct across EDT/EST, unlike a fixed
UTC offset) — used identically by the live agent (real current time) and
the backtest (each historical bar's own timestamp), so the exact same
rule replays against history rather than an approximation of it.

**Entries only** — same "a filter blocks new buys, never a mandatory
exit" convention as every other gate in this project (circuit breaker,
daily notional cap, etc.). A position already open can still exit via
crossover or forced flatten at any time of day.

## Backtest re-run after all five of the above (2026-08-07)

See the top banner for the updated 6-window results after ATR-based
stop/TP, decoupled sizing, bracket orders (execution-quality only, not
backtestable as a distinct effect), the ATR volatility filter, and the
time-of-day filter were all added together.

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

**Run 2026-08-07 across 6 independent 30-day windows** — see the top
banner for the full results table. The honest, evidence-backed standard
this project holds itself to (see crypto-scalper/CLAUDE.md in full) is:
don't trust a signal because the code runs cleanly; trust it because a
real historical test, ideally across more than one window, says it has
positive expected value. The original spec's test came back mixed/
negative; the re-test after the 5 filter/execution changes came back
positive — see the top banner for both results and their caveats before
treating this strategy as more validated than it is.

## Self-hosted runner (added 2026-08-07)

Day one of live trading exposed the same problem the crypto scalper's
cron already had: GitHub's hosted `ubuntu-latest` runners are best-effort
— the entire 9:45-11:30 ET morning entry window went by with **zero**
runs firing (last run before it: 08:51 UTC; next run after it: 16:11
UTC, well past the window's close). Confirmed the same day that even
manually-triggered `workflow_dispatch` runs were sitting queued for 15+
minutes before GitHub auto-cancelled them — this is a shared hosted-runner
capacity issue, not a cron-syntax problem, so no amount of adjusting the
schedule expression fixes it.

**Fix**: `.github/workflows/daytrader.yml` and `daytrader-backtest.yml`
now run on a self-hosted runner (`runs-on: [self-hosted, macOS,
trading-bots]`) installed on the operator's own Mac
(`~/actions-runner`), registered as a per-user LaunchAgent
(`actions.runner.fabjon14-cmd-Jarvis-trader.jonathans-mac`) so it
survives logout/reboot and starts automatically. Jobs now start
immediately — no shared queue to wait in.

**This trades one failure mode for another** — the bot now only fires
when this specific Mac is powered on, awake, and connected. If it sleeps
or loses network, the schedule goes fully silent with no GitHub-hosted
fallback (worse than a delayed run, since a delayed run at least
eventually happens). Keep this machine awake and online during market
hours for the schedule to actually work as intended; verify awake/asleep
history if a gap shows up in the journal that isn't explained by the
entry-window filter.

**Security note**: self-hosted runners on a *public* repository are a
known risk mainly when a workflow can be triggered by outside
contributors (e.g. `pull_request` from a fork) — a malicious PR could run
arbitrary code on the runner's host machine. Neither `daytrader.yml` nor
`daytrader-backtest.yml` has a `pull_request`-style trigger (only
`schedule` and `workflow_dispatch`, the latter restricted to repo
collaborators), so this specific attack vector doesn't apply here — but
if any future workflow on this repo ever adds a `pull_request` trigger,
it must NOT run on this self-hosted runner without separately addressing
that risk (e.g. requiring approval for first-time contributors, which
GitHub provides natively for exactly this reason).

Runner packages verified against GitHub's published SHA-256 digest
before installation (`actions-runner-osx-x64-2.336.0.tar.gz`,
`sha256:f79c43...548fe`) — standard practice for a binary that runs
persistently with repo-scoped credentials.

**Dependencies**: this Mac's system Python (3.14) is used directly
(no `actions/setup-python@v5` — unreliable on self-hosted runners without
a pre-populated tool cache, and the codebase has no 3.11-specific
dependency). `pip install --break-system-packages` is used for
`requests`/`python-dotenv` since this Mac's Python is
externally-managed (Homebrew/PEP 668) — a deliberate, narrow exception
for two pinned, already-installed packages, not a general policy change.

## Local launchd trigger (added 2026-08-07)

The self-hosted runner (above) fixed *execution* delay — once a job is
dispatched, it now starts in seconds. It did **not** fix a second,
separate problem discovered the same day: GitHub's own `schedule:` cron
*dispatch* went 35+ minutes without firing at all during market hours,
with the self-hosted runner sitting idle and ready the entire time. This
confirmed the two failure modes are independent — self-hosted vs. hosted
runners only affects what happens *after* GitHub decides to fire the
trigger, not whether/when it decides to fire it at all. GitHub's
`schedule:` trigger is documented as best-effort with no reliability
guarantee, and switching runner type doesn't touch that.

**Fix**: removed the `schedule:` trigger from `daytrader.yml` entirely.
Firing is now driven by a local launchd job on the operator's Mac —
`~/Library/LaunchAgents/com.jarvis-trader.daytrader-trigger.plist`
(`StartInterval: 300`, i.e. every 5 minutes, continuously) runs
`~/actions-runner/trigger-daytrader.sh`, which calls `gh workflow run
daytrader.yml --repo fabjon14-cmd/Jarvis-trader` (a `workflow_dispatch`
call, not a `schedule` event). An OS-level launchd timer doesn't have
GitHub's schedule-dispatch reliability problem — it's local, not
dependent on a remote scheduler queue.

**Fires 24/7, not just market hours** — deliberately not replicating
market-hours/DST gating logic in launchd when the agent itself already
does this correctly via `research.get_market_clock()` (holds cleanly
with "Market closed" when appropriate, confirmed working in the very
first live runs). Simpler and avoids a second place for market-hours
logic to drift out of sync with the first. Logs:
`~/actions-runner/trigger-daytrader.log` /
`trigger-daytrader.err.log`.

**This still depends on the Mac staying awake and online** — same
requirement as the self-hosted runner above, now doing double duty (both
the trigger and the execution depend on this machine). If it's the
runner itself that's the deeper cause of unreliable timing rather than
GitHub's scheduler specifically, that would show up as: launchd fires
`gh workflow run` reliably (check the trigger log — should show a new
run URL every 5 minutes) but the resulting Actions runs still queue or
start late (check `gh run list --workflow=daytrader.yml`). Check both
logs, not just one, if timing looks off again.

**Verified working**: watched the trigger log fire 3 consecutive times at
16:50:42 / 16:55:44 / 17:00:47 UTC — consistent ~5-minute spacing, a real
improvement over GitHub's 35+ minute silent gap earlier the same day.

### Git push race surfaced by faster, more reliable firing

One of those three verification runs (16:50:42) failed — not from
anything above, but because it landed only 16 seconds after a manual
test run, and its `git push` was rejected non-fast-forward since the
other run's commit had already moved `main` forward. This is a
pre-existing gap (the plain `git commit && git push` in "Commit journal
update" never retried), just more likely to actually manifest now that
firing is fast and reliable instead of randomly staggered by hosted-runner
queue delays. It can also happen *across* workflows — crypto-scalper.yml
pushes to the same `main` branch on its own independent schedule, with no
shared concurrency group between the two.

**Fixed**: the commit step now retries with `git pull --rebase origin
main` on a rejected push (up to 5 attempts, short random backoff) instead
of failing the job outright. `crypto-scalper.yml` has the identical
pre-existing gap and needs the same fix — flagged as a separate follow-up
rather than changed inline here, since that's a currently-live trading
workflow this session wasn't otherwise touching.

## Immediate per-trade email notifications (added 2026-08-08)

On top of the hourly digest below, `agent/daytrader_agent.py` now emails
immediately — same run, right after the order is confirmed placed — for
every buy and every sell it executes itself: `NEW_TRADE` (entry, either
`order_kind`) and `CLOSE` (crossunder, forced EOD flatten, or a manually-
checked stop-loss/take-profit on a fractional position). `_notify_trade()`
wraps the send in try/except so a notification failure (Resend down, bad
credentials) can never break or block the actual trade — it's a side
effect of the trading logic, never a dependency of it.

**Real gap, not a bug**: a whole-share position's stop-loss/take-profit
is filled by the broker directly (see "Bracket orders") — the agent's
own code never executes that sell, so there's no point in this code path
to hook an immediate notification for it. The DIS test trade closed
exactly this way (broker-side take-profit fill, no `CLOSE` line in the
journal at all) and would NOT have triggered an immediate email under
this design — only the hourly digest below would catch it, within up to
60 minutes. Given this account's size, most real trades will use the
fractional path (which the agent does execute directly, so immediate
notification does apply) — but if the position-size cap or account
equity ever grows enough that bracket/whole-share trades become common,
this gap gets more relevant and may be worth closing (e.g. having the
exit loop diff "positions open last run" vs "positions open now" to
catch broker-side fills the agent didn't initiate itself).

## Weekly performance review (added 2026-08-18)

`scripts/review.py` mirrors the crypto scalper's `review.py` — win rate,
realized P&L, orders placed/filled/rejected, and a SPY buy-and-hold
benchmark, computed from Alpaca's own order/portfolio history, not the
journal's prose. Round-trips are matched simple buy→sell pairs (safe
because of the no-averaging-in rule — never more than one open position
per symbol to match against), filtered to `WATCHLIST_SYMBOLS` since the
account is shared with the crypto scalper.

**One real caveat**: `shared_account_equity_change_pct` reflects the
*whole* account, crypto scalper activity included — Alpaca has no
per-strategy equity curve. `realized_pnl` (summed from daytrader's own
matched round-trips) is the reliable number; the equity-change and SPY-
comparison figures are directional context only, not a clean
daytrader-only return. Called out explicitly in the rendered review so
it isn't mistaken for a precise number.

Runs via `.github/workflows/daytrader-review.yml`
(`review.py write PERIOD_DAYS`, writes `reviews/YYYY-MM-DD.md`) or
on-demand through `daytrader-backtest.yml`'s `review_days` input
(`review.py show`, prints without writing/committing). Driven by a local
launchd job — `~/Library/LaunchAgents/com.jarvis-trader.daytrader-review-trigger.plist`,
Fridays ~21:30 UK local time (matches 20:30 UTC during BST; drifts to
21:30 UTC once GMT resumes in late October — not worth a twice-yearly
plist edit for a weekly summary's timing, unlike the 5-minute trading
loop where timing actually matters) — not GitHub's `schedule:`, same
reasoning as every other trigger in this project now.

## Hourly email digest (added 2026-08-08)

Mirrors the crypto scalper's `hourly_digest.py` exactly, adapted for a
shared account: `scripts/hourly_digest.py` reads Alpaca's own order
history for the last 60 minutes, filtered to `WATCHLIST_SYMBOLS` (not
"any order on this account" — the crypto scalper's own activity would
otherwise show up too, see "Sharing the crypto scalper's Alpaca
account"), and emails a summary via the shared `../../scripts/notify.py`
— but only if something actually happened; a quiet hour sends nothing
(no heartbeat spam).

Driven by another local launchd timer, not GitHub's `schedule:` —
`~/Library/LaunchAgents/com.jarvis-trader.daytrader-digest-trigger.plist`
(`StartInterval: 3600`) runs `~/actions-runner/trigger-daytrader-digest.sh`,
which calls `gh workflow run daytrader-hourly-digest.yml`. Same reasoning
as the main trading trigger: GitHub's own cron dispatch already proved
unreliable once (see "Local launchd trigger" above) — no reason to trust
it here either, even though a late digest email is lower-stakes than a
missed trading cycle.

Uses the same `RESEND_API_KEY`/`REPORT_TO_EMAIL` secrets as both other
bots — no new credentials needed. `daytrader-hourly-digest.yml` also
supports a `test: true` manual input that sends a one-off confirmation
email without touching Alpaca at all, for verifying the email path works
independent of any real trade activity.

## Manual order-placement verification (added 2026-08-07)

Every live run so far had been a hold — the strategy's actual entry
conditions (EMA cross + RSI band + ATR-above-average + time-of-day
window) hadn't fired yet, so equity order placement itself had never
been proven to work on this account, only market-data access had (see
"Setup notes" below). The operator asked to "let it trade" as a quick
test; rather than loosening the time-of-day filter to force a real
signal-driven trade — which the operator specified as strict, "bypass
the signal logic completely" outside the windows — `scripts/test_order.py`
was added instead: a one-off manual tool that places a single small
bracket order directly, using a real ATR-derived stop/TP from
`get_signal()`, but skipping the `buy_signal` gate entirely. This proves
the same thing (does Alpaca accept an equity bracket order on this
account, does it journal correctly) without weakening the tested
strategy's rules even temporarily.

Runs via `.github/workflows/daytrader-test-order.yml` —
**`workflow_dispatch` only, never scheduled**, deliberately kept out of
`daytrader.yml` so it can never fire automatically. Once placed, the
resulting position is picked up and managed by the normal agent
(crossunder exit / forced EOD flatten) exactly like a real strategy
trade — this only bypasses the *entry* decision, not any of the
position-management logic after that.

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
- `.github/workflows/daytrader.yml`'s `schedule:` trigger is **live**
  (enabled 2026-08-07, see top banner) — 5-minute cron, 13:00-21:59 UTC
  weekdays. `workflow_dispatch` also stays available for manual runs.
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
