"""Independent closed-candle evidence for the approved demo entry policy.

This validates observations, not a win probability, and creates no orders.
The existing shadow breakout/retest templates are not used for execution.
"""
import math

from .institutional import market_brief
from .strategy import active_setup

NEWS_REQUIRED = 'news_required'
TECHNICAL_OR_NEWS = 'technical_or_news'


def technical_evidence(context, now, *, require_news_health=True):
    """An empty healthy news response differs from a missing or failed feed."""
    result = dict(ready=False, direction=None, reason='news_feed_unavailable',
                  basis='closed_candles_not_probability')
    if require_news_health:
        stamp = context.get('news_checked_at')
        if (context.get('news_feed_healthy') is not True or type(stamp) not in (int, float)
                or not math.isfinite(stamp) or not 0 <= now-stamp <= 21600):
            return result
    if context.get('execution_supported') is not True:
        return dict(result, reason='observation_only_contract')
    if context.get('strategy_policy') in ('three_month_retest_only','independent_hour_day_trends','fast_ai_m15'):
        setup=active_setup(context,now)
        return dict(result,**{k:v for k,v in setup.items() if k not in result},
                    ready=setup['ready'],direction=setup['direction'],reason=setup['reason'])
    brief = market_brief(context, now)
    setup=active_setup(context,now)
    # Recent gaps are rejected by validate_frames. Older market closures remain
    # visible in the brief; they do not alone invalidate fresh closed candles.
    regime = brief['regime']
    if setup['ready'] and setup['strategy'] in ('range_rejection_v1','daily_breakout_retest_v1'):
        return dict(result,**{k:v for k,v in setup.items() if k not in result},
                    ready=True,direction=setup['direction'],reason=setup['reason'])
    if regime not in ('trend_up', 'trend_down'):
        return dict(result, reason=setup['reason'])
    bias = 'up' if regime == 'trend_up' else 'down'
    if brief['frames']['MINUTE_15']['bias'] != bias:
        return dict(result, reason=setup['reason'])
    shock = brief['last_bar_range_atr']
    if shock is None or shock > 3:
        return dict(result, reason='technical_volatility_shock')
    return dict(result,**{k:v for k,v in setup.items() if k not in result},
                ready=setup['ready'],direction=setup['direction'],reason=setup['reason'])
