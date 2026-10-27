# Gold and silver research replay

`portfolio_replay.py` is a read-only comparison for a **single hypothetical USD 1,000
account**. It does not change the worker's trade rules or create broker orders.

It reads Capital historical OHLC files named
`market_hist_{GOLD|SILVER}_{MINUTE_15|HOUR|HOUR_4}_YYYY-MM-DD.json` in the
specified directory. Each file is a JSON array extracted from the `prices`
field of a read-only Capital historical-prices response. Supply at least 40 older closed bars for
each timeframe before the requested start date. The CLI checks UTC timestamps
and rejects conflicting duplicate bars.

```sh
python -m smarttrader.portfolio_replay --history-dir /path/to/prices \
  --start 2026-09-01T00:00:00 --end 2026-10-18T12:00:00 \
  --initial 1000 --slippage-bps 0 --overnight-rate 0
```

The baseline evaluates the existing rules on each closed M15 candle. The
`range_confirm` research variant requires the next M15 midpoint close to
continue beyond the signal close without touching the original stop or target.
It enters at the *following* open only if the original stop/target still offer
2:1 after executable spread and assumed adverse execution. This alternative
does **not** authorize a live/demo order.

One position or pending setup can occupy the shared account at a time; GOLD
wins simultaneous ties. This explicit approximation does not reproduce the
worker's round-robin scanning of other markets or a second staged entry. Its
current 5% GOLD and 10% SILVER margin factors and deal-size rules are current
contract proxies, not verified historical contract terms. Quotes include
historical bid/ask spread. Slippage and overnight funding are *assumptions*
passed on the command line. The rate is a positive fraction of original
notional per UTC 21:00 rollover; zero excludes funding, and Wednesday's broker
triple adjustment is not reconstructed. Both same-bar stop/target touches
resolve against the strategy, and adverse gaps exit at the open.

For the 2026-09-01 through 2026-10-16 available data, zero assumed funding
and slippage yielded the following **illustrative** shared-account results:

| Variant | Closed trades | Wins | Ending realized balance |
| --- | ---: | ---: | ---: |
| Existing rules | 24 | 5 | USD 995.58 |
| Range confirmation | 4 | 1 | USD 999.20 |

On a descriptive split at 2026-09-23, baseline ended at USD 998.04 then
USD 997.54 in two independently reset USD 1,000 simulations. Confirmation
ended at USD 997.83 then USD 1001.37. These periods were already inspected
when designing the variant, so the later portion is **not untouched validation**.
The apparent improvement in the full period reflects mostly fewer trades,
with no evidence of a profitable or robust edge. Keep the trial rules
unchanged until new forward observations support a decision.

As an execution sensitivity, adding one basis point of adverse slippage at
each end left 18 baseline trades (USD 996.72) and two confirmation trades
(USD 997.80). Five basis points left three and zero respectively. A
hypothetical one-basis-point daily rollover charge changed neither balance
in this sample because the simulated completed positions did not cross a
rollover. These are stress scenarios, not broker fee quotes.
