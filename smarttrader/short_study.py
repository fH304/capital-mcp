"""Unvalidated M15 reversal hypothesis for forward observation only.

This module has no broker access or execution authorization. A fixed, nearby
target is a paper exit assumption, not a forecast of reaching that price.
"""
from .institutional import market_brief

VERSION = 'short_reversal_paper_v1'


def short_setup(context, now):
    base = dict(strategy=VERSION, ready=False, direction=None,
                reason='no_short_reversal', execution_authorized=False,
                validated_profitability=False)
    if context.get('execution_supported') is not True:
        return dict(base, reason='observation_only_contract')
    brief = market_brief(context, now)
    bars = context['timeframes']['MINUTE_15']['candles']
    if any(b['t']-a['t'] != 900 for a, b in zip(bars[-21:-1], bars[-20:])):
        return dict(base, reason='short_history_gap')
    atr = brief['baseline_atr_15']
    if atr <= 0 or brief['last_bar_range_atr'] is None or brief['last_bar_range_atr'] > 3:
        return dict(base, reason='short_volatility_shock')
    last, previous = bars[-1], bars[-2]
    direction = brief['frames']['HOUR']['bias']
    higher = brief['frames']['HOUR_4']['bias']
    if direction not in ('up', 'down') or higher == ('down' if direction == 'up' else 'up'):
        return dict(base, reason='short_higher_frame_conflict')
    buy = direction == 'up'
    sign = 1 if buy else -1
    reversal = (sign*(previous['c']-previous['o']) < 0
                and sign*(last['c']-last['o']) > 0
                and sign*(last['c']-(previous['h'] if buy else previous['l'])) > 0)
    if not reversal:
        return base
    mid = (context['bid']+context['ask'])/2
    if abs(mid-last['c']) > .5*atr:
        return dict(base, reason='short_price_extended')
    entry = context['ask'] if buy else context['bid']
    exit_side = context['bid'] if buy else context['ask']
    stop = (min(b['l'] for b in bars[-3:])-.1*atr if buy else
            max(b['h'] for b in bars[-3:])+.1*atr)
    risk = sign*(entry-stop)
    if stop <= 0 or sign*(exit_side-stop) <= 0 or risk <= 0:
        return dict(base, reason='short_invalid_stop')
    stress = .1*atr  # .05 ATR per side, separate from executable spread.
    target = entry+sign*(1.5*risk+2.6*stress)
    reward = sign*(target-entry)
    if reward > 3*atr or reward-stress < 1.5*(risk+stress):
        return dict(base, reason='short_target_not_nearby')
    return dict(base, ready=True, direction='BUY' if buy else 'SELL',
                reason='closed_m15_reversal', entry_price=entry,
                stop_level=stop, target_level=target, atr=atr,
                spread=context['ask']-context['bid'],
                spread_to_risk=(context['ask']-context['bid'])/risk,
                spread_to_reward=(context['ask']-context['bid'])/reward,
                slippage_stress_distance=stress, min_reward_risk=1.5,
                target_basis='fixed_paper_1_5R_nearby_not_observed')
