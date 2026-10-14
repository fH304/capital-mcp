# Closed-candle structure strategy

The approved demo's technical_or_news policy now requires structure_trend_v1 for
all BUY/SELL recommendations, including those supported by news. This tightens
entry requirements; it does not promise a minimum number of trades or a deadline.
The analyst may veto a setup. It cannot create eligibility or override levels.

H4/H1 trends must agree with the closed M15 bias. The last five M15 bars must show
a pullback followed by a directional closed-bar reversal, or a preceding close
beyond the prior 20-bar extreme followed by a closed-bar retest. Those 21 reference
bars must be continuous. A quote more than 0.5 baseline ATR from the signal close
is rejected; a last-bar true range above 3 baseline ATR is rejected. These are
fixed heuristic hypotheses, not calibrated target-hit probabilities.

The invalidation stop is beyond the recent five-bar extreme, buffered by 0.1 ATR.
The analyst's stop must be at or beyond this level. The target cannot exceed the
nearest opposing observed H1/H4 extreme. No target is invented to manufacture 2R.
At executable bid/ask sides, reward/risk must still exceed 2 after a stress
allowance of 0.05 ATR adverse slippage at each end. This is an assumption, not a
measured or guaranteed cost. Spread is included once. Sizing also reserves this stress distance within the
existing risk ceiling, reducing size when necessary. Missing financing, fees and
FX charges must not be reported as known net returns. Fresh broker quotes repeat
all validation before sizing; existing size, shared-risk, margin, daily-loss,
trial-budget, expiry, ownership and confirmation checks remain.

An empty healthy news response remains acceptable for the technical route. A
failed/stale/unverified response still blocks that route. No paid requests or
quota increase are added. Search Render logs for news_ready (the actual event
name), checked_at and rejection_counts; news_failed supplies only fixed safe
reason labels and an HTTP status where available. API credentials, response bodies
and URLs are never added to diagnostic logs. This improves diagnosis; it cannot
repair an expired key or provider entitlement without runtime evidence.

Separate range_rejection_v1 observes rejections near the prior 20-bar support or
resistance when H4/H1 are neutral, with a buffered structure stop and opposite-edge
target. It cannot place orders, bypass the trend gate or be promoted by a flag.
Both hypotheses record prospective next-full-M15-open paper outcomes in
strategy-study.sqlite. Parameters are versioned; historical desk tables remain
unchanged. Paper candidates do not overlap within a market/version's fixed
four-hour horizon. Missing/ambiguous bars and invalid next-open entries are
reported separately, never silently turned into profitable fills.

Read-only evaluation:

    python -B -m smarttrader.strategy_study --report

The report includes candidate/status counts, stressed mean R and closed-trade
cumulative drawdown in R, plus a 70/30 chronological comparison without parameter
fitting. It is prospective evidence for these frozen rules, not a retrospective
backtest of an optimized model. Both segments must be reviewed; the later segment
must not be used to tune this version. A 100-candidate reporting floor is not
statistical proof. Correlated market candidates, intratrade/account drawdowns,
financing and fees are not simulated. No profitable result is claimed until
independent data and actual costs are available. A second strategy must undergo a
separate review and version before execution; no automatic promotion exists.
