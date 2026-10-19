"""Read-only one-week replay of daily_breakout_retest_v1; no broker orders."""
import argparse
import json
from collections import Counter

from .retest_strategy import daily_breakouts, entry, hourly_retest, m15_confirmation, stamp

START = '2026-10-11T00:00:00'
END = '2026-10-18T16:00:00'


def replay(daily, intraday, *, start=START, end=END, initial=1000., notional=100.):
    lower, upper = stamp({'snapshotTimeUTC': start}), stamp({'snapshotTimeUTC': end})
    signals = []
    counts = Counter()
    for epic in sorted(daily):
        hourly = intraday.get(epic+'_HOUR')
        minute = intraday.get(epic+'_MINUTE_15')
        if not isinstance(hourly, list) or not isinstance(minute, list):
            counts['intraday_missing'] += 1
            continue
        by_m15 = {stamp(r): r for r in minute}
        for breakout in daily_breakouts(daily[epic]):
            if not lower <= breakout['known_at'] < upper:
                continue
            counts['closed_daily_breakouts'] += 1
            for i in range(len(hourly)):
                candidate = hourly_retest(hourly, i, breakout)
                if candidate is None:
                    continue
                counts['hourly_retests'] += 1
                confirming = by_m15.get(candidate['hourly_at'] + 2700)
                if not confirming or not m15_confirmation(confirming, candidate):
                    continue
                counts['m15_confirmations'] += 1
                opening_at = candidate['hourly_at'] + 3600
                opening = by_m15.get(opening_at)
                if not opening or opening_at >= upper:
                    continue
                trade = entry(candidate, opening['openPrice'])
                if trade is None:
                    counts['entry_invalid_rejected'] += 1
                    continue
                signals.append(dict(epic=epic, opened_at=opening_at, breakout=breakout,
                                    **trade))
                break  # A daily breakout proposes at most one position.
    signals.sort(key=lambda x: (x['opened_at'], x['epic']))
    balance = initial
    occupied_until = lower
    trades = []
    for signal in signals:
        if signal['opened_at'] < occupied_until:
            counts['shared_account_occupied'] += 1
            continue
        bars = intraday[signal['epic']+'_MINUTE_15']
        sign = signal['direction']
        side = 'bid' if sign == 1 else 'ask'
        for row in bars:
            tick = stamp(row)
            if tick < signal['opened_at'] or tick >= upper:
                continue
            opening = row['openPrice'][side]
            high = row['highPrice'][side]
            low = row['lowPrice'][side]
            stop, target = signal['stop'], signal['target']
            if sign == 1:
                stopped = opening <= stop or low <= stop
                targeted = opening >= target or high >= target
            else:
                stopped = opening >= stop or high >= stop
                targeted = opening <= target or low <= target
            if not (stopped or targeted):
                continue
            # A gap gets its actual open; ambiguous intrabar touch resolves to stop.
            price = (opening if sign*(opening-stop) <= 0 else stop) if stopped else (
                opening if sign*(opening-target) >= 0 else target)
            pnl = notional * sign * (price-signal['entry'])/signal['entry']
            balance += pnl
            trades.append(dict(epic=signal['epic'], breakout_date=signal['breakout']['date'],
                               opened_at=signal['opened_at'], closed_at=tick,
                               reason='stop' if stopped else 'target', pnl_usd=pnl,
                               balance=balance))
            occupied_until = tick + 900
            break
        else:
            counts['unclosed_at_last_available_bar'] += 1
            occupied_until = upper
    return dict(start=start, end=end, inspected_core_markets=len(daily),
                intraday_markets=sum(isinstance(intraday.get(e+'_HOUR'),list) for e in daily),
                initial=initial,notional_per_position=notional,realized_balance=balance,
                open_positions_excluded=counts['unclosed_at_last_available_bar'],
                counts=dict(counts),trades=trades,
                limitations=['No overnight financing, commission or adverse slippage',
                             'No broker minimum size, margin or exact account FX conversion',
                             'One concurrent position; live demo allows two staged legs',
                             'No dynamically discovered crypto universe; no individual shares',
                             'Last week is already inspected and is not untouched validation'])


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--daily',required=True)
    p.add_argument('--intraday',required=True)
    args=p.parse_args()
    with open(args.daily) as source:daily=json.load(source)
    # Accept the compact research export as well as the original Capital rows.
    for market in daily.values():
        for row in market:
            if 't' in row:
                row['snapshotTimeUTC'] = row['t']
                for short, full in (('o','openPrice'),('h','highPrice'),
                                    ('l','lowPrice'),('c','closePrice')):
                    row[full] = row[short]
    with open(args.intraday) as source:intraday=json.load(source)
    print(json.dumps(replay(daily,intraday),indent=2))


if __name__=='__main__':main()
