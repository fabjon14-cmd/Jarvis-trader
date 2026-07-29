# Trader — Project Notes

Paper-trading equities agent. Account is a sandbox (fake money) Alpaca account —
`APCA_BASE_URL` defaults to `https://paper-api.alpaca.markets` in
`scripts/research.py` and `scripts/trade.py`. Do not point it at a live
account without deliberately deciding to.

## Tools available

- `scripts/research.py` — `account | positions | bars SYMBOL | news SYMBOL | orders [STATUS] | portfolio [PERIOD] | earnings SYMBOL | movers [TOP]` (read-only)
- `scripts/trade.py` — `status | order SYMBOL QTY SIDE [LIMIT_PRICE] | cancel`
- `scripts/notify.py "SUBJECT" FILE_PATH` — emails FILE_PATH's contents as the body via Resend, to `REPORT_TO_EMAIL`. Run this as the last step of every scheduled routine, pointing at the file (or section) it just wrote, so the operator gets the full report by email, not just a "ran" ping.
- `watchlist.json` — the list of symbols in scope; don't trade outside it without being told to.

`scripts/trade.py`'s `place_order` pauses for a human y/N confirmation when run
interactively. Under `TRADER_UNATTENDED=1` (scheduled/cloud runs) it
auto-approves instead, but only accepts limit orders and caps notional/count
per run (`TRADER_MAX_ORDER_NOTIONAL`, default $2,000; `TRADER_MAX_ORDERS_PER_RUN`,
default 10). A rejection under those caps is expected behavior, not a bug —
report it in the journal rather than retrying with adjusted numbers.

## Trading rules

These are the operator's deliberately chosen rules (finalized 2026-07-29 after
reviewing a live test run) — not placeholders, and not financial advice, just
this bot's configured risk parameters.

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
  symbol dropping hard with no stabilization signal (e.g. news of a bottom,
  reversal in the last session or two) is a hold, not a discount.
- Selling is allowed, not just buying/holding: if new research turns clearly
  negative on a symbol you currently hold (deteriorating fundamentals, a bad
  earnings print, a broken thesis — not just short-term volatility), sell or
  trim the position. State the specific change in research that justifies it;
  don't sell on noise.
- If research is inconclusive or stale (missing/failed fetch), hold — don't guess.

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
