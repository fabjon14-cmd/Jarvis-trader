# Trader — Project Notes

Paper-trading equities agent. Account is a sandbox (fake money) Alpaca account —
`APCA_BASE_URL` defaults to `https://paper-api.alpaca.markets` in
`scripts/research.py` and `scripts/trade.py`. Do not point it at a live
account without deliberately deciding to.

## Tools available

- `scripts/research.py` — `account | positions | bars SYMBOL | news SYMBOL | orders [STATUS] | portfolio [PERIOD] | earnings SYMBOL | movers [TOP] | circuit-breaker | stop-loss | sector SYMBOL | deployed` (read-only)
- `scripts/trade.py` — `status | order SYMBOL QTY SIDE [LIMIT_PRICE] | cancel`
- `scripts/notify.py "SUBJECT" FILE_PATH` — emails FILE_PATH's contents as the body via Resend, to `REPORT_TO_EMAIL`. Run this as the last step of every scheduled routine, pointing at the file (or section) it just wrote, so the operator gets the full report by email, not just a "ran" ping.
- `watchlist.json` — the list of symbols in scope; don't trade outside it without being told to.
- `sectors.json` — GICS-style sector for each watchlist symbol, backing the sector cap below.

`scripts/trade.py`'s `place_order` pauses for a human y/N confirmation when run
interactively. Under `TRADER_UNATTENDED=1` (scheduled/cloud runs) it
auto-approves instead, but only accepts limit orders and caps notional/count
per run (`TRADER_MAX_ORDER_NOTIONAL`, default $2,000; `TRADER_MAX_ORDERS_PER_RUN`,
default 10). A rejection under those caps is expected behavior, not a bug —
report it in the journal rather than retrying with adjusted numbers.

For buy orders specifically, `place_order` also hard-enforces the circuit
breaker, the daily/weekly notional cap, and the sector cap below — these are
code checks, not something a well-reasoned buy can talk its way past. If one
of them rejects an order, that's the rule working as designed; log it and
move on, same as any other capped rejection.

## Trading rules

These are the operator's deliberately chosen rules (finalized 2026-07-29 after
reviewing a live test run, expanded 2026-07-30 with mechanical risk controls
after the AAPL earnings-window incident showed prose-only rules can get
reasoned around) — not placeholders, and not financial advice, just this
bot's configured risk parameters.

- Only trade symbols in `watchlist.json`.
- Max position size: 10% of buying power per symbol. Max 5 open positions at once.
- Limit orders only in unattended runs (enforced in code, see above).
- No adding to a losing position ("averaging down").
- Do not open a new position within 48h of a symbol's earnings report — event
  risk without a directional edge is a hold, not a bet. Existing positions are
  a separate call (see selling rule below), this only blocks new buys. Check
  this with `scripts/research.py earnings SYMBOL` (`within_48h` field) before
  every new buy — don't infer earnings timing from news headlines, that's how
  the AAPL buy on 2026-07-29 slipped through despite the risk being noted in
  the same journal entry.
- Do not buy into a sharp, uncorroborated downtrend ("falling knife") — a
  symbol dropping hard with no stabilization signal is a hold, not a discount.
  **Stabilization signal, precisely:** at least 2 consecutive daily closes at
  or above the previous close. (Not ATR — this is the simpler, directly
  verifiable definition from the bars data already being pulled; use it
  consistently rather than switching to volatility-based judgment case by case.)
- Selling is allowed, not just buying/holding: if new research turns clearly
  negative on a symbol you currently hold, sell or trim the position.
  **Clearly negative, precisely:** a named, dated catalyst — a downgrade, a
  guidance cut, a lost contract, a bad earnings print, confirmed bad news.
  "Sentiment feels worse" or "price is down" do not qualify on their own;
  cite the specific catalyst in the journal entry.
- If research is inconclusive or stale, hold — don't guess. **Stale,
  precisely:** any research input older than 5 trading days, or predating a
  material news event for that symbol (earnings, guidance change, M&A,
  regulatory action) — whichever is more recent.

### Portfolio circuit breaker

