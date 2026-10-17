# Deterministic demo rules

The approved demo trial defaults to `SMART_DECISION_ENGINE=rules`. No OpenAI
or EODHD request is made by this engine. An explicit `ai` setting preserves
the previous budgeted AI route. The original trial identity and its Friday
deadline still apply; switching engines never resets the ledger or authorizes
another account.

Each closed M15 candle is evaluated once per market. The existing trend
pullback/retest and bounded range edge rejection setups supply direction,
invalidation, observed target and an assumed slippage stress. A signal must
pass the independently recalculated candle structure, executable bid/ask
reward/risk of at least 2:1, Capital demo account checks, daily and grouped
risk limits, ownership reconciliation, and broker confirmation. The news feed
is not a prerequisite for this price-only mode. A missing or stale Capital
quote, invalid candles, insufficient structure or exhausted trial still blocks.

Log `rules_evaluated` records a deterministic decision for each market/candle;
`entry_wait`, `entry_blocked`, `entry_confirmed` and `trial_status` show the
next gate. `ready` expresses only that a rule was met, not a calibrated win
probability or demonstrated profitability. Compare fills, costs, drawdowns,
and time in market against the prior AI trial before any live use.
