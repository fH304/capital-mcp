"""Journal-owned demo positions share conservative risk and margin allowances.

The legacy two-leg policy is same-market and same-direction; the portfolio
policy has no numeric count limit. Neither policy changes leverage.
"""
from decimal import Decimal

from .sizing import decimal


TRADE_FRACTION = Decimal('0.00125')
PAIR_FRACTION = Decimal('0.0025')
MARGIN_FRACTION = Decimal('0.1')
PORTFOLIO_ENTRY_FRACTION = Decimal('0.0005')


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
    margin_factor = value(data, 'margin_factor')
    if leverage < 1 or margin_factor > 1 or margin_factor < 1/leverage or direction not in {'BUY', 'SELL'} or ask < bid:
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
        original_margin_factor = value(plan, 'margin_factor') if 'margin_factor' in plan else Decimal(1)/old_leverage
        open_margin += max(value(plan, 'required_margin'), value(plan, 'actual_margin'),
                           size*ask*max(margin_factor, original_margin_factor,
                                        Decimal(1)/old_leverage)*margin_fx)
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


def portfolio_entry_limits(data, owned, *, candle, now, hourly_direction=None, risk_multiplier=1):
    """No fixed position count; reserve every owned position across markets.

    The aggregate sizing-reference and 10% free-margin ceilings remain in place.
    Without a broker stop the reference is not a bound on actual loss.
    Quotes, conversions and contract factors for *each* existing market must
    come from a fresh authenticated broker snapshot; missing evidence blocks.
    """
    if risk_multiplier not in (1, 5) or (risk_multiplier == 5 and hourly_direction not in ('BUY','SELL')):
        raise ExposureError('Invalid portfolio risk policy')
    equity, available = value(data, 'equity'), value(data, 'free_margin')
    daily = value(data, 'daily_remaining_risk', True)
    positions, quotes = data.get('positions'), data.get('position_quotes')
    if (data.get('hedging_mode') is not True or not isinstance(positions, list)
            or not isinstance(quotes, dict) or data.get('working_orders') != []):
        raise ExposureError('Unverified portfolio positions or hedging mode')
    if type(candle) not in (int, float) or type(now) not in (int, float):
        raise ExposureError('A fresh closed entry candle is required')
    decimal(candle, 'candle', True)
    ids = [item.get('position', {}).get('dealId') for item in positions]
    if (not isinstance(owned, dict) or any(not isinstance(x, str) or not x for x in ids)
            or len(ids) != len(set(ids)) or set(ids) != set(owned)
            or set(ids) != set(quotes)):
        raise ExposureError('Unknown or changed portfolio ownership')
    baseline, open_risk, open_margin = equity, Decimal(0), Decimal(0)
    components = []
    for item in positions:
        pos, market = item['position'], item['market']
        plan, quote = owned[pos['dealId']], quotes[pos['dealId']]
        if (market.get('epic') != plan.get('epic') or quote.get('epic') != plan.get('epic')
                or pos.get('direction') != plan.get('direction')
                or pos.get('size') != plan.get('size')
                or pos.get('currency') != plan.get('quote_currency')
                or quote.get('quote_currency') != plan.get('quote_currency')
                or pos.get('contractSize') != 1
                or pos.get('stopLevel') != plan.get('stop_level')
                or pos.get('profitLevel') != plan.get('target_level')
                or pos.get('leverage') != plan.get('leverage')):
            raise ExposureError('Existing position identity or protection changed')
        stamp = value(quote, 'quote_time', True)
        conversion_stamp = value(quote, 'conversion_time', True)
        if (not 0 <= Decimal(str(now)) - stamp <= 30
                or not 0 <= Decimal(str(now)) - conversion_stamp <= 30):
            raise ExposureError('Stale portfolio market quote or conversion')
        if decimal(candle, 'candle', True) <= value(plan, 'signal_candle', True) \
                and plan['epic'] == data.get('epic'):
            # A single opposite leg of the same hourly hedge may share its
            # hour. No third leg and no old-trend position can use the carveout.
            matching = [p for p in owned.values() if p['epic'] == plan['epic']]
            if not (hourly_direction and len(matching) == 1
                    and plan.get('strategy') == 'hourly_hedge_v1'
                    and plan.get('hour_start') == candle
                    and plan['direction'] != hourly_direction):
                raise ExposureError('Same market requires a later closed candle')
        bid, ask = value(quote, 'bid'), value(quote, 'ask')
        fx, margin_fx = value(quote, 'quote_to_account'), value(quote, 'fx_rate')
        if ask < bid:
            raise ExposureError('Invalid portfolio executable spread')
        size, fill = value(pos, 'size'), value(pos, 'level')
        leverage = value(pos, 'leverage')
        factor = max(value(quote, 'margin_factor'), value(plan, 'margin_factor'), Decimal(1)/leverage)
        conversion = max(fx, value(plan, 'quote_to_account'))
        if pos.get('stopLevel') is None and plan.get('risk_basis') == 'hourly_reference_unbounded' \
                and plan.get('strategy') == 'hourly_hedge_v1':
            reference = value(plan, 'sizing_reference_distance')
            # Count an adverse move since fill in addition to the original
            # reference; this is still only a sizing scenario, not maximum loss.
            adverse = max(Decimal(0), fill-bid if pos['direction']=='BUY' else ask-fill)
            distance = reference+adverse
        else:
            stop = value(pos, 'stopLevel')
            distance = max(fill-stop, bid-stop) if pos['direction']=='BUY' else max(stop-fill, stop-ask)
            if distance <= 0:
                raise ExposureError('Invalid existing stop distance')
        risk = max(value(plan, 'planned_risk'), value(plan, 'actual_risk'), size*distance*conversion)
        margin = max(value(plan, 'required_margin'), value(plan, 'actual_margin'),
                     size*max(fill,ask)*factor*margin_fx)
        baseline = min(baseline, value(plan, 'group_equity'))
        open_risk += risk
        open_margin += margin
        components.append(dict(risk=float(risk),margin=float(margin),
                               quote_currency=pos['currency'],quote_to_account=float(fx),fx_rate=float(margin_fx)))
    cap = baseline*PAIR_FRACTION*risk_multiplier
    risk_budget = min(equity*PORTFOLIO_ENTRY_FRACTION*risk_multiplier,
                      cap-open_risk, daily-open_risk)
    margin_limit = available*MARGIN_FRACTION
    margin_budget = margin_limit-open_margin
    if risk_budget <= 0 or margin_budget <= 0:
        raise ExposureError('No remaining combined risk or margin allowance')
    return dict(group_equity=float(baseline), portfolio_risk_limit=float(cap),
                existing_risk=float(open_risk), existing_margin=float(open_margin),
                risk_budget=float(risk_budget), margin_budget=float(margin_budget),
                group_margin_limit=float(margin_limit),
                daily_remaining_risk=float(min(daily,cap)),
                margin_fraction=float(margin_budget/available),
                existing_positions=len(positions),existing_components=components)
