# Staged demo entries

`SMART_PAIR_ENTRIES=2` permits at most two journal-owned positions, on one
market and in one direction. The authorized demo trial defaults to this mode;
other accounts retain the flat-account default (`SMART_PAIR_ENTRIES=1`).

Each entry keeps the 0.125% equity risk ceiling. Both positions share a 0.25%
group ceiling, using the lower of current equity and the original group equity.
The existing leg reserves the larger of planned loss, actual fill loss and
current equity-to-stop downside, using conservative currency conversion.
The daily risk allowance is also shared. Combined estimated margin is bounded
by 10% of current free margin. Broker minimum sizes can prevent an addition.

The second entry requires a later closed M15 candle, a new positive assessment,
the same direction, and an existing leg that has moved favourably beyond its
executable entry price. News, technical, spread, freshness and daily gates still
apply. Unknown positions, working orders, changed protection, incomplete legacy
plans or an uncertain order prevent additions. Entries and protective closures
are serialized with account reconciliation. Restarts do not erase reservations.

The adapter reads and verifies hedging mode and current leverage. It records
the actual leverage of each confirmed position and uses each existing position's
own leverage to estimate margin. It never sends account preference updates.
An analysis assessment is not a calibrated target-hit probability. Probability
based leverage increases remain disabled pending independent validation.

Startup logs show `entry_policy=staged_owned_same_pair`, `pair_entries=2` and
`pair_risk_fraction=0.0025`. Confirmed entry plans include combined planned and
actual risk, margin, signal candle and leverage. A gate veto does not force an
order. Stops and exposure ceilings are planning controls, not guaranteed fills
or loss limits during gaps. Trial duration and paid-analysis budgets are unchanged.
