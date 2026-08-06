# Crypto Scalper — Project Notes

## ⚠ Trading live despite a negative backtest finding (2026-08-06) — read before assuming this is validated

This agent was briefly paused, then **resumed the same day on the
operator's explicit, informed instruction**, after being shown the
finding below and confirming they wanted it running anyway on this
paper account. This is not an oversight or a stale warning — it's a
deliberate choice being made with the numbers in front of the operator,
and it should stay that way: don't let a future run of this bot, or a
future editor of this file, quietly forget why this line exists.

**The finding:** a full research pass the same day (documented in detail
under "ATR multiplier calibration" and the sections after it below)
tested this signal — and a from-scratch trend-following alternative —
across **8 independent, non-overlapping 60-day windows spanning 480 days
(16 months) of real market history**. Results, averaged across all 8
windows:

| | Avg return/window | Windows profitable |
|---|---|---|
| Mean-reversion (this strategy, currently live) | **-22.2%** | 1 of 8 |
| Trend-following (alternative tried, not live) | **-42.4%** | 0 of 8 |
| BTC buy-and-hold (benchmark) | -2.25% | — |

Both signal families lose money on average, both lose considerably more
than simply holding BTC, and a market-regime classifier (efficiency
ratio — see "Regime classification check" below) does not predict which
one wins in a given window. This isn't a thin or ambiguous result — it's
a large-sample, multi-window, multi-strategy finding, not a single bad
backtest.

**To pause again:** set `CRYPTO_TRADING_PAUSED: "1"` back in the `env:`
block of `.github/workflows/crypto-scalper.yml` — the mechanism is still
in `scalper_agent.py` (`TRADING_PAUSED`), blocks new buys only, and
never blocks exits on an open position.

---

A second, independent agent from the equities Trader in the repo root — its
own, separate Alpaca **paper-trading** (fake money) account (own API keys,
`CRYPTO_APCA_*` env vars, own equity curve), a different strategy, a
different watchlist, its own risk budget, and its own journal. Do not point
it at a live account without deliberately deciding to.

This exists because a human asked for an RSI/EMA momentum-scalp strategy,
which is a fundamentally different risk shape than the equities bot's
research-driven, hourly, hold-for-days approach. It runs on its own Alpaca
account specifically so a bug, a runaway loop, or a bad fill in this agent
can never touch the equities bot's account, and vice versa — full blast-
radius isolation between the two, not just separate code paths on shared
capital.

## Tools available

- `scripts/research.py` — `signal PAIR | bars PAIR | positions | deployed | exit-flags | circuit-breaker | max-positions PAIR | category-cap PAIR | account` (read-only). Talks to this agent's own Alpaca account via `CRYPTO_APCA_API_KEY_ID` / `CRYPTO_APCA_API_SECRET_KEY` / `CRYPTO_APCA_BASE_URL` — entirely separate credentials from the equities Trader's `APCA_*` vars.
- `scripts/trade.py` — `order PAIR QTY SIDE [LIMIT_PRICE]`
- `agent/scalper_agent.py` — the actual run loop: checks exits, then evaluates new-buy signals, places orders, builds a JSON decision envelope for every pair, and appends both to the day's journal. This is what gets scheduled every 5 minutes.
- `scripts/review.py` — `write [PERIOD_DAYS] | show [PERIOD_DAYS]`, quantitative-only performance rollup (win rate, realized P&L, BTC benchmark). Scheduled weekly, separately, via `.github/workflows/crypto-scalper-review.yml`. See "Weekly review" below.
- `watchlist.json` — crypto pairs in scope: 19 pairs as of 2026-08-06 (expanded from 7 — see "Watchlist expansion to 19 pairs" below). Don't trade pairs outside it without being told to.
- `categories.json` — category per pair (`Layer1`, `DeFi`, `Meme`, `Payments`, `Infrastructure`, `Utility`), backing the category cap below. Mirrors the equities bot's `sectors.json` pattern.
- `../scripts/notify.py` (shared with the equities bot) — email the day's journal. Call this **at most once a day** (e.g. after the last scheduled firing), not every run — this agent fires far more often than the equities bot's hourly cadence, and per-run emails would be spam.
- `scripts/hourly_digest.py` — emails a summary of any buy/sell activity in the last hour, via the shared `notify.py`. Sends nothing if there was no activity. Scheduled hourly via `.github/workflows/crypto-scalper-hourly-digest.yml`. See "Hourly digest" below.

`scripts/trade.py`'s `place_order` pauses for a human y/N confirmation when
run interactively. Under `CRYPTO_SCALPER_UNATTENDED=1` (scheduled/cloud runs)
it auto-approves instead, but only accepts limit orders and enforces every
cap below in code. A rejection under those caps is expected behavior — log
it, don't retry with adjusted numbers.

## Strategy — precisely

