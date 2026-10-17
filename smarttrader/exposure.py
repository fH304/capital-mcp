"""Two staged demo entries share one conservative risk and margin allowance.

This is an exposure rule, not a probability model. It never changes leverage.
All open positions must belong to the journal and to the same market/direction.
"""
from decimal import Decimal

from .sizing import decimal


TRADE_FRACTION = Decimal('0.00125')
PAIR_FRACTION = Decimal('0.0025')
MARGIN_FRACTION = Decimal('0.1')


class ExposureError(ValueError):
    pass


def value(data, key, zero=False):
    raw = data.get(key)
    if type(raw) not in (int, float):
        raise ExposureError('Unverified exposure value: '+key)
    return decimal(raw, key, zero)


def entry_limits(data, owned, *, direction, assessment, candle):
    """Independent broker snapshot + durable journal; no model risk inputs.

    Reserve the larger of original planned loss, actual fill loss and current
    equity-to-stop downside. FX changes can increase that reservation, never
    release it. Group equity cannot grow while an original leg stays open.
    """
    equity, available = value(data, 'equity'), value(data, 'free_margin')
    daily = value(data, 'daily_remaining_risk', True)
    fx, margin_fx = value(data, 'quote_to_account'), value(data, 'fx_rate')
    bid, ask = value(data, 'bid'), value(data, 'ask')
    leverage = value(data, 'leverage')
    if leverage < 1 or direction not in {'BUY', 'SELL'} or ask < bid:
        raise ExposureError('Invalid exposure contract')
    if data.get('hedging_mode') is not True:
        raise ExposureError('Separate demo entries require verified hedging mode')
    if type(candle) not in (int, float):
        raise ExposureError('A closed candle is required for staged entries')
    decimal(candle, 'candle', True)
    positions = data.get('positions')
    if not isinstance(positions, list) or data.get('working_orders') != []:
        raise ExposureError('Unverified positions or pending working orders')
    if len(positions) >= 2:
        raise ExposureError('pair_position_limit')
    ids = [item.get('position', {}).get('dealId') for item in positions]
    if (not isinstance(owned, dict) or any(not isinstance(x, str) or not x for x in ids)
            or len(set(ids)) != len(ids) or set(ids) != set(owned)):
        raise ExposureError('Unknown or changed position ownership')
    baseline, open_risk, open_margin = equity, Decimal(0), Decimal(0)
    for item in positions:
        pos, market = item['position'], item.get('market', {})
        plan = owned[pos['dealId']]
        if (market.get('epic') != data.get('epic') or plan.get('epic') != data.get('epic')
                or pos.get('direction') != direction or plan.get('direction') != direction
                or pos.get('size') != plan.get('size')
                or pos.get('currency') != data.get('quote_currency')
                or pos.get('currency') != plan.get('quote_currency')
                or pos.get('contractSize') != 1
                or pos.get('stopLevel') != plan.get('stop_level')
                or pos.get('profitLevel') != plan.get('target_level')
                or pos.get('leverage') != plan.get('leverage')):
            raise ExposureError('Existing leg identity or protection changed')
        if assessment != 'positive':
            raise ExposureError('Second entry requires a new positive assessment')
        if decimal(candle, 'candle', True) <= value(plan, 'signal_candle', True):
            raise ExposureError('Second entry requires a later closed candle')
        size, fill, stop = value(pos, 'size'), value(pos, 'level'), value(pos, 'stopLevel')
        old_leverage = value(pos, 'leverage')
        if old_leverage < 1:
            raise ExposureError('Invalid existing leverage')
        if (direction == 'BUY' and bid <= fill) or (direction == 'SELL' and ask >= fill):
            raise ExposureError('No addition to a losing or flat leg')
        baseline = min(baseline, value(plan, 'group_equity'))
        conversion = max(fx, value(plan, 'quote_to_account'))
        distance = max(fill-stop, bid-stop) if direction == 'BUY' else max(stop-fill, stop-ask)
        if distance <= 0:
            raise ExposureError('Invalid existing stop distance')
        open_risk += max(value(plan, 'planned_risk'), value(plan, 'actual_risk'),
                         size*distance*conversion)
        open_margin += max(value(plan, 'required_margin'), value(plan, 'actual_margin'),
                           size*ask/old_leverage*margin_fx)
    pair_limit = baseline*PAIR_FRACTION
    risk_budget = min(equity*TRADE_FRACTION, pair_limit-open_risk, daily-open_risk)
    margin_limit = available*MARGIN_FRACTION
    margin_budget = margin_limit-open_margin
    if risk_budget <= 0 or margin_budget <= 0:
        raise ExposureError('No remaining combined risk or margin allowance')
    return dict(group_equity=float(baseline), pair_risk_limit=float(pair_limit),
                existing_risk=float(open_risk), existing_margin=float(open_margin),
                risk_budget=float(risk_budget), margin_budget=float(margin_budget),
                group_margin_limit=float(margin_limit),
                daily_remaining_risk=float(min(daily, pair_limit)),
                margin_fraction=float(margin_budget/available),
                existing_positions=len(positions))
