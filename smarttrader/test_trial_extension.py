"""A fixed, explicitly authorized deadline extension cannot replenish money."""
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from .analysis import AnalysisClient
from .recovery import review_exports
from .test_analysis import context
from .test_recovery import exports, NOW as RECOVERY_NOW
from .test_trial import authorize_test_account, original_checkpoint, response
from .trial import (APPROVED_ACCOUNT, AUTHORIZED_STARTED_AT, DURATION,
                    EXTENSION_AUTHORIZED_AT, EXTENSION_ID, EXTENSION_ORIGINAL_ENDS_AT,
                    EXTENDED_ENDS_AT, LIMIT_NANO, MAX_CHARGE, MODEL, TRIAL_ID,
                    TrialBudget, TrialStopped)


class TrialExtensionTests(unittest.TestCase):
    def setUp(self):
        authorize_test_account(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'trial.sqlite'
        self.now = EXTENSION_AUTHORIZED_AT+1

    def seed(self, *, reason='duration_elapsed', start=AUTHORIZED_STARTED_AT):
        original_checkpoint(self.path, start)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE trial_runs SET stop_reason=?,last_seen=?',
                       (reason, min(self.now, start+DURATION)))

    def budget(self, now=None):
        budget = TrialBudget(self.path, APPROVED_ACCOUNT, self.now if now is None else now)
        self.addCleanup(budget.close)
        return budget

    def test_deadline_is_fixed_at_the_end_of_friday_in_dubai(self):
        end = datetime.fromtimestamp(EXTENDED_ENDS_AT-1, ZoneInfo('Asia/Dubai'))
        self.assertEqual(end.isoformat(), '2026-10-16T23:59:59+04:00')
        self.assertEqual(end.weekday(), 4)
        self.assertEqual(EXTENSION_ORIGINAL_ENDS_AT, AUTHORIZED_STARTED_AT+DURATION)
        self.assertLess(EXTENSION_ORIGINAL_ENDS_AT, EXTENSION_AUTHORIZED_AT)
        self.assertLess(EXTENSION_AUTHORIZED_AT, EXTENDED_ENDS_AT)

    def test_expired_original_ledger_resumes_without_erasing_calls_or_metrics(self):
        self.seed()
        budget = self.budget()
        budget.db.execute('INSERT INTO trial_calls VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)',
                          ('settled', TRIAL_ID, APPROVED_ACCOUNT, 'GOLD',
                           EXTENSION_ORIGINAL_ENDS_AT-1, 900_000_000, 900_000_000))
        budget.db.execute('INSERT INTO trial_calls VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL)',
                          ('pending', TRIAL_ID, APPROVED_ACCOUNT, 'US100',
                           EXTENSION_ORIGINAL_ENDS_AT-1, MAX_CHARGE))
        budget.db.commit()
        budget.record('entry_blocked', dict(epic='GOLD', reason='missing_news'))
        budget.observe_account(EXTENSION_ORIGINAL_ENDS_AT-1, 1002.67, 0)
        calls = budget.db.execute('SELECT * FROM trial_calls ORDER BY id').fetchall()
        status = budget.summary(self.now)
        self.assertTrue(status['trial_active'])
        self.assertTrue(status['ledger_verified'])
        self.assertIsNone(status['stop_reason'])
        self.assertEqual(status['extension_id'], EXTENSION_ID)
        self.assertTrue(status['extension_applied'])
        self.assertEqual(status['started_at'], AUTHORIZED_STARTED_AT)
        self.assertEqual(status['ends_at'], EXTENDED_ENDS_AT)
        self.assertEqual(status['authorized_ends_at'], EXTENDED_ENDS_AT)
        self.assertEqual(status['original_ends_at'], EXTENSION_ORIGINAL_ENDS_AT)
        self.assertEqual(status['budget_usd'], 20)
        self.assertEqual(status['requests'], 2)
        self.assertAlmostEqual(status['accounted_usd'], .9+MAX_CHARGE/1e9)
        self.assertEqual(status['unreconciled_usd'], MAX_CHARGE/1e9)
        self.assertEqual(status['metrics'][0]['count'], 1)
        self.assertEqual(status['equity_observation']['initial_equity'], 1002.67)
        self.assertEqual(budget.db.execute('SELECT * FROM trial_calls ORDER BY id').fetchall(), calls)
        self.assertEqual(budget.db.execute('SELECT started_at,ends_at,limit_nano FROM trial_runs').fetchone(),
                         (AUTHORIZED_STARTED_AT, EXTENSION_ORIGINAL_ENDS_AT, LIMIT_NANO))
        restarted = self.budget(self.now+86400)
        self.assertEqual(restarted.status(self.now+86400)['accounted_usd'], status['accounted_usd'])
        self.assertEqual(restarted.status(self.now+86400)['ends_at'], EXTENDED_ENDS_AT)

    def test_verified_recovery_receipt_and_historical_debit_survive_extension(self):
        budget = self.budget(RECOVERY_NOW)
        checkpoint = review_exports(*exports(), RECOVERY_NOW)
        state = dict(account_id=APPROVED_ACCOUNT, positions=[], working_orders=[],
                     observed_at=RECOVERY_NOW)
        recovered = budget.recover_missing(checkpoint, RECOVERY_NOW, state)
        charge = budget.reserve('GOLD', MODEL, RECOVERY_NOW+1)
        budget.settle(charge, response())
        receipt = budget.recovery_guard.read_bytes()
        recovery_row = budget.db.execute('SELECT * FROM trial_recoveries').fetchall()
        self.assertEqual(budget.status(EXTENSION_ORIGINAL_ENDS_AT)['stop_reason'], 'duration_elapsed')
        resumed = self.budget()
        status = resumed.status(self.now)
        self.assertTrue(status['trial_active'])
        self.assertEqual(status['accounted_usd_scope'], 'provider_checkpoint_plus_local')
        self.assertEqual(status['historical_budget_debit_usd'], recovered['accounted_usd'])
        self.assertAlmostEqual(status['accounted_usd'], recovered['accounted_usd']+.0032)
        self.assertEqual(status['requests'], 1)
        self.assertEqual(resumed.recovery_guard.read_bytes(), receipt)
        self.assertEqual(resumed.db.execute('SELECT * FROM trial_recoveries').fetchall(), recovery_row)

    def test_only_a_time_stop_can_be_lifted(self):
        self.seed()
        budget = self.budget()
        for reason in ('budget_exhausted', 'usage_unverified', 'trial_state_missing',
                       'trial_ledger_replaced', 'clock_moved_backwards'):
            with self.subTest(reason=reason):
                budget.db.execute('UPDATE trial_runs SET stop_reason=?', (reason,))
                budget.db.commit()
                self.assertEqual(budget.status(self.now)['stop_reason'], reason)
                with self.assertRaises(TrialStopped):
                    budget.require_entry(self.now)

    def test_late_invalid_usage_cannot_hide_behind_the_original_time_stop(self):
        self.seed(reason=None)
        budget = self.budget(EXTENSION_ORIGINAL_ENDS_AT-1)
        charge = budget.reserve('GOLD', MODEL, EXTENSION_ORIGINAL_ENDS_AT-1)
        budget.status(EXTENSION_ORIGINAL_ENDS_AT)
        with self.assertRaisesRegex(TrialStopped, 'usage_unverified'):
            budget.settle(charge, dict(response(), usage=None))
        resumed = self.budget()
        self.assertEqual(resumed.status(self.now)['stop_reason'], 'usage_unverified')
        self.assertEqual(resumed.status(self.now)['unreconciled_usd'], MAX_CHARGE/1e9)
        with self.assertRaises(TrialStopped):
            resumed.require_entry(self.now)

    def test_time_extension_does_not_refund_an_exhausted_budget(self):
        self.seed()
        budget = self.budget()
        used = LIMIT_NANO-MAX_CHARGE+1
        budget.db.execute('INSERT INTO trial_calls VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)',
                          ('prior-spend', TRIAL_ID, APPROVED_ACCOUNT, 'GOLD',
                           EXTENSION_ORIGINAL_ENDS_AT-1, used, used))
        budget.db.commit()
        with self.assertRaisesRegex(TrialStopped, 'budget_exhausted'):
            budget.reserve('BTCUSD', MODEL, self.now)
        restarted = self.budget(self.now+1)
        self.assertEqual(restarted.status(self.now+1)['accounted_usd'], used/1e9)
        self.assertEqual(restarted.status(self.now+1)['requests'], 1)
        self.assertEqual(restarted.status(self.now+1)['stop_reason'], 'budget_exhausted')

    def test_original_deadline_still_applies_before_the_new_authorization(self):
        self.seed()
        budget = self.budget(EXTENSION_AUTHORIZED_AT-1)
        before = budget.status(EXTENSION_AUTHORIZED_AT-1)
        self.assertFalse(before['trial_active'])
        self.assertEqual(before['stop_reason'], 'duration_elapsed')
        self.assertEqual(before['ends_at'], EXTENSION_ORIGINAL_ENDS_AT)
        self.assertNotIn('extension_id', before)
        self.assertTrue(budget.status(EXTENSION_AUTHORIZED_AT)['trial_active'])

    def test_missing_replaced_or_unapproved_ledger_cannot_receive_a_funded_trial(self):
        missing = self.budget()
        self.assertEqual(missing.status(self.now)['stop_reason'], 'trial_state_missing')
        self.assertIsNone(missing.status(self.now)['remaining_usd'])
        missing.db.execute('UPDATE trial_runs SET stop_reason=NULL,started_at=?',
                           (AUTHORIZED_STARTED_AT+1,))
        missing.db.commit()
        self.assertEqual(missing.status(self.now)['stop_reason'], 'trial_ledger_replaced')
        self.assertFalse(missing.status(self.now)['extension_applied'])
        with patch('smarttrader.trial.ACCOUNT_SHA256', '0'*64):
            missing.db.execute('UPDATE trial_runs SET stop_reason=NULL,started_at=?',
                               (AUTHORIZED_STARTED_AT,))
            missing.db.commit()
            self.assertEqual(missing.status(self.now)['stop_reason'], 'unauthorized_trial_account')
            self.assertNotIn('extension_id', missing.status(self.now))

    def test_lost_or_corrupt_recovery_receipt_cannot_be_overridden_by_extension(self):
        self.seed()
        budget = self.budget()
        budget.recovery_guard.write_bytes(b'old-receipt-without-database-checkpoint')
        status = budget.status(self.now)
        self.assertEqual(status['stop_reason'], 'trial_recovery_state_missing')
        self.assertFalse(status['trial_active'])
        self.assertFalse(status['extension_applied'])
        with self.assertRaises(TrialStopped):
            budget.reserve('GOLD', MODEL, self.now)

    def test_read_only_report_reflects_grant_without_modifying_original_ledger(self):
        self.seed()
        budget = self.budget()
        before = budget.db.execute('SELECT * FROM trial_runs').fetchall()
        report = TrialBudget.report(self.path, APPROVED_ACCOUNT, self.now)
        self.assertTrue(report['trial_active'])
        self.assertEqual(report['ends_at'], EXTENDED_ENDS_AT)
        self.assertEqual(budget.db.execute('SELECT * FROM trial_runs').fetchall(), before)
        self.assertEqual(budget.db.execute('SELECT COUNT(*) FROM trial_calls').fetchone()[0], 0)

    def test_exact_new_deadline_blocks_entries_and_redeploy_cannot_extend_it(self):
        self.seed()
        budget = self.budget()
        self.assertTrue(budget.require_entry(EXTENDED_ENDS_AT-1)['trial_active'])
        with self.assertRaisesRegex(TrialStopped, 'duration_elapsed'):
            budget.require_entry(EXTENDED_ENDS_AT)
        restarted = self.budget(EXTENDED_ENDS_AT+86400)
        self.assertEqual(restarted.status(EXTENDED_ENDS_AT+86400)['ends_at'], EXTENDED_ENDS_AT)
        with self.assertRaisesRegex(TrialStopped, 'duration_elapsed'):
            restarted.reserve('GOLD', MODEL, EXTENDED_ENDS_AT+86400)

    def test_clock_rollback_after_new_expiry_does_not_reopen_trial(self):
        self.seed()
        budget = self.budget()
        budget.status(EXTENDED_ENDS_AT)
        self.assertEqual(budget.status(EXTENDED_ENDS_AT-60)['stop_reason'], 'clock_moved_backwards')
        self.assertFalse(budget.status(EXTENDED_ENDS_AT-60)['trial_active'])

    def test_new_expiry_after_reservation_blocks_paid_http_and_keeps_pending_charge(self):
        self.seed()
        budget = self.budget()
        clock = [self.now]
        original_reserve = budget.reserve
        calls = []
        def reserve(*args):
            result = original_reserve(*args)
            clock[0] = EXTENDED_ENDS_AT
            return result
        fresh = context()
        fresh['quote_time'] = self.now
        fresh['articles'][0]['published_utc'] = datetime.fromtimestamp(
            self.now-60, ZoneInfo('UTC')).isoformat()
        client = AnalysisClient('test-key', MODEL, str(self.path.parent/'analysis.sqlite'),
                                daily_calls=None, opener=lambda *a, **kw: calls.append(a), trial=budget)
        self.addCleanup(client.db.close)
        with patch.object(budget, 'reserve', reserve), patch('time.time', lambda: clock[0]):
            with self.assertRaisesRegex(TrialStopped, 'duration_elapsed'):
                client.analyze(fresh)
        self.assertEqual(calls, [])
        self.assertEqual(budget.status(EXTENDED_ENDS_AT)['unreconciled_usd'], MAX_CHARGE/1e9)


if __name__ == '__main__':
    unittest.main()
