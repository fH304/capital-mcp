# Independent H1 and D1 trend rules (demo)

The approved demo rules engine makes no OpenAI or news requests. The previous
daily breakout, hourly retest and M15 confirmation route is no longer used.
H1 and D1 are evaluated independently from **closed candles**; neither route
requires a signal or directional agreement from the other.

Each route requires at least 21 closed bars. It considers a BUY when its last
10 closes average above its last 20 closes and the latest close exceeds the
previous bar's high. A SELL reverses both comparisons. The route uses the
latest five lows/highs and 0.1 ATR as its protective stop. The proposed
target covers at least twice the entry-to-stop distance plus 0.1 ATR execution
stress. Broker executable bid/ask are used for the entry and checked again
before submission. One signal is reserved per route and closed bar.

M15 prices remain in the common account safety and loss-limit pipeline; they
do not decide trade direction. Missing daily data does not stop the hourly
signal; missing hourly data does not stop the daily signal. The service may
still wait because of stale quotes, invalid protection, insufficient margin,
broker restrictions, existing exposure or approved trial limits. The rules do
not imply a measured success probability or guaranteed profit.
