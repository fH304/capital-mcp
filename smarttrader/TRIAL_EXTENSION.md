# Explicit extension through Friday, with the original total budget

On 12 October 2026 the user authorized extending the same demo trial through
the end of Friday, 16 October, in Asia/Dubai. The exclusive deadline is
17 October 2026 at 00:00 Dubai, or 16 October at 20:00 UTC.

This is a time extension, not a new trial. The original account pin, trial ID,
start, $20 total token budget, provider recovery receipt, settled charges,
pending reservations, decision metrics and equity observations remain intact.
The original ID contains `72h` as historical identification. It is not changed
to create another budget. Deployments and restarts never move the fixed deadline.

The original database `ends_at` and recovery receipt are retained so their
identity checks continue to verify the original grant. Status reports expose
the extended effective deadline in `ends_at` and `authorized_ends_at`, plus
`original_ends_at`, `extension_id`, `extension_authorized_at` and
`extension_applied`. No environment change or recovery command is required.

Only an original, verified ledger can use this grant. Only the superseded
`duration_elapsed` stop can be lifted during the new authorized time window.
Missing/replaced state, invalid receipts, usage failures, exhausted budgets and
clock rollback remain blocked. The worker stops new paid analysis and entries
at the new deadline or when the existing budget guard stops it. Position
protection continues independently.

News, technical confirmation, executable-price reward/risk, broker minimum size,
risk, margin and staged-entry rules are unchanged. This extension does not force
a trade or promote the shadow breakout/retest templates to execution.

After deployment, verify `trial_status` reports the extension above and
`trial_active: true`, with a verified ledger. If another stop reason remains,
investigate it rather than deleting state or repeating recovery.
