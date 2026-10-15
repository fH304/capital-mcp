# Explicit demo analysis budget increase

On 15 October 2026 the user authorized a $25 **total** OpenAI analysis ceiling
for the existing trial through the already fixed Friday deadline (17 October
00:00 Asia/Dubai, exclusive). This adds at most $5 to the original $20 ceiling.
It does not buy credits, reset the provider checkpoint, clear pending charges,
or change the trial ID, account pin, broker risk limits or trading strategy.

The original database row retains its $20 limit to verify ledger identity. From
the authorization timestamp forward, a verified original ledger can use the $25
effective total. Only a previous `budget_exhausted` stop is lifted, and only if
the already accounted historical debit plus all local settled and full pending
reservations leaves room for another maximum charge. Other stop reasons remain
blocked. The fixed Friday deadline still applies, even with money remaining.

After deploy, check `trial_status` for `trial_active`, `ledger_verified`,
`authorized_budget_usd: 25`, `budget_increase_applied`, `accounted_usd`, and
`unreconciled_usd`. If any stop remains, diagnose it without deleting state.