If total portfolio equity has dropped more than 4% intraday, or more than 8%
over the trailing 5 trading days, halt all new buy orders for the rest of
that check — existing sell/trim actions (including the stop-loss rule below)
are still allowed and expected. Checked automatically: `place_order` calls
`scripts/research.py circuit-breaker` before any buy and hard-rejects if
halted, so this cannot be reasoned past — but check it explicitly yourself
too (`scripts/research.py circuit-breaker`) before deciding whether to even
evaluate new buys this run, so the journal reflects an intentional decision
rather than a run of rejected orders.

### Per-position stop-loss

Mechanical, fires on price alone, independent of the "clearly negative
research" selling rule above — a position can hit this with no news at all.
Check `scripts/research.py stop-loss` at the start of every Trading Session
run: if it flags a position, you MUST act on it (not "may"), same run:
- **-15%** from cost basis → trim the position to half its current size.
- **-25%** from cost basis → close the position entirely.
Log the trigger level and the resulting order in the journal.

### Sector cap

No more than 2 of the (up to 5) open positions may be in the same sector
(per `sectors.json`) at time of purchase. Check `scripts/research.py sector
SYMBOL` before any new buy on a symbol you don't already hold — adding to an
existing position doesn't count against this, only opening a new one does.
Enforced automatically in `place_order` for buys, same as the circuit
breaker; a rejection here is the rule working, not a bug to route around by
picking a different sizing.

### Daily/weekly aggregate deployment cap

Max **$500** total buy notional per trading day, max **$1,000** per rolling
7-day window — tracked as a running total from actual order history
(`scripts/research.py deployed`), not reset by each fresh routine run. This
caps how much new capital the bot can commit regardless of how many Trading
Session firings happen in a day or how many symbols look attractive.
Enforced automatically in `place_order` for buys (`TRADER_DAILY_NOTIONAL_CAP`
/ `TRADER_WEEKLY_NOTIONAL_CAP` env vars, defaults $500 / $1,000). Sells are
exempt — this caps new risk, not reducing existing risk.

### Duplicate-order protection