**Buy trigger:** ALL of the following must be true (expanded 2026-08-05
from RSI+EMA20 alone, at the operator's request, to add trend context and
a volatility filter):
- RSI(14) < 30 on **5-minute** bars (Wilder's smoothing, standard 14-period).
- The close **crosses above** EMA(20), also on **5-minute** bars — the
  previous bar's close was at or below its EMA, and this bar's close is
  above its EMA. A pair already trading above its EMA with no cross event
  does not qualify; this is a cross detector, not a level check. (Same
  precision principle as the equities bot's stabilization-signal
  definition — a fuzzy "looks like it's turning" is not verifiable, a
  cross event is.)
- **EMA(20) > EMA(50)** ("bullish alignment"), computed on **1-HOUR**
  bars — a deliberately slower timeframe than the two conditions above.
  See "Multi-timeframe trend filter" below for why; the short version is
  that computing this on the same 5-minute bars made it nearly impossible
  to satisfy alongside RSI-oversold, and a backtest confirmed it: zero
  trades across 60 days / 136k bars.
- **No ATR volatility spike:** ATR(14) on **5-minute** bars (Wilder-smoothed)
  is not more than `CRYPTO_ATR_SPIKE_MULTIPLE` (default 2x) its own
  trailing 20-period average. RSI and EMA behave unreliably during a
  volatility/news shock — a spike blocks the signal regardless of what
  RSI/EMA say, same "don't trust a check that can't be verified right now"
  principle as the API-failure handling below.

### Multi-timeframe trend filter (added 2026-08-05)

The EMA(20)/EMA(50) alignment check originally ran on the same 5-minute
bars as everything else. A backtest of that version (`scripts/backtest.py`,
60 days, ~136k bars across all 8 pairs) produced **zero trades** — not a
low win rate, literally no entries at all, matching the zero trades
observed in live testing over several hours the same day. The cause: a
real RSI(14)-oversold reading on 5-minute bars usually happens *during* a
decline sharp enough to also drag the fast-reacting 5-minute EMA(20) below
the 5-minute EMA(50) at the same time — so "oversold" and "still
bullish-aligned" were nearly mutually exclusive when both were measured on
the same fast timeframe.

The fix: compute the EMA(20)/EMA(50) alignment on **1-hour** bars instead
(`research.get_1h_trend_alignment()`), while RSI and the entry cross stay
on 5-minute bars. An hourly EMA reacts far more slowly than a 5-minute
one, so a brief 5-minute dip doesn't also flip the hourly trend — this
keeps the original intent (don't buy dips in a genuine downtrend) while
actually being satisfiable alongside a 5-minute oversold reading. This is
a standard real technique (multi-timeframe confirmation: a slow timeframe
for trend context, a fast timeframe for entry timing), not a novel
experiment.

Only **fully completed** hourly bars are used — Alpaca's most recent
returned 1-hour bar for a live "now" fetch is very likely the
currently-forming (incomplete) hour, and using its still-changing values
would be the same "don't count today's still-forming data" mistake this
project avoids elsewhere (see the equities bot's falling-knife rule). The
backtest replicates this exactly via timestamp-based lookup — a 1-hour
bar's EMA values only become usable in the replay from
`bar_start + 1 hour` onward, never before, so there's no look-ahead bias
introduced by the fix.

After this change, re-run the backtest before trusting the strategy
further — the fix addresses the *mechanism* that produced zero trades,
but doesn't by itself prove the resulting signal has positive edge.

All four conditions are computed together in `research.get_signal()` and
returned as a single `buy_signal` boolean — see that function's docstring
for the exact formulas.

**Take-profit:** close the position at **+1.5%** unrealized P/L from cost
basis (Alpaca's own `unrealized_plpc`, not a derived recalculation).

**Stop-loss:** close the position at **-0.75%** unrealized P/L from cost
basis. Mechanical, fires on price alone.

**Max hold time:** if a position has been open **4 hours**
(`CRYPTO_MAX_HOLD_HOURS`) without hitting either threshold, close it anyway.
Not in the original spec, but a scalp strategy with no time bound can turn
into indefinite bag-holding on a pair that just chops sideways — this keeps
the agent's exposure window bounded, in the same spirit as the rest of this
project's "don't let a soft signal turn into an open-ended risk" approach.
Reassess this value like any other parameter if it turns out to cut winners
short in practice.

**No averaging in:** if a pair already has an open position, a new buy
signal on it is a hold, not an add — one entry per pair at a time. This
means the strategy can't pyramid into a single name; each pair's exposure is
capped at one trade's notional regardless of how many times the signal
fires while it's held.

**No leverage.** Every order trades cash buying power only — nothing in
`place_order` requests margin, and this should stay that way unless a human
deliberately decides otherwise (see Leverage note below).

**Order pricing:** entries, take-profit exits, and timeout exits are all
limit orders priced within 0.1% of the latest close
(`CRYPTO_LIMIT_SLIPPAGE_BUFFER` in `scalper_agent.py`, above the reference
price for buys, below it for sells).

**Stop-loss execution is the one exception — market order, not limit**
(changed 2026-08-05). A limit sell can simply not fill during a sharp,
fast drop, letting the loss run past -0.75% with no backstop — the entire
point of a stop-loss is a guaranteed exit, which a resting limit order
doesn't provide. `trade.place_order(..., market=True)` is used only for
`close_stop_loss` exits; every other order type stays limit-only. The
reference price is still passed through and used for the notional-cap
check and duplicate-order dedup — it just isn't sent as the order's actual
price. While fixing this, a related gap was found and fixed in the same
change: the per-order notional cap (`CRYPTO_MAX_ORDER_NOTIONAL`) and the
per-run order-count cap (`CRYPTO_MAX_ORDERS_PER_RUN`) previously applied
to every order unconditionally, meaning a mandatory stop-loss exit reached
later in a run could get rejected by the same cap that limits new
buying — contradicting "a mandatory exit is never blocked by a buy-side
gate" a few paragraphs up. Both caps now only apply to buys, matching
every other buy-only gate below.

## Risk controls (enforced in code, not just prose)

These mirror the equities bot's philosophy — a well-reasoned trade shouldn't
be able to talk its way past a mechanical cap — sized down for a smaller,
faster-turnover strategy on a separate budget.

- **Per-trade cap:** the smaller of `CRYPTO_MAX_ORDER_NOTIONAL` (default
  $200), 10% of current buying power (`CRYPTO_PER_TRADE_PCT_CAP` — lowered
  from an initial 5% to 2% on 2026-08-05, then raised to 10% later the same
  day at the operator's explicit request; on the ~$1,000 paper account this
  moved typical position size from ~$20 to ~$100, still under the $200
  notional cap so the % cap now actually binds), and this pair's even
  split of the remaining daily/weekly headroom (see "Even-split allocation"
  below).
- **Max concurrent positions:** `CRYPTO_MAX_POSITIONS` (default 5, raised
  from 3 on 2026-08-05 to accommodate the expanded 8-pair/3-category
  watchlist — a cap of 3 on 8 pairs would have made the category cap below
  meaningless, since positions would always run out before categories
  could concentrate). A new pair can't open a 6th+ position; adding to an
  already-held pair is blocked separately by the no-averaging-in rule above.
- **Category cap:** no more than `CRYPTO_MAX_PER_CATEGORY` (default 2) open
  positions in the same category (`categories.json`: Layer1, DeFi, Meme) at
  time of purchase. Mirrors the equities bot's sector cap exactly — adding
  to an existing position doesn't count, only opening a new one does.
  Checked via `research.check_category_cap()`, enforced in `place_order`
  for buys.
- **Daily/weekly notional cap:** `CRYPTO_DAILY_NOTIONAL_CAP` (default $300)
  and `CRYPTO_WEEKLY_NOTIONAL_CAP` (default $1,500), tracked from this
  account's own order history — a completely separate budget from the
  equities bot's $500/day, $3,000/week cap on its own account. Sells are
  exempt, same reasoning as the equities cap — this bounds new risk, not
  reducing existing risk.
- **Circuit breaker:** same thresholds and logic as the equities bot's
  portfolio-level check (>4% intraday or >8% trailing-5-trading-day drawdown
  halts new buys), computed independently against this account's own equity
  curve — a drawdown on one account has no bearing on the other's circuit
  breaker. Exits are never blocked by this.
- **Per-run order cap:** `CRYPTO_MAX_ORDERS_PER_RUN` (default 5).
- **Duplicate-order protection:** same exact-match-today dedup against
  Alpaca's own order history as the equities bot, for the same reason (a run
  that crashes after submitting but before journaling shouldn't double a
  position on restart).
- **API/data failure handling:** if any check errors or returns nothing
  usable — signal, exit-flags, circuit-breaker, deployed notional, account —
  treat that pair as unknown for this run and hold. Never fall back to
  inferring from price alone or anything else. Log which check failed.

### Leverage

Currently hard-coded to none — the operator was asked and chose cash-only
as the default when this agent was built (2026-08-02), and re-confirmed
this on 2026-08-05 after a specific request to size positions via
leverage (~133% of equity per trade) was declined. Every order is cash-
collateralized against `buying_power`; Alpaca doesn't offer margin on
crypto in any case, so an over-notional order would simply be rejected at
the exchange, not silently leveraged. If leverage is ever genuinely wanted
on a different, marginable venue, that's a deliberate, explicit rebuild —
not a parameter to quietly raise — and should be treated with the same
weight as pointing this at a live account.

### RSI-depth-weighted allocation across simultaneous signals (added 2026-08-05)

Per the operator's request that the strategy not "focus on investing in
one" pair, this started as an even split, then was changed the same day
to a weighted split when the operator asked for stronger setups to get
more capital than weaker ones. **Important framing, stated explicitly so
it isn't misread later:** nothing here predicts which trade will make
more money — that's not knowable from RSI, EMA, ATR, or any other
technical reading. The weighting below is a signal-STRENGTH proxy (how
textbook the setup looks by the numbers already computed), not a
profitability forecast.

If more than one watchlist pair qualifies for a buy in the SAME run, the
remaining daily/weekly headroom is split across all of them weighted by
**RSI depth** — `weight = 30 - rsi`, i.e. how far below the oversold
threshold the reading is. A pair at RSI 10 gets proportionally far more
of the budget than one that barely qualified at RSI 28. This was chosen
over combining RSI depth with EMA-gap trend strength specifically because
a single factor doesn't require inventing a relative weight between two
different things with no basis for the exact ratio.

`scalper_agent.py`'s `run()` does this in two passes: pass 1 evaluates
every pair's signal and collects which ones qualify without sizing or
placing anything yet; pass 2 computes each candidate's share as
`headroom × (weight_i / sum(all weights))`, then sizes it against
`min(MAX_ORDER_NOTIONAL, per_trade_cap, weighted_headroom)` — so a single
qualifying pair still gets the full normal per-trade cap (its weighted
share of itself is 100%), and only sharing with others actually divides
the budget. If a candidate's weighted share would exceed its own
per-trade cap, it's simply capped there — the leftover is **not**
redistributed to the other candidates (a known simplification; revisit
if it turns out to matter in practice).

This addresses within-run concentration by signal strength. It does NOT,
on its own, prevent one pair from qualifying more often than others
across many runs over time (e.g. if DOGE/USD's price action happens to
trip the RSI/EMA/ATR conditions more frequently than BTC/USD does) —
that's the mechanical signal doing its job, not a bug, and the existing
category cap (max 2 per category) and 5-position cap already bound how
much of the account any one pair or category can occupy at once
regardless of how it got there.

### Risk-based sizing (informational ceiling, not the operative cap)

Added 2026-08-05 at the operator's request: for every `NEW_TRADE`,
`research.compute_risk_based_qty()` computes the quantity that would risk
exactly `CRYPTO_TARGET_RISK_PCT` (default 1%) of account equity if the
0.75% stop-loss is hit — i.e. `qty` such that
`qty × entry_price × (stop_loss_pct / 100) == target_risk_pct × equity`.

This is deliberately **not** used as the operative position size on its
own — the final quantity is `min(risk_based_qty, notional_cap_qty)`, where
`notional_cap_qty` comes from the existing per-trade/daily/weekly caps
above. With the current 0.75% stop, a 1% risk target implies a position
worth ~133% of equity — far larger than the 2%-of-buying-power notional
cap allows — so **in practice the notional cap binds, not the risk
target**: actual risk per trade works out to roughly 0.015% of equity, not
1%. This was a deliberate choice (see conversation 2026-08-05) after the
alternative — loosening the notional cap ~66x so 1% could actually bind —
was rejected as a real, undisclosed increase in risk-per-trade dressed up
as a sizing formula.

Every `NEW_TRADE` decision envelope's `risk_sizing` field logs both
numbers transparently: `target_risk_pct`/`target_risk_dollar` (what was
asked for) alongside `actual_risk_pct`/`actual_risk_dollar` and
`binding_constraint` (which cap actually decided the size) — so "why is
actual risk so far below target" is answerable directly from the journal,
not hidden by only logging the target. If the notional caps are ever
raised for a real reason, `binding_constraint` will start reporting
`risk_target` instead of `notional_cap`, which is the signal that this
formula has started to actually matter.

## Execution cadence

Runs via `.github/workflows/crypto-scalper.yml` (GitHub Actions), on a
**5-minute cron** (`*/5 * * * *`), not Claude's Routines/Schedule system —
that platform enforces a hard 1-hour minimum interval, which ruled it out
for this agent (discovered 2026-08-05 when a 1-minute cadence was
originally requested; GitHub Actions' own stated floor is 5 minutes, and
even that isn't guaranteed exact under load — this is the closest reliable
approximation available on either platform). `workflow_dispatch` is also
enabled for manual test runs from the Actions tab. A `concurrency` group
prevents overlapping runs if one firing takes longer than 5 minutes.

Crypto trades 24/7 (no market-hours gate like the equities bot), so this
runs around the clock. Since the buy signal is computed on 5-minute bars
anyway, a 5-minute cadence loses essentially no signal freshness on entries
versus a tighter poll — it mainly trades off faster **exit** reaction
(stop-loss/take-profit/timeout would be caught within ~1 minute at a
1-minute cadence, vs. up to 5 here). It does not loosen any risk cap —
daily/weekly notional, per-trade %, max-positions, no-averaging-in, and
duplicate-order protection are all frequency-independent and enforced the
same regardless of how often this fires.

Credentials (`CRYPTO_APCA_API_KEY_ID` / `CRYPTO_APCA_API_SECRET_KEY` /
`CRYPTO_APCA_BASE_URL`) are GitHub Actions repository secrets on
`fabjon14-cmd/Jarvis-trader` — a different secret store from the equities
bot's credentials, which live in Claude's cloud Environment used by its
Routines. Set them via `gh secret set NAME --repo fabjon14-cmd/Jarvis-trader`
or the repo's Settings → Secrets and variables → Actions page. This is a
paper-account setup; revisit every part of it (cadence, caps, credential
location) before ever considering a live account.

## Journal format

One file per day: `journal/YYYY-MM-DD.md`, separate from the equities bot's
`../journal/`. Each run appends a `## Run YYYY-MM-DD HH:MM UTC` section
(handled automatically by `scalper_agent.py`) rather than overwriting prior
runs. Every decision — buy, hold, or exit — gets logged with the specific
number behind it (`rsi=24.1`, `crossed_above_ema20=true`,
`unrealized_plpc: -0.81%`), same audit-trail standard as the equities bot:
"why did it hold at 14:15" should be answerable directly from the journal.

**JSON decision envelope (added 2026-08-05, at the operator's request):**
in addition to the plain-English line per pair above, each run appends a
single fenced ` ```json ` block containing an array with one envelope per
watchlist pair — every pair, every run, whether the action was `HOLD`,
`NEW_TRADE`, or `CLOSE`. Each envelope (see `research.build_decision_envelope`)
contains:
1. `portfolio` — cash and net liquidation value at decision time.
2. `positions` — open position count and notional exposure by category.
3. `technical_validation` — RSI(14) and EMA(20) on 5-minute bars,
   EMA(20)/EMA(50) + alignment on 1-hour bars, ATR(14) + its 20-period
   average and whether it's spiking (5-minute).
4. `invalidation_price` — the hard stop-loss price level (entry price ×
   (1 − stop-loss %)) for this pair's position, or `null` if no position is
   open. This is the price the strategy would need to trade back to for the
   stop-loss rule above to fire — not a recalculation, just cost basis run
   through the same fixed percentage.

This is the strict, parseable record; the plain-English line above it is
for a human skimming the file — both describe the same decision.

If this runs as a scheduled cloud routine, commit and push the journal file
at the end of each run — each routine starts from a fresh clone.

## Weekly review (added 2026-08-05)

`.github/workflows/crypto-scalper-review.yml` runs `scripts/review.py write`
every Friday at 20:30 UTC (matching the equities bot's review cadence),
writing `reviews/YYYY-MM-DD.md`. It computes, from Alpaca's own order and
portfolio history — not from re-reading the journal's prose:
- Orders placed/filled/rejected over the period.
- Closed round-trips (buy+sell pairs, single-entry-per-pair makes this a
  simple match rather than a FIFO queue), win rate, and realized P&L.
- Account equity change over the period vs. a BTC/USD buy-and-hold
  benchmark over the same window — same "out/underperformed by N points"
  framing as the equities bot's SPY comparison.
- Trend vs. the most recent prior review on file, if any.

**What this deliberately does NOT do:** the equities bot's weekly review
also has a "Rule adherence" section — an LLM re-reading the journal's prose
against CLAUDE.md's rules to flag violations. That review runs as a full
Claude cloud agent (a Routine); this one runs as a plain Python script on a
GitHub Actions cron, with no LLM in the loop, so it can't do that
qualitative read. The output says so explicitly rather than silently
omitting it. If a qualitative review is wanted later, that's a different
execution model (an actual agent run, like the equities Routines) — not
something to fake with more Python.

## Hourly digest (added 2026-08-05)

`.github/workflows/crypto-scalper-hourly-digest.yml` runs
`scripts/hourly_digest.py` at the top of every hour. It reads Alpaca's own
order history (not the journal) for orders in the last 60 minutes,
excluding rejected/canceled/expired ones — a rejection means it did NOT
buy or sell, which isn't the "did it trade" signal this is for. If nothing
filled in that window, **it sends nothing** — a deliberate choice (operator
decision, 2026-08-05) over an always-send heartbeat, since most hours have
had zero trades so far and an empty-every-hour email would mostly be noise.
Email only (SMS/WhatsApp were considered and declined — both need a new
paid third-party account, while email reuses the existing free Resend
setup already wired into `../scripts/notify.py`).

Needs `RESEND_API_KEY` and `REPORT_TO_EMAIL` as GitHub Actions repository
secrets — the same Resend account as the equities bot works fine, but the
key has to be re-added here specifically, since the equities bot's copy
lives in Claude's Routines environment, not GitHub Actions secrets. Set
via `gh secret set RESEND_API_KEY --repo fabjon14-cmd/Jarvis-trader` and
`gh secret set REPORT_TO_EMAIL --repo fabjon14-cmd/Jarvis-trader`.

## Backtesting (added 2026-08-05)

`scripts/backtest.py`, run on demand via `.github/workflows/crypto-scalper-backtest.yml`
(`workflow_dispatch` only — not scheduled). Replays the exact live signal
logic (RSI(14)<30 + EMA20 cross-above + EMA20>EMA50 + no ATR spike) and
exit logic (+1.5% TP / -0.75% SL / 4h timeout) bar-by-bar over historical
5-minute candles, with **no look-ahead**: at bar i, only `bars[0..i]` is
used to decide anything about bar i, and exit checks for a position only
start from the bar *after* it was entered.

**Scope — read before trusting the output:**
- Signal-only. Does NOT simulate the daily/weekly notional caps,
  max-positions cap, category cap, or RSI-weighted cross-pair allocation —
  those govern how much capital gets deployed, not whether an individual
  trade wins or loses. Results are per-trade **% return**, not dollar P&L.
- If both TP and SL are touched within the same historical bar, OHLC data
  alone can't tell you which happened first — the backtest conservatively
  assumes the worse outcome (stop-loss) hit first, standard practice to
  avoid overstating results.
- RSI/EMA/ATR are computed from a rolling last-150-bar window at each
  step, not the full history since account inception — a standard
  approximation given EMA's exponential decay (bars further back than
  ~150 contribute negligibly to EMA(50) anyway), done for runtime
  (recomputing indicators over the full growing history at every one of
  tens of thousands of bars would be far slower).
- The take-profit exit applies the same 0.1% limit-order slippage buffer
  as live trading; the stop-loss exit uses the exact stop price (matching
  live's market-order guaranteed-fill design).

This answers "does the signal itself have edge" — the necessary first
question — not "would this exact bot, with its exact budget caps, have
made money." A budget-constrained simulation (also modeling the caps and
allocation) would be a heavier follow-up if the signal-only result looks
promising enough to justify it.

Also added the same day: `scripts/diagnose_signal.py` (counts how often
each individual buy condition is true, separately, to find which one is
the actual bottleneck when the combined signal fires rarely or never —
not part of the permanent pipeline) and, on the aggregate backtest,
`avg_return_by_exit_reason`, per-pair `exit_breakdown`, an `exclude_pairs`
option (test dropping a pair without touching the live watchlist), and a
`detail PAIR` mode (full trade-by-trade list for one pair).

### AVAX/USD removed from the watchlist (2026-08-05)

The 60-day backtest (after both signal fixes above) showed the full
8-pair watchlist losing -8.37% against a +1.97-2.01% BTC buy-and-hold
benchmark. `exclude_pairs=AVAX/USD` on the same window flipped this to
**+4.48%** — outperforming buy-and-hold — with max drawdown roughly
halved (-16.66% → -8.11%). AVAX/USD alone was hitting stop-loss on 72.5%
of its trades (29/40) versus a 47% stop-loss rate across the other seven
pairs; its actual volatility doesn't fit the fixed 0.75% stop tuned
around the rest of the watchlist, so "good" entries kept getting shaken
out by normal noise before the reversal could play out. Not a close
call — one pair was solely responsible for turning a profitable result
into a losing one. `categories.json` was updated in the same change
(Layer1 category still has BTC/ETH/SOL/DOT, unaffected by the removal).
If AVAX-like volatility comes up again for a different pair, the fix
would be a per-pair stop distance, not another removal — but that's a
larger change than was justified here for one data point.

### Out-of-sample validation exposed the real problem (2026-08-06)

Added `end_days_ago` to `scripts/backtest.py` to test the current config
against a window that had no part in tuning it (e.g. `lookback_days=60,
end_days_ago=60` tests the 60 days ending 60 days ago). Result: the
config that scored +4.48% on the most recent 60 days lost **-23.91%** on
the prior 60 days, underperforming BTC buy-and-hold (-11.82% that
window) by ~12 points. The AVAX conclusion didn't generalize either — in
this earlier window, **DOT/USD** showed the identical pattern AVAX
showed in the other one (74% stop-loss rate, clear worst performer).
AVAX wasn't uniquely broken; it was just whichever pair happened to be
most volatile relative to a flat percentage stop in the window being
looked at. Removing pairs one at a time as they show up doesn't fix
that — a different pair takes the "worst performer" role in a different
period.

### ATR-based stop-loss/take-profit, replacing the flat percentages (2026-08-06)

Fixes the actual mechanism behind both findings above:
`research.compute_atr_based_stop_tp_pct()` sizes each trade's stop/TP off
**that pair's own ATR(14) at entry time**, not one flat 0.75%/1.5% for
every pair regardless of how volatile it naturally is —
`CRYPTO_ATR_STOP_MULTIPLIER`/`CRYPTO_ATR_TP_MULTIPLIER` (currently 6x/12x
— see "ATR multiplier calibration" below for why that number and not the
originally-shipped 1.5x/3x, and why neither is actually validated;
preserves the original 2:1 reward:risk ratio) control the sizing. A
naturally choppier pair gets proportionally more room before being
called a "stop-out"; a calmer pair gets a tighter one — so the same
relative risk applies to whichever pair happens to be volatile in a
given period, instead of that pair repeatedly getting shaken out by its
own normal noise.

**How this is persisted per trade:** Alpaca doesn't offer a way to
attach custom metadata to a position, and unlike the old flat globals,
each trade's stop/TP now genuinely differs — so `research.
encode_trade_params()`/`decode_trade_params()` pack the entry-time
stop/TP into the buy order's `client_order_id` (e.g. `cs-sl35-tp70` =
0.35% stop, 0.70% target), which `trade.place_order()` passes straight
to Alpaca. `get_exit_flags()` recovers it from the entry order on every
run via `get_position_trade_params()`. This follows the same "Alpaca's
own order history is the source of truth, not local state that could
desync" principle used everywhere else in this project (duplicate-order
protection, position entry time, etc.) — no separate state file to keep
in sync or lose on a crash. A position with no recoverable
`client_order_id` (opened before this feature existed, or placed
manually/interactively without it) falls back to the flat
`STOP_LOSS_PCT`/`PROFIT_TARGET_PCT` defaults.

The backtest mirrors this exactly: at entry, it computes stop/TP from
that bar's own ATR and stores it on the in-memory `position` dict for
the rest of that trade's simulated life — no persistence complexity
there since a single `simulate_pair()` call already holds everything in
memory.

### ATR multiplier calibration — read this before trusting any live result (2026-08-06)

**Nothing tested this day generalized across both the recent 60-day
window and an earlier out-of-sample 60-day window.** In order:

| Config | Recent 60d | Out-of-sample 60d |
|---|---|---|
| Flat 0.75%/1.5%, 8 pairs | -8.37% | not tested |
| Flat, AVAX excluded | +4.48% | -23.91% |
| ATR 1.5x/3x (tight) | -8.02% | not tested |
| ATR 6x/12x (wide) | +40.7% | -22.21% |
| ATR 6x/12x + BTC daily-EMA regime filter | 0 trades (filter blocked everything) | -30.98% (filter let it all through, worse) |

The regime filter (`get_market_regime()`, still in `research.py` but
**not wired into live trading**) failed in the most telling way: it
blocked every trade in the calm/profitable window and barely blocked
anything in the actual decline — a daily EMA20/50 cross is too laggy in
both directions to gate on. Five different configurations, five
different failure modes, all following the same shape: good on whichever
window they were tuned against, bad on the other one. That pattern is
itself the finding — this points at the underlying RSI+EMA+ATR signal
not having a durable statistical edge, not at any one parameter being
miscalibrated.

**6x/12x is the current live default because the operator explicitly
chose it (2026-08-06) after this finding** — specifically as "the
config that wasn't actively losing in the most recent backtest," to run
live for a day of observation, NOT because it's validated. 1.5x/3x
wasn't re-tested out-of-sample, so this isn't even a clean "6x/12x beat
1.5x/3x" comparison. Do not read confidence into short-term live results
under this config without re-reading this section first. If revisiting
this strategy, the honest next step is a proper multi-window/walk-forward
validation process — treat any single day or single 60-day backtest
window as anecdote, not evidence, given what happened here.

### Regime classification check + trend-following alternative — the real answer (2026-08-06)

Following up on "the honest next step is proper multi-window validation"
above, immediately the same day: built `simulate_pair_trend()` (a
from-scratch, opposite-philosophy strategy — buy breakouts above the
1-hour EMA20 with 1-hour AND daily uptrend both required, no RSI-oversold
gate at all, wide 20x-ATR take-profit and 24h max hold instead of tight
symmetric targets — "cut losses short, let winners run") and
`regime_check()` (Kaufman's Efficiency Ratio: net move ÷ total path
length — 1.0 = pure trend, 0.0 = pure chop), then ran both strategies
plus the regime metric across **8 independent, non-overlapping 60-day
windows spanning 480 days**, the full history available across all 7
watchlist pairs (SOL/USD's Alpaca listing is the limiting factor — data
doesn't go back meaningfully further than ~480 days for it, confirmed by
checking `bars_fetched_per_pair` at increasing depth before committing
to this window count).

| end_days_ago | Efficiency Ratio | BTC net | Mean-reversion | Trend-following |
|---|---|---|---|---|
| 0 (most recent) | 0.036 | +2.8% | +40.7% | -36.2% |
| 60 | 0.128 | -11.8% | -23.2% | -0.9% |
| 120 | 0.010 | +1.1% | -61.6% | -40.3% |
| 180 | 0.253 | -24.8% | -26.4% | -45.1% |
| 240 | 0.165 | -16.3% | -48.7% | -70.0% |
| 300 | 0.075 | -5.9% | -22.6% | -67.1% |
| 360 | 0.182 | +11.9% | -26.9% | -24.5% |
| 420 | 0.271 | +25.1% | -8.9% | -55.3% |

**Averages: mean-reversion -22.2%/window (1 of 8 profitable), trend-following
-42.4%/window (0 of 8 profitable), BTC buy-and-hold -2.25%/window.**

Two conclusions, both load-bearing for the pause at the top of this file:

1. **The regime hypothesis doesn't hold.** The single most choppy window
   (ER=0.010, "should" favor mean-reversion) was mean-reversion's *worst*
   result, not its best. No clean relationship between efficiency ratio
   and which strategy wins — this isn't "need more data to see the
   pattern," 8 independent windows is enough to say the naive
   trend-vs-chop theory doesn't predict outcomes here.
2. **Trend-following is not a fix — it's worse**, nearly 2x the average
   loss of mean-reversion. Building an opposite-philosophy strategy from
   scratch and testing it properly (not just tuning the existing one
   further) was the actual test of "is there a real edge hiding in a
   different signal shape," and the answer came back no.

This is why trading is paused rather than the live config being tuned
again: this was a large-sample (8 windows, ~1,800+ total simulated
trades across both strategies), multi-strategy result, not a single
lucky or unlucky backtest. Re-litigating this needs a genuinely
different data source or strategy class — not another indicator
combination on the same 5-minute crypto bars — and that's a
multi-session research undertaking, not a same-day parameter search.
`simulate_pair_trend()`, `regime_check()`, and `compute_efficiency_ratio()`
remain in `scripts/backtest.py` for that future work.

### Watchlist expansion to 19 pairs (2026-08-06)

At the operator's request to widen how many pairs get scanned each run
(the original 7-pair watchlist meant most 5-minute cycles found zero
qualifying candidates simply from small sample size). Target was 40
pairs; landed at 19 for two reasons documented here so a future reader
doesn't assume 19 was an arbitrary shortfall:

1. **Alpaca's actual paper-crypto lineup doesn't have 40 tradable,
   non-stablecoin pairs to begin with.** Its crypto support has been a
   stable, well-documented set of roughly 20-odd pairs for a while —
   there's no deeper bench to expand into regardless of how the
   selection is made.
2. **Live verification against this specific account failed twice.**
   Added `scripts/list_assets.py` and a `crypto-scalper-list-assets.yml`
   on-demand workflow specifically to query `GET /v2/assets?asset_class=
   crypto` and confirm the exact tradable list on this account before
   trusting it, rather than guessing. Both `workflow_dispatch` runs sat
   queued for ~15 minutes and were auto-cancelled by GitHub with zero
   steps ever executing — a GitHub Actions runner-allocation stall (the
   same platform behavior that's made today's 5-minute cron land at
   15-45+ minute gaps instead), not a bug in the script. Given the
   operator's "do it" instruction to proceed without further back-and-
   forth, the list below was built from Alpaca's well-established public
   documentation instead of a live-confirmed query.

**Added (12):** LTC/USD, XTZ/USD, AAVE/USD, MKR/USD, YFI/USD, SUSHI/USD,
CRV/USD, SHIB/USD, BCH/USD, XRP/USD, GRT/USD, BAT/USD — alongside the
original 7 (BTC/USD, ETH/USD, SOL/USD, DOT/USD, LINK/USD, UNI/USD,
DOGE/USD), for 19 total. `categories.json` was extended with three new
categories to keep the sector cap meaningful across a wider set:
`Payments` (BCH/USD, XRP/USD), `Infrastructure` (GRT/USD), `Utility`
(BAT/USD) — DeFi and Layer1 absorbed most of the new adds since that's
where Alpaca's actual lineup is concentrated.

**Deliberately excluded:**
- **USDC/USD, USDT/USD** — stablecoins. An RSI/EMA momentum-scalp signal
  is meaningless against an asset pegged to $1; including them would
  just waste scan cycles and occupy a category slot for no reason.
- **AVAX/USD** — left out, not re-litigated. It was removed 2026-08-05
  for blowing through the old flat 0.75% stop on its own volatility (see
  "AVAX/USD removed from the watchlist" above). The ATR-based stop/TP
  redesign added the next day arguably addresses the exact mechanism
  that got AVAX removed, so this exclusion may no longer be justified —
  but re-adding it is a separate, deliberate call this expansion didn't
  make on its own, since it wasn't what was asked.

**If a symbol here turns out not to be tradable on this account**, it
fails closed under the existing API/data-failure rule (a failed check ⇒
hold, log which check failed) — not a functional risk, just a wasted
cycle for that pair until corrected. `scripts/list_assets.py` and its
workflow remain in the repo to get the real, account-verified list once
GitHub Actions stops stalling — worth re-running that before trusting
this list is 100% accurate.

**No other risk parameter changed.** More pairs scanned is not more risk
exposure — `CRYPTO_MAX_POSITIONS` (5), `CRYPTO_MAX_PER_CATEGORY` (2), and
the daily/weekly notional caps are unchanged and still bound total
exposure regardless of watchlist size.

## Setup notes

- Requires a **second, separate** Alpaca paper account — do not reuse the
  equities bot's `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`. Create a new paper
  account (or a new key pair, if Alpaca is set up to support multiple paper
  accounts on one login) at
  https://app.alpaca.markets/paper/dashboard/overview, and set
  `CRYPTO_APCA_API_KEY_ID` / `CRYPTO_APCA_API_SECRET_KEY` /
  `CRYPTO_APCA_BASE_URL` in the repo-root `.env` (see `.env.example`).
- If orders reject with an asset-not-tradable error, crypto trading may need
  to be enabled on that Alpaca paper account via the Alpaca dashboard —
  that's an account setting, not something these scripts control.
- Other new env vars (see root `.env.example`): `CRYPTO_SCALPER_UNATTENDED`,
  `CRYPTO_MAX_ORDER_NOTIONAL`, `CRYPTO_MAX_ORDERS_PER_RUN`,
  `CRYPTO_DAILY_NOTIONAL_CAP`, `CRYPTO_WEEKLY_NOTIONAL_CAP`,
  `CRYPTO_MAX_POSITIONS`, `CRYPTO_MAX_PER_CATEGORY`, `CRYPTO_PER_TRADE_PCT_CAP`,
  `CRYPTO_MAX_HOLD_HOURS`, `CRYPTO_PROFIT_TARGET_PCT`, `CRYPTO_STOP_LOSS_PCT`,
  `CRYPTO_ATR_SPIKE_MULTIPLE`, `CRYPTO_TARGET_RISK_PCT`,
  `CRYPTO_ATR_STOP_MULTIPLIER`, `CRYPTO_ATR_TP_MULTIPLIER`,
  `CRYPTO_RSI_LOOKBACK_BARS`.
