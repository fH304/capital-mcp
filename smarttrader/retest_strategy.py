"""Experimental daily breakout / hourly retest / M15 confirmation.

Pure, read-only decision functions. Nothing in the trading worker imports this
module, so a historical study cannot authorize a broker order.
"""
from datetime import datetime, timezone
from statistics import mean

VERSION = 'daily_breakout_retest_v1'


def stamp(row):
    return datetime.fromisoformat(row['snapshotTimeUTC'].replace('Z', '+00:00')).replace(
        tzinfo=timezone.utc).timestamp()


def mid(row, field):
    sides = row[field]
    return (sides['bid'] + sides['ask']) / 2


def daily_breakouts(daily):
    """Signals are known only at the *end* of the dated daily candle."""
    result = []
    for i in range(20, len(daily)):
        row = daily[i]
        close = mid(row, 'closePrice')
        high = max(mid(r, 'highPrice') for r in daily[i-20:i])
        low = min(mid(r, 'lowPrice') for r in daily[i-20:i])
        direction = 1 if close > high else -1 if close < low else 0
        if direction:
            result.append(dict(direction=direction, level=high if direction == 1 else low,
                               known_at=stamp(row) + 86400, date=row['snapshotTimeUTC'][:10]))
    return result


def hourly_retest(hourly, i, breakout):
    """Require a touched level and an hourly close back in breakout direction."""
    if i < 15:
        return None
    row = hourly[i]
    when = stamp(row)
    if not breakout['known_at'] <= when < breakout['known_at'] + 3 * 86400:
        return None
    sign = breakout['direction']
    true_ranges = []
    for j in range(i-14, i):
        r, previous = hourly[j], hourly[j-1]
        high, low, previous_close = mid(r, 'highPrice'), mid(r, 'lowPrice'), mid(previous, 'closePrice')
        true_ranges.append(max(high-low, abs(high-previous_close), abs(low-previous_close)))
    atr = mean(true_ranges)
    if atr <= 0:
        return None
    level = breakout['level']
    extreme = mid(row, 'lowPrice' if sign == 1 else 'highPrice')
    touched = abs(extreme - level) <= .25 * atr
    close = mid(row, 'closePrice')
    if not touched or sign * (close-level) <= 0:
        return None
    stop = extreme - sign * .1 * atr
    return dict(direction=sign, level=level, stop=stop, hourly_at=when,
                atr_hour=atr, breakout_date=breakout['date'])


def m15_confirmation(row, retest):
    """The final quarter hour of the closed hourly candle must reject the level."""
    if stamp(row) != retest['hourly_at'] + 2700:
        return False
    sign = retest['direction']
    high, low = mid(row, 'highPrice'), mid(row, 'lowPrice')
    opening, close = mid(row, 'openPrice'), mid(row, 'closePrice')
    if high <= low or sign * (close-opening) < .5 * (high-low):
        return False
    return ((close-low)/(high-low) >= .75 if sign == 1
            else (high-close)/(high-low) >= .75)


def entry(retest, next_open):
    """Executable next-open sides, fixed 2R target; 10% spread-to-stop cap."""
    sign = retest['direction']
    price = next_open['ask' if sign == 1 else 'bid']
    spread = next_open['ask'] - next_open['bid']
    risk = sign * (price - retest['stop'])
    if price <= 0 or risk <= 0 or spread < 0 or spread > .1 * risk:
        return None
    if sign * (price - retest['level']) > .5 * retest['atr_hour']:
        return None
    return dict(direction=sign, entry=price, stop=retest['stop'],
                target=price + sign * 2 * risk, risk=risk, spread=spread,
                strategy=VERSION)
