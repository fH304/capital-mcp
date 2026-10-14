"""Versioned, deterministic demo setups. Thresholds are hypotheses, not odds."""
from .institutional import market_brief

VERSION = 'structure_trend_v1'


def trend_setup(context, now):
    brief = market_brief(context, now)
    base = dict(ready=False, direction=None, reason='higher_timeframes_range_or_mixed',
                strategy=VERSION, validated_profitability=False)
    if brief['regime'] not in ('trend_up', 'trend_down'):
        return base
    buy = brief['regime'] == 'trend_up'
    sign = 1 if buy else -1
    if brief['frames']['MINUTE_15']['bias'] != ('up' if buy else 'down'):
        return dict(base, reason='entry_frame_disagrees')
    atr = brief['baseline_atr_15']
    if atr <= 0 or brief['last_bar_range_atr'] is None or brief['last_bar_range_atr'] > 3:
        return dict(base, reason='volatility_shock')
    bars = context['timeframes']['MINUTE_15']['candles']
    recent = bars[-5:]
    if any(b['t']-a['t'] != 900 for a,b in zip(bars[-21:-1],bars[-20:])):
        return dict(base, reason='setup_history_gap')
    last, previous = bars[-1], bars[-2]
    edge = 'h' if buy else 'l'
    level = (max if buy else min)(b[edge] for b in bars[-22:-2])
    # All reference windows precede the signal; no current/unclosed bar is used.
    reversal = sign*(last['c']-last['o']) > 0 and sign*(last['c']-previous['c']) > 0
    retest = (sign*(previous['c']-level) > .1*atr and reversal
              and sign*(last['c']-level) > 0
              and sign*((context['bid'] if buy else context['ask'])-level) > 0
              and (last['l'] <= level+.2*atr if buy else last['h'] >= level-.2*atr))
    pullback = (any(sign*(b['c']-a['c']) < 0 for a,b in zip(recent[:-2],recent[1:-1]))
                and reversal and sign*(last['c']-recent[0]['c']) >= -.5*atr)
    if not (retest or pullback):
        return dict(base, reason='no_closed_pullback_or_retest')
    mid = (context['bid']+context['ask'])/2
    if abs(mid-last['c']) > .5*atr:
        return dict(base, reason='price_extended_no_chasing')
    entry = context['ask'] if buy else context['bid']
    exit_side = context['bid'] if buy else context['ask']
    invalidation = ((min(b['l'] for b in recent) if buy else max(b['h'] for b in recent))
                    - sign*.1*atr)
    if invalidation <= 0 or sign*(exit_side-invalidation) <= 0:
        return dict(base, reason='structure_invalidated')
    # Use an observed opposing extreme, never generate a distant target for 2R.
    levels = [b[edge] for name in ('HOUR','HOUR_4')
              for b in context['timeframes'][name]['candles'][-20:]
              if sign*(b[edge]-entry) > 0]
    if not levels:
        return dict(base, reason='no_observed_target')
    target = (min if buy else max)(levels)
    risk, reward = sign*(entry-invalidation), sign*(target-entry)
    # Explicit stress assumption: .05 ATR adverse slippage at each end.
    # Spread is already in executable entry; it is not subtracted again.
    stress = .1*atr
    if reward-stress < 2*(risk+stress):
        return dict(base, reason='structure_reward_risk_insufficient')
    return dict(base, ready=True, direction='BUY' if buy else 'SELL',
                reason='breakout_retest' if retest else 'trend_pullback',
                invalidation_level=invalidation, target_ceiling=target,
                reference_level=level if retest else recent[0]['c'],
                atr=atr, entry_price=entry, stop_level=invalidation, target_level=target,
                slippage_stress_distance=stress, target_basis='nearest_observed_higher_frame_extreme')


def accepts_levels(setup, action, stop, target, bid, ask):
    if not setup['ready'] or setup['direction'] != action:
        return False
    sign = 1 if action == 'BUY' else -1
    entry = ask if action == 'BUY' else bid
    risk, reward = sign*(entry-stop), sign*(target-entry)
    stress = setup['slippage_stress_distance']
    return (risk > 0 and sign*(setup['invalidation_level']-stop) >= -1e-12
            and sign*(setup['target_ceiling']-target) >= -1e-12
            and reward-stress >= 2*(risk+stress))


def range_setup(context, now):
    """Separate prospective hypothesis; this result never authorizes an order."""
    brief = market_brief(context, now)
    base = dict(ready=False, direction=None, reason='not_range',strategy='range_rejection_v1',
                execution_authorized=False, validated_profitability=False)
    if brief['regime'] != 'range':
        return base
    bars = context['timeframes']['MINUTE_15']['candles']
    if any(b['t']-a['t'] != 900 for a,b in zip(bars[-21:-1],bars[-20:])):
        return dict(base,reason='range_history_gap')
    atr = brief['baseline_atr_15']; last = bars[-1]
    if atr <= 0 or brief['last_bar_range_atr'] is None or brief['last_bar_range_atr'] > 3:
        return dict(base,reason='range_volatility_shock')
    low = min(b['l'] for b in bars[-21:-1]); high = max(b['h'] for b in bars[-21:-1])
    buy = last['l'] <= low+.2*atr and last['c'] > last['o'] and last['c'] > low
    sell = last['h'] >= high-.2*atr and last['c'] < last['o'] and last['c'] < high
    if buy == sell or abs((context['bid']+context['ask'])/2-last['c']) > .5*atr:
        return dict(base,reason='no_range_edge_rejection')
    sign = 1 if buy else -1
    entry = context['ask'] if buy else context['bid']
    stop = (min(low,last['l'])-.1*atr if buy else max(high,last['h'])+.1*atr)
    target = high if buy else low
    risk = sign*(entry-stop); reward = sign*(target-entry)
    if stop <= 0 or target <= 0 or risk <= 0 or reward-.1*atr < 2*(risk+.1*atr):
        return dict(base,reason='range_reward_risk_insufficient')
    return dict(base,ready=True,direction='BUY' if buy else 'SELL',reason='range_edge_rejection',
                stop_level=stop,target_level=target,entry_price=entry,atr=atr,
                slippage_stress_distance=.1*atr)
