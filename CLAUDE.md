# Trader — Project Notes

Paper-trading equities agent. Account is a sandbox (fake money) Alpaca account —
`APCA_BASE_URL` defaults to `https://paper-api.alpaca.markets` in
`scripts/research.py` and `scripts/trade.py`. Do not point it at a live
account without deliberately deciding to.

## Tools available

- `scripts/research.py` — `account | positions | bars SYMBOL | news SYMBOL` (read-only)
- `scripts/trade.py` — `status | order SYMBOL QTY SIDE [LIMIT_PRICE] | cancel`
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
  a separate call (see selling rule below), this only blocks new buys.
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
