"""Independent closed-candle evidence for the approved demo entry policy.

This validates observations, not a win probability, and creates no orders.
The existing shadow breakout/retest templates are not used for execution.
"""
import math

from .institutional import market_brief
from .universe import rank

NEWS_REQUIRED = 'news_required'
TECHNICAL_OR_NEWS = 'technical_or_news'


def technical_evidence(context, now):
    """An empty healthy news response differs from a missing or failed feed."""
    result = dict(ready=False, direction=None, reason='news_feed_unavailable',
                  basis='closed_candles_not_probability')
    stamp = context.get('news_checked_at')
    if (context.get('news_feed_healthy') is not True or type(stamp) not in (int, float)
            or not math.isfinite(stamp) or not 0 <= now-stamp <= 21600):
        return result
    if context.get('execution_supported') is not True:
        return dict(result, reason='observation_only_contract')
    brief = market_brief(context, now)
    scoring = rank(context)
    if scoring['score'] <= 0:
        return dict(result, reason=scoring['reason'])
    # Recent gaps are rejected by validate_frames. Older market closures remain
    # visible in the brief; they do not alone invalidate fresh closed candles.
    regime = brief['regime']
    if regime not in ('trend_up', 'trend_down'):
        return dict(result, reason='technical_regime_not_trending')
    bias = 'up' if regime == 'trend_up' else 'down'
    if brief['frames']['MINUTE_15']['bias'] != bias:
        return dict(result, reason='technical_entry_frame_disagrees')
    shock = brief['last_bar_range_atr']
    if shock is None or shock > 3:
        return dict(result, reason='technical_volatility_shock')
    return dict(result, ready=True, direction='BUY' if bias == 'up' else 'SELL',
                reason='closed_trend_evidence_ready')
