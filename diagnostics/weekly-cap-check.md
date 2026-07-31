# Weekly notional cap check — 2026-07-31

Diagnostic only. No orders placed, no other files touched.

## Results

Env var check:
```
TRADER_WEEKLY_NOTIONAL_CAP = 3000
```

`scripts/research.py deployed`:
```json
{"daily_deployed": 0.0, "weekly_deployed": 1705.0}
```

## Verdict

Cap is live at $3000 — `TRADER_WEEKLY_NOTIONAL_CAP` resolves to `3000`, not the
code default of `1000`. Current weekly deployed notional is $1,705.00,
consistent with the raised $3,000 trial cap (it would already be blocking new
buys under the old $1,000 default, since $1,705 > $1,000).
