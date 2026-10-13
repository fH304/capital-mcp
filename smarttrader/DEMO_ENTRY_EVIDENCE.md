# Conditional technical entries in the approved demo trial

The approved demo trial now defaults to `SMART_ENTRY_EVIDENCE=technical_or_news`.
Other accounts retain `news_required`, and cannot select the new policy. Demo
arming and exclusive-account settings are still required; preview never submits.

An empty successfully checked news response no longer forces WAIT. In that case,
the analyst may propose BUY/SELL only with validated recent closed candles,
H4/H1 trend agreement, matching M15 bias and no large latest-bar volatility shock.
The existing trend ranking must be positive. These are heuristic evidence checks,
not a calibrated probability or demonstrated profitable strategy.

The model cannot declare eligibility: the analyst client and order planner
recompute it independently. Supplied news must still be cited and considered.
Failed/unverified/stale news checks block entry. News and other feed/time guards
are checked again immediately before the broker POST. No extra news or AI request
is introduced; the existing reservations and provider limits remain.

Absolute stops/targets must retain at least 2:1 reward/risk at executable prices.
Fresh broker metadata, FX, minimum size, shared risk/margin, ownership and order
confirmation rules remain in force. The original spending limit and fixed trial
deadline remain. Leverage is unchanged. Shadow breakout/retest templates remain
isolated and do not supply these order recommendations.

`smart_worker_started` and `smart_heartbeat` report `armed` and
`entry_evidence_policy`. Expect `mode=demo`, `armed=true`, and
`entry_evidence_policy=technical_or_news` for activated conditional entries.
`entry_confirmed` proves an actual broker confirmation. WAIT is still a valid
decision; `entry_wait` now records a concrete reason in the persistent report.
