"""Short-horizon, closed-candle candidate for AI to accept or reject.

The model cannot choose the stop, target, size or direction independently of
this evidence. A candidate is not a claim about win probability or profit.
"""
import math


def setup(context, now):
    base = dict(ready=False, direction=None, reason='no_fast_signal',
                strategy='ai_fast_m15_v1', validated_profitability=False)
    if context.get('execution_supported') is not True:
        return dict(base, reason='observation_only_contract')
    try:
        bars = context['timeframes']['MINUTE_15']['candles']
        hourly = context['timeframes']['HOUR']['candles']
        bid, ask = context['bid'], context['ask']
        if len(bars) < 20 or len(hourly) < 5 or not 0 < bid <= ask:
            return dict(base, reason='insufficient_market_data')
        last, previous = bars[-1], bars[-2]
        if not 0 <= now-(last['t']+900) <= 900 or any(
                b['t']-a['t'] != 900 for a,b in zip(bars[-5:-1],bars[-4:])):
            return dict(base, reason='stale_or_gapped_candles')
        ranges = [max(b['h']-b['l'], abs(b['h']-a['c']), abs(b['l']-a['c']))
                  for a,b in zip(bars[-15:-1],bars[-14:])]
        atr = sum(ranges)/len(ranges)
        if not math.isfinite(atr) or atr <= 0 or last['h']-last['l'] > 2.5*atr:
            return dict(base, reason='volatility_shock_or_flat')
        direction = ('BUY' if last['c'] > last['o'] and last['c'] > previous['c']
                     and hourly[-1]['c'] > hourly[-4]['c'] else
                     'SELL' if last['c'] < last['o'] and last['c'] < previous['c']
                     and hourly[-1]['c'] < hourly[-4]['c'] else None)
        if not direction or abs(last['c']-last['o']) < .15*atr:
            return dict(base, reason='momentum_not_confirmed')
        sign = 1 if direction == 'BUY' else -1
        entry = ask if sign == 1 else bid
        exit_side = bid if sign == 1 else ask
        if abs((bid+ask)/2-last['c']) > .6*atr:
            return dict(base, reason='price_moved_after_signal')
        # A recent swing invalidates the signal. Do not enter if the necessary
        # stop is too wide for a short-lived trade or mostly broker spread.
        stop = (last['l']-.05*atr if sign == 1 else last['h']+.05*atr)
        risk = sign*(entry-stop)
        if stop <= 0 or risk <= 0 or sign*(exit_side-stop) <= 0:
            return dict(base, reason='no_valid_invalidation')
        if risk > .65*atr or ask-bid > .15*risk:
            return dict(base, reason='stop_too_wide_or_spread_too_costly')
        # A bounded 2.3R exit is a scenario, never a predicted target. It
        # must be reachable within 1.5 recent ATRs and pass a slippage stress.
        target = entry + sign*2.3*risk
        reward = sign*(target-entry)
        stress = .05*atr
        if reward > 1.5*atr:
            return dict(base, reason='target_too_far_for_fast_window')
        if reward-stress < 2*(risk+stress):
            return dict(base, reason='target_does_not_pay_for_risk_and_cost')
        return dict(base, ready=True, direction=direction, reason='m15_h1_momentum',
                    stop_level=stop, target_level=target,
                    invalidation_level=stop, target_ceiling=target,
                    slippage_stress_distance=stress,
                    target_basis='atr_bounded_2_3r_scenario', signal_at=last['t'],
                    atr=atr, entry_price=entry)
    except (KeyError, TypeError, IndexError, ZeroDivisionError, ValueError):
        return dict(base, reason='invalid_market_data')
