# Public-data desk analysis and cost-aware demo entries

The analyst follows a documented research process: higher-timeframe regime,
closed-bar structure, news evidence, invalidation, executable-price costs, an
opposing scenario and missing data. A label such as "positive" is not a win
probability. This is not a bank's proprietary strategy and has no demonstrated
profitable edge. See the BIS report on [FX execution algorithms](https://www.bis.org/publ/mktc13.pdf)
and the [FX Global Code](https://www.globalfxc.org/fx-global-code/) for execution
and risk-management practices, not an endorsement of these trading rules.

## Spread is a cost, not a fixed spread/ATR veto

This policy supersedes historical descriptions of a fixed spread cutoff in older
documentation and saved reports; those older files are historical evidence.

There is no fixed 20% spread/ATR entry cutoff. Wide spreads reduce the technical
ranking score but cannot alone make it zero. The executable BUY ask or SELL bid
must still permit at least 2:1 reward/risk with valid stop and target levels.
Sizing includes the spread once and can reduce size or reject a broker minimum
that exceeds the risk/margin allowance. Fresh broker prices are checked again
before orders and at confirmation. Future slippage and financing are unknown;
2:1 geometry does not prove positive net expectancy.

Direction, ownership, staged-entry, daily-loss and trial guards still apply.
The approved demo now also permits validated technical entries with a healthy
but empty news feed; see DEMO_ENTRY_EVIDENCE.md. Failed news feeds still block
entry. The explicit time extension is documented in TRIAL_EXTENSION.md and
preserves the original total spending limit. Leverage preferences are unchanged.
Historical spread thresholds remain read-only research references.

## New entry templates are shadow-only

`institutional.sqlite` records one observation per market/closed M15 candle and
strategy version, including WAITs. Its breakout/retest templates require H4/H1
trend agreement, closed-bar confirmation and structure-based invalidation; they
avoid volatility shocks and chasing. Their constants are heuristic research
assumptions. A 2R paper target is an exit rule, not a predicted price.

Paper signals never reach the AI as order recommendations or the coordinator.
There is no environment switch to promote them to trading. Independent candidates
use the next full M15 bid/ask open and up to 16 future bars. Ambiguous stop/target
bars, missing bars and invalid next-open entries are reported separately. This is
not a two-leg portfolio simulation and omits slippage, financing and FX fees.
The analyst's existing recommendations remain subject to the existing entry
pipeline, with the cost-aware spread policy described above.

No global order book, client flow, scheduled macro calendar, verified cross-market
signal or global traded volume is supplied. Missing news does not establish a
quiet event window. No VWAP, volume profile or institutional orders are invented.
The 20-bar indicators describe the available short lookback, not a long-term
macro model. Paper results need separate out-of-sample and forward evaluation
before any template can be promoted; unit tests establish mechanics, not returns.

Startup logs identify `analysis_framework=institutional_discipline_v1`,
`strategy_research=shadow_breakout_retest_v1` and
`spread_policy=executable_reward_risk_and_sizing`. New research logs are
`institutional_observation`; `execution_authorized` is always false there.

Read-only report from the configured persistent state directory:

```sh
python -B -m smarttrader.institutional --report
```