Before submitting any unattended order, `place_order` checks Alpaca's own
order history (not in-memory state — that doesn't survive a crash) for an
order already placed today with the exact same symbol/side/qty/limit_price,
and refuses to re-submit if one exists. This exists specifically so that a
run which crashes after submitting an order but before journaling it, then
restarts and re-evaluates the same decision, doesn't double the position.
You don't need to do anything extra for this — it's automatic — but if a
placement comes back `"duplicate": true`, log it as "already placed, not
re-submitted," not as a rejection or an error.

### Confirm before treating an order as done

Submitting an order successfully is not the same as it being filled.
`place_order`'s return value tells you the real status Alpaca gave back
(`accepted`, `pending_new`, `rejected`, etc.) — read it, don't assume. If you
place an order and then need to make another sizing decision later in the
same run (e.g. evaluating the next symbol's 10%-of-buying-power cap), re-run
`scripts/research.py account` / `positions` and use the fresh numbers rather
than mentally subtracting what you think you just spent — Alpaca reserves
buying power on acceptance, before a fill, so the real numbers are already
available and more trustworthy than arithmetic.

### Audit trail

Every decision this run makes — buy, sell, hold, or held-because-capped —
gets logged, not just the trades that went through. For each symbol, name
the specific rule that drove the decision and the exact data point behind
it: `"within_48h: true"`, `"unrealized_plpc: -16.2%"`, `"sector cap: 2 already
held in Financials"`, `"no stabilization signal: last close -1.1% vs prior"`.
A vague "held on caution" is not enough — the goal is that "why did it hold
Tuesday" is answerable from the journal as directly as "why did it buy
Wednesday." This is what the weekly review actually checks against.

### API/data failure handling

If any check command errors, times out, or returns nothing usable — for any
symbol, for any of `earnings`, `circuit-breaker`, `stop-loss`, `sector`,
`bars`, or `news` — treat that symbol as unknown for this run and hold. Do
**not** fall back to inferring from news headlines, sentiment, or anything
else as a substitute for the failed check; that is the exact failure mode
that let the 2026-07-29 AAPL earnings-window violation through when the
earnings check didn't exist yet and news inference stood in for it. A
failed check is itself information — log which command failed and why you
held, the same as any other rule-driven hold.

## Journal format

One file per day: `journal/YYYY-MM-DD.md`. Each scheduled routine appends its
own section to the same day's file — create the file with all three headings
if it doesn't exist yet, and only fill in the section you're responsible for:

```markdown
# YYYY-MM-DD

## Research
(Morning Research fills this in: per-symbol summary of bars + news, anything notable.)

## Trading Session
(Trading Session fills this in: decision per symbol — buy/sell/hold — with
reasoning, and the outcome of any place_order call, including rejections.)

## End of Day Reflection
(End of Day Journal fills this in: final positions, account value, what
worked, what didn't, what to watch tomorrow.)
```

If this is running as a scheduled cloud routine, commit and push the journal
file at the end of your run — each routine starts from a fresh clone, so
uncommitted changes won't be visible to the next one.

## Weekly performance review format

One file per review, `reviews/YYYY-MM-DD.md` (dated the Friday it runs), covering
the trailing week (Mon–Fri). Ground the numbers in `scripts/research.py orders`
(actual fills) and `scripts/research.py portfolio` (equity curve) — don't just
re-summarize the journal's prose, compute real figures from these:

```markdown
# Week ending YYYY-MM-DD

## Performance
- Trades placed / filled / rejected this week (count each)
- Win rate: % of closed round-trips (buy+sell pairs, or a sell of a position
  opened this week) with positive P&L. State "N/A, no closed trades" if none.
- Realized P&L this week (from closed round-trips) and equity change over the
  week (from portfolio history) — these can differ if positions are still open.
- Largest win / largest loss, if any.

## Benchmark comparison
Pull SPY's price at the start and end of the review period via
`scripts/research.py bars SPY` and compute its % change over the same window
as the account's equity change above. State explicitly whether the account
out- or under-performed SPY this week, and by how much (in percentage points,
not just both numbers side by side). A flat/no-trade week still gets compared
— "we made no moves and SPY did X" is itself useful signal.

## Trend across weeks
Read every prior `reviews/*.md` file on this branch (there may be none yet
for early weeks — say so plainly rather than fabricating a trend from
nothing). Across however many weeks of history exist, note:
- Whether win rate and equity are trending up, down, or flat.
- Whether the account has out- or under-performed SPY cumulatively, not just
  this week.
- Whether any single rule (earnings window, falling-knife, sizing, averaging
  down) has been flagged as violated in more than one week — call this out
  explicitly by name if so, since a repeat violation is a rule-design problem,
  not a one-off mistake.

## Rule adherence
Re-read this week's Trading Session journal entries against the Trading rules
above. Flag any trade that looks like it violated a rule (sizing, earnings
window, falling-knife, averaging down) even if the reasoning at the time
seemed sound — this section is a check on the rules, not a re-justification
of what was already decided.

## Notes for next week
Anything the rules should maybe adjust, any open positions or pending orders
carrying into next week, anything to watch.
```

Same persistence rule as the journal: checkout `claude/trading-journal`, merge
`origin/main` first, commit and push this file there when done.

## Watchlist scout format

The scout only proposes candidates — it never edits `watchlist.json` itself.
Expanding the trading universe is the operator's call, not an automated one.

One file per run, `scout/YYYY-MM-DD.md`. Pull `scripts/research.py movers` for
today's top gainers/losers, drop any symbol already in `watchlist.json`, and
for each remaining candidate worth surfacing (use judgment — not every mover
is a real candidate, e.g. skip obvious one-off news spikes with no sustained
trend) pull `scripts/research.py bars SYMBOL` and `news SYMBOL` for a quick
read:

```markdown
# Watchlist scout — YYYY-MM-DD

## Candidates
For each candidate: symbol, why it showed up (gainer/loser, % move), a brief
bars + news read, and an explicit recommendation (add / watch / skip) with
reasoning. If nothing worth surfacing today, say so plainly rather than
padding the list.

## No changes made
State explicitly that watchlist.json was not modified — these are proposals
for the operator to review and act on manually.
```

Same persistence rule: checkout `claude/trading-journal`, merge `origin/main`
first, commit and push this file there when done.
