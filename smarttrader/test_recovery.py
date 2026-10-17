import csv
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from .recovery import (review_exports, validate_checkpoint, canonical,
                       require_persistent_state, apply_reviewed)
from .trial import (APPROVED_ACCOUNT, AUTHORIZED_STARTED_AT, DURATION,
                    LIMIT_NANO, MAX_CHARGE, MODEL, TrialBudget, TrialStopped)
from .test_trial import response, authorize_test_account, original_checkpoint


DAY=int(AUTHORIZED_STARTED_AT)//86400*86400
NOW=AUTHORIZED_STARTED_AT+86400


def csv_bytes(headers,rows):
    text=io.StringIO()
    writer=csv.DictWriter(text,fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return text.getvalue().encode()


def exports():
    costs=csv_bytes(['start_time','end_time','amount_value','amount_currency','organization_id'],[
        dict(start_time=DAY,end_time=DAY+86400,amount_value='.00056',amount_currency='usd',organization_id='org-test'),
        dict(start_time=DAY+86400,end_time=DAY+172800,amount_value='',amount_currency='',organization_id='')])
    usage=csv_bytes(['start_time','end_time','num_model_requests','model','batch','service_tier',
                     'input_tokens','output_tokens','input_cached_tokens','api_key_id','project_id'],[
        dict(start_time=DAY,end_time=DAY+86400,num_model_requests='1.0',model=MODEL,batch='False',service_tier='default',
             input_tokens='1000.0',output_tokens='100.0',input_cached_tokens='0.0',api_key_id='key-test',project_id='proj-test'),
        dict(start_time=DAY+86400,end_time=DAY+172800,num_model_requests='',model='',batch='',service_tier='',
             input_tokens='',output_tokens='',input_cached_tokens='',api_key_id='',project_id='')])
    return costs,usage


class AccountPinTests(unittest.TestCase):
    def test_environment_account_alone_cannot_authorize_paid_requests(self):
        with tempfile.TemporaryDirectory() as directory,patch('smarttrader.trial.ACCOUNT_SHA256','0'*64):
            path=Path(directory)/'trial.sqlite'
            original_checkpoint(path,AUTHORIZED_STARTED_AT)
            budget=TrialBudget(path,APPROVED_ACCOUNT,NOW)
            try:
                with self.assertRaisesRegex(TrialStopped,'unauthorized_trial_account'):
                    budget.reserve('GOLD',MODEL,NOW)
                self.assertEqual(budget.db.execute('SELECT COUNT(*) FROM trial_calls').fetchone()[0],0)
            finally:
                budget.close()

    def test_environment_cannot_override_the_compiled_account_pin(self):
        from .trial import ACCOUNT_SHA256, is_approved_account
        with patch.dict(os.environ,{'BOT_ACCOUNT_ID':'other-account','ACCOUNT_SHA256':'0'*64}):
            self.assertFalse(is_approved_account('other-account'))
            from .trial import ACCOUNT_SHA256 as unchanged
            self.assertEqual(unchanged,ACCOUNT_SHA256)


class ExportReviewTests(unittest.TestCase):
    def setUp(self):
        authorize_test_account(self)

    def test_exact_cost_matching_and_upward_cent_rounding(self):
        costs,usage=exports()
        checkpoint=review_exports(costs,usage,NOW)
        self.assertEqual(checkpoint['cost_nano'],560_000)
        self.assertEqual(checkpoint['budget_debit_nano'],10_000_000)
        self.assertEqual(checkpoint['export_requests'],1)
        self.assertEqual(checkpoint['started_at'],AUTHORIZED_STARTED_AT)
        self.assertEqual(checkpoint['ends_at'],AUTHORIZED_STARTED_AT+DURATION)
        self.assertEqual(checkpoint['scope'],'organization_total_including_pretrial')

    def test_mismatched_daily_billing_blocks_review(self):
        costs,usage=exports()
        with self.assertRaisesRegex(ValueError,'disagree'):
            review_exports(costs.replace(b'.00056',b'.00055'),usage,NOW)

    def test_unpriced_model_batch_and_service_tier_block(self):
        costs,usage=exports()
        for old,new in [(MODEL.encode(),b'other-model'),(b'False',b'True'),(b'default',b'priority')]:
            with self.subTest(new=new),self.assertRaises(ValueError):
                review_exports(costs,usage.replace(old,new),NOW)

    def test_negative_currency_and_fractional_counts_block(self):
        costs,usage=exports()
        for c,u in [(costs.replace(b'.00056',b'-.00056'),usage),
                    (costs.replace(b'usd',b'aed'),usage),
                    (costs,usage.replace(b'1000.0',b'1000.5'))]:
            with self.subTest(c=c[:50]),self.assertRaises(ValueError):
                review_exports(c,u,NOW)

    def test_date_coverage_before_original_start_or_inconsistent_files_blocks(self):
        costs,usage=exports()
        with self.assertRaises(ValueError):
            review_exports(costs,usage,NOW+86400)
        with self.assertRaises(ValueError):
            review_exports(costs,usage.replace(str(DAY).encode(),str(DAY+86400).encode()),NOW)

    def test_modified_authorization_or_debit_cannot_validate(self):
        checkpoint=review_exports(*exports(),NOW)
        for key,value in [('ends_at',checkpoint['ends_at']+1),('limit_nano',LIMIT_NANO+1),
                          ('cost_nano',0),('budget_debit_nano',0),('costs_sha256',''),
                          ('account','other'),('observed_at',float('nan'))]:
            altered=dict(checkpoint,**{key:value})
            with self.subTest(key=key),self.assertRaises(ValueError):
                validate_checkpoint(altered)


class LedgerRecoveryTests(unittest.TestCase):
    def setUp(self):
        authorize_test_account(self)
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.path=self.directory/'trial.sqlite'
        self.checkpoint=review_exports(*exports(),NOW)
        self.budget=TrialBudget(self.path,APPROVED_ACCOUNT,NOW)
        self.addCleanup(self.budget.close)
        self.flat=dict(account_id=APPROVED_ACCOUNT,positions=[],working_orders=[],observed_at=NOW)

    def apply(self,**kwargs):
        return self.budget.recover_missing(kwargs.get('checkpoint',self.checkpoint),
                                          kwargs.get('now',NOW),kwargs.get('state',self.flat))

    def test_explicit_recovery_keeps_original_deadline_and_offset_after_restart(self):
        before=self.budget.db.execute('SELECT started_at,ends_at,limit_nano FROM trial_runs').fetchone()
        status=self.apply()
        self.assertTrue(status['trial_active'])
        self.assertTrue(status['ledger_verified'])
        self.assertFalse(status['history_complete'])
        self.assertEqual(status['requests'],0)
        self.assertEqual(status['export_requests'],1)
        self.assertEqual(status['accounted_usd'],.01)
        self.assertEqual(status['remaining_usd'],19.99)
        self.assertEqual(status['accounted_usd_scope'],'provider_checkpoint_plus_local')
        self.assertEqual(self.budget.db.execute('SELECT started_at,ends_at,limit_nano FROM trial_runs').fetchone(),before)
        charge=self.budget.reserve('GOLD',MODEL,NOW+1)
        self.budget.settle(charge,response())
        restarted=TrialBudget(self.path,APPROVED_ACCOUNT,NOW+2)
        self.addCleanup(restarted.close)
        self.assertAlmostEqual(restarted.status(NOW+2)['accounted_usd'],.0132)
        self.assertEqual(restarted.status(AUTHORIZED_STARTED_AT+DURATION)['stop_reason'],'duration_elapsed')

    def test_surviving_pending_and_settled_charges_are_added_without_erasing_rows(self):
        self.budget.db.execute('INSERT INTO trial_calls VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)',
                              ('settled',self.checkpoint['trial_id'],APPROVED_ACCOUNT,'GOLD',NOW,1000,1000))
        self.budget.db.execute('INSERT INTO trial_calls VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL)',
                              ('pending',self.checkpoint['trial_id'],APPROVED_ACCOUNT,'GOLD',NOW,MAX_CHARGE))
        self.budget.db.commit()
        rows=self.budget.db.execute('SELECT * FROM trial_calls').fetchall()
        status=self.apply()
        self.assertEqual(status['unreconciled_usd'],MAX_CHARGE/1e9)
        self.assertAlmostEqual(status['accounted_usd'],.01+.000001+MAX_CHARGE/1e9)
        self.assertEqual(self.budget.db.execute('SELECT * FROM trial_calls').fetchall(),rows)

    def test_repeated_apply_cannot_refund_new_spend(self):
        self.apply()
        self.budget.reserve('GOLD',MODEL,NOW+1)
        before=self.budget.status(NOW+1)['accounted_usd']
        with self.assertRaises(TrialStopped):
            self.apply(now=NOW+1)
        self.assertEqual(self.budget.status(NOW+1)['accounted_usd'],before)

    def test_deleted_database_stays_blocked_even_with_old_reviewed_checkpoint(self):
        self.apply()
        self.budget.close()
        self.path.unlink()
        replacement=TrialBudget(self.path,APPROVED_ACCOUNT,NOW+1)
        self.addCleanup(replacement.close)
        state=replacement.status(NOW+1)
        self.assertEqual(state['stop_reason'],'trial_recovery_state_missing')
        self.assertIsNone(state['remaining_usd'])
        with self.assertRaises(TrialStopped):
            replacement.recover_missing(self.checkpoint,NOW+1,self.flat)

    def test_changed_guard_blocks_paid_requests(self):
        self.apply()
        self.budget.recovery_guard.write_bytes(b'changed')
        status=self.budget.status(NOW)
        self.assertEqual(status['stop_reason'],'trial_recovery_invalid')
        self.assertFalse(status['ledger_verified'])
        self.assertIsNone(status['remaining_usd'])
        with self.assertRaises(TrialStopped):
            self.budget.reserve('GOLD',MODEL,NOW)

    def test_missing_guard_or_modified_audit_blocks_entries(self):
        self.apply()
        receipt=self.budget.recovery_guard.read_bytes()
        self.budget.recovery_guard.unlink()
        with self.assertRaisesRegex(TrialStopped,'trial_recovery_invalid'):
            self.budget.require_entry(NOW)
        self.budget.recovery_guard.write_bytes(receipt)
        self.budget.db.execute('UPDATE trial_recoveries SET checkpoint_sha=?',('0'*64,))
        self.budget.db.commit()
        with self.assertRaisesRegex(TrialStopped,'trial_recovery_invalid'):
            self.budget.require_entry(NOW)

    def test_recovered_budget_reserves_against_historical_debit(self):
        checkpoint=dict(self.checkpoint,cost_nano=19_500_000_000,budget_debit_nano=19_500_000_000)
        self.apply(checkpoint=checkpoint)
        self.budget.reserve('GOLD',MODEL,NOW)
        with self.assertRaisesRegex(TrialStopped,'budget_exhausted'):
            self.budget.reserve('SILVER',MODEL,NOW)
        self.assertLessEqual(self.budget.status(NOW)['accounted_usd'],20)

    def test_expired_stale_future_or_nonflat_recovery_never_unblocks(self):
        cases=[dict(now=AUTHORIZED_STARTED_AT+DURATION),dict(now=NOW+6*3600+1),
               dict(state=dict(self.flat,observed_at=NOW+1)),
               dict(state=dict(self.flat,positions=[{}])),
               dict(state=dict(self.flat,working_orders=[{}])),
               dict(state=dict(self.flat,account_id='other'))]
        for case in cases:
            with self.subTest(case=case),self.assertRaises(TrialStopped):
                self.apply(**case)
            self.assertFalse(self.budget.recovery_guard.exists())
            self.assertEqual(self.budget.status(NOW)['stop_reason'],'trial_state_missing')

    def test_existing_original_or_replaced_ledger_cannot_be_recovered_again(self):
        for reason,start in [(None,AUTHORIZED_STARTED_AT),('trial_ledger_replaced',NOW)]:
            self.budget.db.execute('UPDATE trial_runs SET stop_reason=?,started_at=?',(reason,start))
            self.budget.db.commit()
            with self.subTest(reason=reason),self.assertRaises(TrialStopped):
                self.apply()
            self.assertFalse(self.budget.recovery_guard.exists())

    def test_insufficient_budget_cannot_create_receipt_or_grant(self):
        checkpoint=dict(self.checkpoint,cost_nano=LIMIT_NANO,budget_debit_nano=LIMIT_NANO)
        with self.assertRaisesRegex(TrialStopped,'budget_exhausted'):
            self.apply(checkpoint=checkpoint)
        self.assertFalse(self.budget.recovery_guard.exists())
        self.assertEqual(self.budget.status(NOW)['stop_reason'],'trial_state_missing')

    def test_guard_only_crash_state_is_blocked(self):
        self.budget.recovery_guard.write_bytes(canonical(self.checkpoint))
        status=self.budget.status(NOW)
        self.assertEqual(status['stop_reason'],'trial_recovery_state_missing')
        with self.assertRaises(TrialStopped):
            self.apply()

    def test_concurrent_apply_has_only_one_baseline(self):
        barrier=threading.Barrier(2)
        result=[]
        def apply():
            budget=TrialBudget(self.path,APPROVED_ACCOUNT,NOW)
            try:
                barrier.wait(timeout=5)
                budget.recover_missing(self.checkpoint,NOW,self.flat)
                result.append('applied')
            except TrialStopped:
                result.append('blocked')
            finally:
                budget.close()
        threads=[threading.Thread(target=apply) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertCountEqual(result,['applied','blocked'])
        self.assertEqual(self.budget.db.execute('SELECT COUNT(*) FROM trial_recoveries').fetchone()[0],1)
        self.assertEqual(self.budget.status(NOW)['accounted_usd'],.01)

    def test_read_only_report_does_not_modify_recovered_state(self):
        self.apply()
        before=self.budget.db.execute('SELECT * FROM trial_runs').fetchall()
        self.assertEqual(TrialBudget.report(self.path,APPROVED_ACCOUNT,NOW+1)['remaining_usd'],19.99)
        self.assertEqual(self.budget.db.execute('SELECT * FROM trial_runs').fetchall(),before)

    def test_apply_uses_only_unarmed_broker_and_requires_fresh_flat_account(self):
        calls=[]
        state=self.flat
        class Broker:
            def __init__(self,**kwargs):
                calls.append(kwargs)
            def login(self):
                calls.append('login')
            def account_state(self):
                return state
        env=dict(CAP_ENV='demo',BOT_ACCOUNT_ID=APPROVED_ACCOUNT,SMART_MODE='demo',
                 SMART_ARMED='DEMO_ONLY',SMART_EXCLUSIVE_ACCOUNT=APPROVED_ACCOUNT,SMART_MARKETS='broad')
        with patch('smarttrader.recovery.require_persistent_state',return_value=self.directory):
            result=apply_reviewed(self.checkpoint,env,clock=lambda:NOW,broker_factory=Broker)
        self.assertFalse(calls[0]['armed'])
        self.assertEqual(result['event'],'trial_budget_reconciled')
        self.assertTrue(result['trial_active'])

    def test_live_config_and_unmounted_state_refuse_recovery_before_broker(self):
        with self.assertRaises(TrialStopped):
            apply_reviewed(self.checkpoint,{'CAP_ENV':'live'},clock=lambda:NOW,
                           broker_factory=lambda **kw:self.fail('Unexpected broker'))
        with patch('smarttrader.recovery.os.path.ismount',return_value=False),self.assertRaises(TrialStopped):
            require_persistent_state(self.directory)

    def test_expired_or_old_export_refuses_before_any_network(self):
        for now in [AUTHORIZED_STARTED_AT+DURATION,NOW+6*3600+1,NOW-1]:
            with self.subTest(now=now),self.assertRaises(TrialStopped):
                apply_reviewed(self.checkpoint,{},clock=lambda:now,
                               broker_factory=lambda **kw:self.fail('Unexpected broker'))


if __name__=='__main__':
    unittest.main()
