"""An hourly, demo-only, two-sided experiment. All times are UTC.

This strategy has no directional edge: identical long and short positions pay
both spreads. Risk gates can skip markets or prevent the second leg entirely.
"""
import math
import re
from decimal import Decimal, ROUND_FLOOR

from .exposure import ExposureError, portfolio_entry_limits
from .fx import ROUTES
from .sizing import SizingError, size_position


NAME_METALS = re.compile(r'\b(gold|silver|platinum|palladium|copper|aluminium|aluminum|nickel|zinc)\b', re.I)
FOCUS = {'USD', 'JPY', 'GBP', 'NZD'}
MULTIPLIER = 5
TARGET_USD = 1.0


def hour_start(now):
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError('Invalid hourly clock')
    return int(now // 3600) * 3600


def market_epics(markets):
    """Use Capital's authenticated catalogue, then validate each contract at entry."""
    if not isinstance(markets, list):
        raise ValueError('Invalid market catalogue')
    selected = set()
    for item in markets:
        if not isinstance(item, dict):
            raise ValueError('Invalid market catalogue item')
        epic, kind = item.get('epic'), item.get('instrumentType')
        if not isinstance(epic, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}', epic):
            continue
        if kind == 'CURRENCIES':
            symbol = re.sub(r'[^A-Z]', '', str(item.get('symbol', '')).upper())
            if len(symbol) == 6 and ({symbol[:3], symbol[3:]} & FOCUS) \
                    and symbol[3:] in ROUTES | {'USD': None}:
                selected.add(epic)
        elif kind == 'COMMODITIES' and NAME_METALS.search(str(item.get('instrumentName', ''))):
            # Metadata/FX checks at entry still reject unsupported contracts.
            selected.add(epic)
    return sorted(selected)


def should_close(plan, position, now):
    if plan.get('strategy') != 'hourly_hedge_v1':
        return None
    start = plan.get('hour_start')
    if type(start) is not int or start < 0 or start % 3600 or now < start:
        raise ValueError('Invalid owned hourly position')
    if now >= start + 3600:
        return 'hourly_window_end'
    upl = position.get('upl')
    if type(upl) in (int, float) and math.isfinite(upl) and upl >= TARGET_USD:
        return 'hourly_profit_target'
    return None


def plan_entry(snapshot, owned, direction, now, stop):
    """Request exactly five times the old per-entry base, or block the entry.

    The old base is the existing 0.05% equity sizing policy on this contract.
    Other positions and the 5x aggregate risk/margin ceiling are checked again
    by the broker adapter immediately before POST.
    """
    if snapshot.get('environment') != 'demo' or direction not in ('BUY', 'SELL'):
        raise ValueError('Demo and an explicit direction required')
    if snapshot.get('tradeable') is not True or snapshot.get('daily_stopped') is not False:
        raise ValueError('Market or daily gate is closed')
    now_hour = hour_start(now)
    if now >= now_hour + 3540:
        raise ValueError('Insufficient time to establish hourly hedge')
    epic = snapshot['epic']
    same = [p for p in owned.values() if p['epic'] == epic]
    if any(p.get('strategy') != 'hourly_hedge_v1' or p.get('hour_start') != now_hour for p in same):
        raise ValueError('Earlier position in this market must close first')
    if len(same) > 1 or same and same[0]['direction'] == direction:
        raise ValueError('Direction already entered for this hour')
    candle = now_hour
    limits = portfolio_entry_limits(snapshot, owned, candle=candle, now=now,
                                    hourly_direction=direction, risk_multiplier=MULTIPLIER)
    bid, ask = snapshot['bid'], snapshot['ask']
    if not 0 < bid <= ask or not math.isfinite(stop) or stop <= 0:
        raise ValueError('Invalid quote or protective stop')
    loss = (ask-stop if direction == 'BUY' else stop-bid)
    if loss <= 0:
        raise ValueError('Protective stop has wrong side')
    inputs = {key:snapshot[key] for key in ('equity','free_margin','point_value',
              'quote_to_account','margin_per_unit','min_size','size_step','max_size')}
    # Compute the previous strategy's stand-alone base on the same stop; no
    # silent downsize masquerading as fivefold volume.
    base = size_position(stop_distance=(bid-stop if direction == 'BUY' else stop-ask),
                         spread=ask-bid, open_risk=0,
                         daily_remaining_risk=snapshot['daily_remaining_risk'],
                         risk_fraction='.0005', portfolio_fraction='.0025',
                         margin_fraction='.1', **inputs)['size']
    step = Decimal(str(snapshot['size_step']))
    size = float((Decimal(str(base)) * MULTIPLIER / step).to_integral_value(rounding=ROUND_FLOOR) * step)
    if size > snapshot['max_size'] or size < snapshot['min_size']:
        raise ValueError('Fivefold size outside contract limits')
    risk = size*loss*snapshot['quote_to_account']
    margin = size*snapshot['margin_per_unit']
    if risk > limits['risk_budget'] + 1e-9 or margin > limits['margin_budget'] + 1e-9:
        raise ExposureError('Fivefold size exceeds remaining shared risk or margin')
    result = dict(account_id=str(snapshot['account_id']),epic=epic,direction=direction,
                  size=size,stop_level=stop,target_level=None,
                  target_basis='hourly_usd_profit_or_hour_end',
                  strategy='hourly_hedge_v1',hour_start=now_hour,
                  planned_risk=risk,required_margin=margin,
                  reason='hourly_two_sided_demo',article_ids=[],
                  signal_candle=candle,assessment='neutral',
                  group_equity=limits['group_equity'],
                  portfolio_risk_limit=limits['portfolio_risk_limit'],
                  existing_risk=limits['existing_risk'],existing_margin=limits['existing_margin'],
                  combined_planned_risk=limits['existing_risk']+risk,
                  combined_required_margin=limits['existing_margin']+margin,
                  leverage=snapshot['leverage'],margin_factor=snapshot['margin_factor'],
                  entry_policy='hourly_hedge_risk_bounded')
    for key in ('account_currency','quote_currency','fx_epic','fx_rate',
                'quote_to_account','conversion_time','conversion_fee_buffer'):
        if key in snapshot:
            result[key] = snapshot[key]
    return result
