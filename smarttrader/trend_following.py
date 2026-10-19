"""Independent H1 and D1 closed-candle trend signals for demo execution."""
import math

from .candles import numeric, utc_stamp, MarketDataError

POLICY = 'independent_hour_day_trends'


def daily_candles(rows, now):
    """Keep only complete broker days; reject malformed or duplicated prices."""
    if not isinstance(rows, list):
        raise MarketDataError('Invalid daily candle history')
    result = []
    seen = set()
    for row in rows:
        try:
            t = utc_stamp(row['snapshotTimeUTC'])
            if t in seen or t > now:
                raise MarketDataError('Duplicate or future daily candle')
            seen.add(t)
            if t + 86400 > now:
                continue
            values = {}
            for field, key in (('openPrice','o'),('highPrice','h'),
                               ('lowPrice','l'),('closePrice','c')):
                bid, ask = numeric(row[field]['bid']), numeric(row[field]['ask'])
                if ask < bid:
                    raise MarketDataError('Inverted daily spread')
                values[key] = (bid + ask) / 2
            if not values['l'] <= min(values['o'],values['c']) <= max(values['o'],values['c']) <= values['h']:
                raise MarketDataError('Invalid daily OHLC')
            result.append(dict(t=t, **values))
        except (KeyError, TypeError):
            raise MarketDataError('Incomplete daily candle') from None
    return sorted(result, key=lambda bar:bar['t'])[-40:]


def frame_setup(context, route, now):
    """A route uses its own candles only; executable quote defines entry and 2R target."""
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
    closes = [bar['c'] for bar in bars]
    fast = sum(closes[-10:])/10
    slow = sum(closes[-20:])/20
    long = fast > slow and latest['c'] > bars[-2]['h']
    short = fast < slow and latest['c'] < bars[-2]['l']
    if not (long or short):
        return dict(base,reason='trend_continuation_absent')
    ranges = [max(bars[i]['h']-bars[i]['l'],abs(bars[i]['h']-bars[i-1]['c']),
                  abs(bars[i]['l']-bars[i-1]['c'])) for i in range(len(bars)-14,len(bars))]
    atr = sum(ranges)/14
    if not math.isfinite(atr) or atr <= 0:
        return dict(base,reason='trend_volatility_unavailable')
    sign = 1 if long else -1
    entry = context['ask'] if long else context['bid']
    stop = (min(bar['l'] for bar in bars[-5:]) - .1*atr if long else
            max(bar['h'] for bar in bars[-5:]) + .1*atr)
    risk = sign*(entry-stop)
    if stop <= 0 or risk <= 0:
        return dict(base,reason='trend_stop_invalid')
    stress = .1*atr
    target = entry + sign*(2*risk + 3*stress + max(risk,atr)*1e-8)
    if target <= 0:
        return dict(base,reason='trend_target_invalid')
    return dict(base,ready=True,direction='BUY' if long else 'SELL',
                reason='closed_candle_trend_continuation',signal_at=latest['t'],
                entry_price=entry,stop_level=stop,invalidation_level=stop,
                target_level=target,target_ceiling=target,
                target_basis='two_risk_units_after_execution_stress',
                slippage_stress_distance=stress,atr=atr)


def active_setup(context, now):
    route = context.get('trend_route')
    if route in ('HOUR','DAY'):
        return frame_setup(context,route,now)
    daily = frame_setup(context,'DAY',now)
    return daily if daily['ready'] else frame_setup(context,'HOUR',now)
