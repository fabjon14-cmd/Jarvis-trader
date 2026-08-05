# Crypto Scalper — Project Notes

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
- `watchlist.json` — crypto pairs in scope: `BTC/USD`, `ETH/USD`, `SOL/USD`, `AVAX/USD`, `DOT/USD`, `LINK/USD`, `UNI/USD`, `DOGE/USD` (expanded from the original 3 on 2026-08-05 so the category cap below has more than one category to actually enforce across). Don't trade pairs outside it without being told to.
- `categories.json` — category per pair (`Layer1`, `DeFi`, `Meme`), backing the category cap below. Mirrors the equities bot's `sectors.json` pattern.
- `../scripts/notify.py` (shared with the equities bot) — email the day's journal. Call this **at most once a day** (e.g. after the last scheduled firing), not every run — this agent fires far more often than the equities bot's hourly cadence, and per-run emails would be spam.
- `scripts/hourly_digest.py` — emails a summary of any buy/sell activity in the last hour, via the shared `notify.py`. Sends nothing if there was no activity. Scheduled hourly via `.github/workflows/crypto-scalper-hourly-digest.yml`. See "Hourly digest" below.

`scripts/trade.py`'s `place_order` pauses for a human y/N confirmation when
run interactively. Under `CRYPTO_SCALPER_UNATTENDED=1` (scheduled/cloud runs)
it auto-approves instead, but only accepts limit orders and enforces every
cap below in code. A rejection under those caps is expected behavior — log
it, don't retry with adjusted numbers.

## Strategy — precisely

**Buy trigger:** on the 5-minute timeframe, ALL of the following must be
true on the same completed bar (expanded 2026-08-05 from RSI+EMA20 alone,
at the operator's request, to add trend context and a volatility filter):
- RSI(14) < 30 (Wilder's smoothing, standard 14-period).
- The close **crosses above** EMA(20) — the previous bar's close was at or
  below its EMA, and this bar's close is above its EMA. A pair already
  trading above its EMA with no cross event does not qualify; this is a
  cross detector, not a level check. (Same precision principle as the
  equities bot's stabilization-signal definition — a fuzzy "looks like it's
  turning" is not verifiable, a cross event is.)
- **EMA(20) > EMA(50)** ("bullish alignment") — the short-term trend is
  above the long-term trend, so this is a dip-buy within an established
  uptrend, not a bottom-call against the broader trend. Both EMAs computed
  from the same 5-minute closes.
- **No ATR volatility spike:** ATR(14) (Wilder-smoothed, from the same
  5-minute OHLC bars) is not more than `CRYPTO_ATR_SPIKE_MULTIPLE` (default
  2x) its own trailing 20-period average. RSI and EMA behave unreliably
  during a volatility/news shock — a spike blocks the signal regardless of
  what RSI/EMA say, same "don't trust a check that can't be verified right
  now" principle as the API-failure handling below.

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

### Even-split allocation across simultaneous signals (added 2026-08-05)

Per the operator's request that the strategy not "focus on investing in
one" pair: if more than one watchlist pair qualifies for a buy in the
SAME run, the remaining daily/weekly headroom is split evenly across all
of them, not given first-come-first-served to whichever pair happens to
be earlier in `watchlist.json` (BTC/USD is always first in that file, so
without this it would have had a structural, order-of-iteration advantage
with no basis in signal strength). `scalper_agent.py`'s `run()` does this
in two passes: pass 1 evaluates every pair's signal and collects which
ones qualify without sizing or placing anything yet; pass 2 divides
`headroom / len(candidates)` and sizes each qualifying pair against
`min(MAX_ORDER_NOTIONAL, per_trade_cap, split_headroom)` — so a single
qualifying pair still gets the full normal per-trade cap (split among 1
is just itself), and only sharing with others actually divides the
budget.

This addresses within-run concentration. It does NOT, on its own, prevent
one pair from qualifying more often than others across many runs over
time (e.g. if DOGE/USD's price action happens to trip the RSI/EMA/ATR
conditions more frequently than BTC/USD does) — that's the mechanical
signal doing its job, not a bug, and the existing category cap (max 2 per
category) and 5-position cap already bound how much of the account any
one pair or category can occupy at once regardless of how it got there.

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
3. `technical_validation` — RSI(14), EMA(20)/EMA(50) + alignment, ATR(14)
   + its 20-period average and whether it's spiking.
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
  `CRYPTO_ATR_SPIKE_MULTIPLE`, `CRYPTO_TARGET_RISK_PCT`.
