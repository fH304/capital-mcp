# Explicit recovery of a missing demo budget ledger

Deployment does not recover or renew a trial automatically. Prefer restoring the complete original database and its durable receipts from a verified backup.

If the original ledger is unavailable, the recovery tool can prepare a one-time budget checkpoint from matching provider Cost and Usage CSV exports. Review the files locally, with the approved demo account configured in `BOT_ACCOUNT_ID`:

```bash
python -B -m smarttrader.recovery --costs costs.csv --usage usage.csv --observed-at UNIX_TIME
```

Use the UTC export observation time, not the end of the current daily bucket. Daily costs must match the pinned model's text usage and tariff. The tool charges the whole exported organization period, including pretrial use, rounds upward to cents and adds all surviving local charges and reservations. Never publish the exports or the resulting checkpoint in the repository.

After the recovery module is deployed, apply the reviewed checkpoint explicitly in the service shell:

```bash
python -B -m smarttrader.recovery --apply REVIEWED_CHECKPOINT
```

Application requires the authorized demo configuration, a mounted persistent state directory, an existing blocked missing-ledger placeholder, a fresh broker check showing no positions or working orders, and recent exports within the original authorization window. The broker check is unarmed and sends no trading order or AI request. The original duration and total limit remain fixed.

The checkpoint and its companion receipt persist together. Repeated application cannot refund new spend. Missing or altered receipts, or a lost database with a surviving receipt, block further activity. Preserve both files when backing up state.

Success reports `trial_budget_reconciled`, `accounted_usd_scope=provider_checkpoint_plus_local` and `history_complete=false`. This restores a reviewed budget checkpoint, not lost request or trading history. Provider exports are observed snapshots and can lag final billing. Local request counts and observations remain explicitly partial.

Validation: 194 unit and integration tests pass with simulated HTTP. Tests cover original expiry and limit, retained reservations, repeated and concurrent recovery, receipt loss and corruption, empty-account checks and the fixed account pin. The account identifier is read from the environment and checked against its compiled fingerprint; changing the environment cannot authorize a different account.
