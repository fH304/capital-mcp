# Independent H1 and D1 trend rules (demo)

The approved demo rules engine makes no OpenAI or news requests. The previous
daily breakout, hourly retest and M15 confirmation route is no longer used.
H1 and D1 are evaluated independently from **closed candles**; neither route
requires a signal or directional agreement from the other.

Each route requires at least 21 closed bars. It considers a BUY when its last
10 closes average above its last 20 closes and the latest close exceeds the
previous bar's high. A SELL reverses both comparisons. The route uses the
latest five lows/highs and 0.1 ATR as its protective stop. The proposed
broker stop stays in place after entry, including during API or worker outages.
New H1/D1 entries have **no fixed take-profit order**. The worker checks the
entry route's completed candles for an opposite 10/20-close trend and a close
beyond the previous candle's opposite extreme. Only a newer signal from the
same route closes the owned position. For H1 entries only H1 reversal counts;
for D1 entries only D1 reversal counts. Normal session gaps in exit data do
not postpone this check; missing, invalid or stale evidence never authorizes a
close. Broker executable bid/ask are used for entry and checked again before
submission. One signal is reserved per route and closed bar.

The watcher still checks the broker stop and daily loss limit independently of
the trend feed. Positions opened before this change keep their existing take
profit and do not acquire a new reversal rule without a recorded entry route.
New positions reserve risk against their stop; a gap or slippage can exceed the
planned loss. A trend reversal can arrive late and can also give back profits.

M15 prices remain in the common account safety and loss-limit pipeline; they
do not decide trade direction. Missing daily data does not stop the hourly
signal; missing hourly data does not stop the daily signal. The service may
still wait because of stale quotes, invalid protection, insufficient margin,
broker restrictions, existing exposure or approved trial limits. The rules do
not imply a measured success probability or guaranteed profit.
