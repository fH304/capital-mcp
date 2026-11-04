"""Account-currency risk sizing. Pure calculation; no broker orders.

point_value and quote_to_account must come from verified contract metadata and
fresh conversion quotes. The caller must reconcile open risk and daily loss.
"""
from decimal import Decimal, ROUND_FLOOR


class SizingError(ValueError):
    pass


def decimal(value, name, zero=False):
    try:
        value = Decimal(str(value))
    except Exception:
        raise SizingError('Invalid ' + name) from None
    if not value.is_finite() or value < 0 or (not zero and value == 0):
        raise SizingError('Invalid ' + name)
    return value


def size_position(*, equity, free_margin, stop_distance, spread,
                  point_value, quote_to_account, margin_per_unit,
                  min_size, size_step, max_size, open_risk,
                  daily_remaining_risk, risk_fraction='0.0025',
                  portfolio_fraction='0.005', margin_fraction='0.1'):
    """Return size and planned risk; floors size and rejects broker minima.

    equity includes unrealized P/L. stop_distance/spread use price units.
    point_value is quote-currency P/L for a one-point move per size unit.
    margin_per_unit is already converted to account currency.
    daily_remaining_risk is supplied by a persistent daily-loss controller.
    Planned loss is not a guarantee against gaps or execution slippage.
    """
    values = dict(equity=equity, free_margin=free_margin, stop_distance=stop_distance,
                  spread=spread, point_value=point_value, quote_to_account=quote_to_account,
                  margin_per_unit=margin_per_unit, min_size=min_size, size_step=size_step,
                  max_size=max_size, open_risk=open_risk, daily_remaining_risk=daily_remaining_risk,
                  risk_fraction=risk_fraction, portfolio_fraction=portfolio_fraction,
                  margin_fraction=margin_fraction)
    v = {k: decimal(x,k,k in {'spread','open_risk','daily_remaining_risk'}) for k,x in values.items()}
    if not v['risk_fraction'] <= v['portfolio_fraction'] <= 1 or v['margin_fraction'] > 1:
        raise SizingError('Invalid risk or margin fraction')
    if v['min_size'] > v['max_size']:
        raise SizingError('Inconsistent broker size limits')
    budget = min(v['equity'] * v['risk_fraction'],
                 v['equity'] * v['portfolio_fraction'] - v['open_risk'],
                 v['daily_remaining_risk'] - v['open_risk'])
    if budget <= 0:
        raise SizingError('No remaining risk budget')
    unit_risk = (v['stop_distance'] + v['spread']) * v['point_value'] * v['quote_to_account']
    raw = min(budget / unit_risk,
              v['free_margin'] * v['margin_fraction'] / v['margin_per_unit'], v['max_size'])
    size = (raw / v['size_step']).to_integral_value(rounding=ROUND_FLOOR) * v['size_step']
    if size < v['min_size']:
        raise SizingError('Broker minimum exceeds available risk or margin')
    return {'size': float(size), 'planned_risk': float(size * unit_risk),
            'risk_budget': float(budget), 'required_margin': float(size * v['margin_per_unit']),
            'equity_used': float(v['equity'])}
