"""Independent H1 and D1 closed-candle trend signals for demo execution."""
import math

from .candles import numeric, utc_stamp, MarketDataError

POLICY = 'independent_hour_day_trends'


def daily_candles(rows, now):
    """Keep only complete broker days; reject malformed or duplicated prices."""
    return trend_candles(rows,'DAY',now)


def trend_candles(rows, route, now):
    """Broker history for trend exits can contain real overnight/session gaps."""
    if route not in ('HOUR','DAY'):
        raise MarketDataError('Invalid trend candle route')
    if not isinstance(rows, list):
        raise MarketDataError('Invalid trend candle history')
    duration = 3600 if route == 'HOUR' else 86400
    result = []
    seen = set()
    for row in rows:
        try:
            t = utc_stamp(row['snapshotTimeUTC'])
            if t in seen or t > now:
                raise MarketDataError('Duplicate or future trend candle')
            seen.add(t)
            if t + duration > now:
                continue
            values = {}
            for field, key in (('openPrice','o'),('highPrice','h'),
                               ('lowPrice','l'),('closePrice','c')):
                bid, ask = numeric(row[field]['bid']), numeric(row[field]['ask'])
                if ask < bid:
                    raise MarketDataError('Inverted trend spread')
                values[key] = (bid + ask) / 2
            for side in ('bid','ask'):
                low, high = row['lowPrice'][side], row['highPrice'][side]
                opening, closing = row['openPrice'][side], row['closePrice'][side]
                if not low <= min(opening,closing) <= max(opening,closing) <= high:
                    raise MarketDataError('Invalid trend side OHLC')
            if not values['l'] <= min(values['o'],values['c']) <= max(values['o'],values['c']) <= values['h']:
                raise MarketDataError('Invalid trend OHLC')
            result.append(dict(t=t, **values))
        except (KeyError, TypeError):
            raise MarketDataError('Incomplete trend candle') from None
    return sorted(result, key=lambda bar:bar['t'])[-40:]


def frame_setup(context, route, now):
    """A route uses its own closed candles; the broker stop bounds entry risk."""
    if route not in ('HOUR','DAY'):
        raise ValueError('Unsupported trend route')
    version = 'hour_trend_v1' if route == 'HOUR' else 'day_trend_v1'
    base = dict(ready=False, direction=None, reason='trend_history_unavailable',
                strategy=version, route=route, validated_profitability=False)
    bars = (context.get('timeframes',{}).get('HOUR',{}).get('candles',[])
            if route == 'HOUR' else context.get('trend_daily',[]))
    if len(bars) < 21:
        return base
    duration = 3600 if route == 'HOUR' else 86400
    latest = bars[-1]
    if latest['t'] + duration > now or now - latest['t'] - duration > duration + 120:
        return dict(base,reason='trend_candle_stale')
    if any(b['t']-a['t'] != duration for a,b in zip(bars[-5:-1],bars[-4:])):
        return dict(base,reason='trend_recent_gap')
    direction = candle_direction(bars)
    long, short = direction == 'BUY', direction == 'SELL'
    if not (long or short):
        return dict(base,reason='trend_continuation_absent')
    ranges = [max(bars[i]['h']-bars[i]['l'],abs(bars[i]['h']-bars[i-1]['c']),
                  abs(bars[i]['l']-bars[i-1]['c'])) for i in range(len(bars)-14,len(bars))]
    atr = sum(ranges)/14
    if not math.isfinite(atr) or atr <= 0:
        return dict(base,reason='trend_volatility_unavailable')
    entry = context['ask'] if long else context['bid']
    stop = (min(bar['l'] for bar in bars[-5:]) - .1*atr if long else
            max(bar['h'] for bar in bars[-5:]) + .1*atr)
    risk = entry-stop if long else stop-entry
    if stop <= 0 or risk <= 0:
        return dict(base,reason='trend_stop_invalid')
    return dict(base,ready=True,direction='BUY' if long else 'SELL',
                reason='closed_candle_trend_continuation',signal_at=latest['t'],
                entry_price=entry,stop_level=stop,invalidation_level=stop,
                target_level=0,target_ceiling=0,
                target_basis='confirmed_same_frame_reversal_no_take_profit',
                slippage_stress_distance=.1*atr,atr=atr)


def candle_direction(bars):
    """The entry and exit use the same 10/20 close trend and prior bar break."""
    closes = [bar['c'] for bar in bars]
    fast, slow = sum(closes[-10:])/10, sum(closes[-20:])/20
    if fast > slow and bars[-1]['c'] > bars[-2]['h']:
        return 'BUY'
    if fast < slow and bars[-1]['c'] < bars[-2]['l']:
        return 'SELL'
    return None


def reversal_exit(bars, route, direction, entry_signal_at, now):
    """Close only on a *new* completed opposite signal from the entry route."""
    if route not in ('HOUR','DAY') or direction not in ('BUY','SELL') \
            or type(entry_signal_at) not in (float,int) or not math.isfinite(entry_signal_at):
        raise ValueError('Invalid owned trend position')
    duration = 3600 if route == 'HOUR' else 86400
    if len(bars) < 21 or bars[-1]['t'] <= entry_signal_at:
        return False
    if (bars[-1]['t'] + duration > now or now-bars[-1]['t']-duration > duration+120
            or any(a['t'] >= b['t'] for a,b in zip(bars[:-1],bars[1:]))):
        return False
    opposite = 'SELL' if direction == 'BUY' else 'BUY'
    return candle_direction(bars) == opposite


def active_setup(context, now):
    route = context.get('trend_route')
    if route in ('HOUR','DAY'):
        return frame_setup(context,route,now)
    daily = frame_setup(context,'DAY',now)
    return daily if daily['ready'] else frame_setup(context,'HOUR',now)
