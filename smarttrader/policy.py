"""Bounded sizing and leverage proposals. Does not change broker preferences.

The analysis label is NOT a win probability. validation_passed must come from
an independent evaluation gate, never from the language model's own output.
"""
from .sizing import SizingError, decimal, size_position


def analysis_risk(assessment, validation_passed=False):
    if type(validation_passed) is not bool:
        raise SizingError('Validation gate must be a boolean')
    if assessment not in {'positive', 'acceptable', 'reject'}:
        raise SizingError('Unknown assessment')
    if assessment == 'reject':
        raise SizingError('Analysis rejects entry')
    # Unvalidated positive claims do not increase exposure. No loss chasing.
    return '0.0025' if assessment == 'positive' and validation_passed else '0.00125'


def assessed_size(*, assessment, validation_passed=False, **inputs):
    if 'risk_fraction' in inputs:
        raise SizingError('Analysis policy owns the trade risk fraction')
    return size_position(risk_fraction=analysis_risk(assessment, validation_passed), **inputs)


def leverage_proposal(*, notional, free_margin, available, current, maximum,
                      class_has_positions, class_has_working_orders,
                      data_fresh, margin_fraction='0.1'):
    """Lowest available leverage within the cap that covers planned notional.

    Assumes notional/leverage margin; broker market-specific rules must be
    fetched and sizing recomputed after a preference change and before entry.
    This proposal must not be used directly as an execution instruction.
    """
    for flag in (class_has_positions, class_has_working_orders, data_fresh):
        if type(flag) is not bool:
            raise SizingError('Invalid account state flag')
    if not data_fresh:
        raise SizingError('Stale data; no leverage change')
    n = decimal(notional,'notional')
    margin = decimal(free_margin,'free_margin')
    fraction = decimal(margin_fraction,'margin_fraction')
    cap = decimal(maximum,'maximum')
    old = decimal(current,'current')
    if cap < 1 or old < 1 or fraction > 1:
        raise SizingError('Invalid leverage or margin cap')
    if not isinstance(available, (list, tuple)) or not available:
        raise SizingError('Available leverage values are missing')
    choices = sorted({decimal(x,'available leverage') for x in available})
    if choices[0] < 1 or old not in choices:
        raise SizingError('Unverified current leverage')
    limit = margin * fraction
    if class_has_positions or class_has_working_orders:
        if old > cap or n / old > limit:
            raise SizingError('Class is occupied; reduce size or skip instead of changing leverage')
        chosen = old
        reason = 'preserve_leverage_for_occupied_class'
    else:
        candidates = [x for x in choices if x <= cap and n / x <= limit]
        if not candidates:
            raise SizingError('No permitted leverage fits the margin budget; reduce size or skip')
        chosen = candidates[0]
        reason = 'lowest_permitted_leverage_fitting_margin'
    return {'leverage': float(chosen), 'change_required': chosen != old,
            'estimated_margin': float(n / chosen), 'reason': reason,
            'requires_broker_rule_recheck': True}
