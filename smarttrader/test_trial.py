import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from .analysis import AnalysisClient, AnalysisError
from .capital import CapitalDemo
from .runner import Config, Worker
from .marketdata import MarketCollector
from .test_analysis import NOW, context, recommendation
from .test_runner import LiveStateTransport
from .test_universe import MARKETS, article, trending
from .trial import (APPROVED_ACCOUNT, DURATION, LIMIT_NANO, MAX_CHARGE,
                    MODEL, TRIAL_ID, TrialBudget, TrialStopped)


def response(action='WAIT', inp=6000, out=500, cached=0, status='completed'):
    result = (recommendation() if action=='BUY' else
              dict(action='WAIT',assessment='reject',stop_level=0,target_level=0,
                   reason='No entry candidate',article_ids=[]))
    return dict(model=MODEL,status=status,usage=dict(input_tokens=inp,output_tokens=out,
                input_tokens_details=dict(cached_tokens=cached)),
                output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(result))])])


class Response:
    def __init__(self, data):
        self.data=data
    def __enter__(self):
        return self
    def __exit__(self,*args):
        pass
    def read(self,size):
        return json.dumps(self.data).encode()


class TrialBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=self.tmp.name+'/trial.sqlite'
        self.budgets=[]
    def tearDown(self):
        for budget in self.budgets:
            budget.close()
        self.tmp.cleanup()
    def budget(self,now=NOW):
        budget=TrialBudget(self.path,APPROVED_ACCOUNT,now)
        self.budgets.append(budget)
        return budget
    def consume_except(self,budget,remaining):
        budget.db.execute('INSERT INTO trial_calls VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)',
                          ('previous',TRIAL_ID,APPROVED_ACCOUNT,'EURUSD',NOW,
                           LIMIT_NANO-remaining,LIMIT_NANO-remaining))
        budget.db.commit()
    def test_usage_settled_with_cache_discount_and_integer_money(self):
        budget=self.budget()
        charge=budget.reserve('GOLD',MODEL,NOW)
        self.assertEqual(budget.status(NOW)['unreconciled_usd'],MAX_CHARGE/1e9)
        budget.settle(charge,response(cached=2000))
        status=budget.status(NOW)
        self.assertAlmostEqual(status['accounted_usd'],.0026)
        self.assertEqual(status['unreconciled_usd'],0)
        self.assertEqual(status['requests'],1)
    def test_total_budget_not_reset_at_midnight_or_restart(self):
        budget=self.budget()
        self.consume_except(budget,MAX_CHARGE-1)
        with self.assertRaisesRegex(TrialStopped,'budget_exhausted'):
            budget.reserve('GOLD',MODEL,NOW)
        restarted=self.budget(NOW+86400)
        self.assertEqual(restarted.status(NOW+86400)['started_at'],NOW)
        self.assertEqual(restarted.status(NOW+86400)['ends_at'],NOW+DURATION)
        with self.assertRaises(TrialStopped):
            restarted.reserve('BTCUSD',MODEL,NOW+86400)
    def test_duration_stops_at_exact_deadline_and_cannot_be_renewed(self):
        budget=self.budget()
        self.assertTrue(budget.status(NOW+DURATION-1)['trial_active'])
        with self.assertRaisesRegex(TrialStopped,'duration_elapsed'):
            budget.reserve('US100',MODEL,NOW+DURATION)
        self.assertFalse(self.budget(NOW+DURATION+86400).status(NOW+DURATION+86400)['trial_active'])
    def test_lost_response_keeps_reservation_after_restart(self):
        budget=self.budget()
        budget.reserve('EURUSD',MODEL,NOW)
        restarted=self.budget(NOW+1)
        self.assertEqual(restarted.status(NOW+1)['accounted_usd'],MAX_CHARGE/1e9)
        self.assertEqual(restarted.status(NOW+1)['unreconciled_usd'],MAX_CHARGE/1e9)
    def test_simultaneous_clients_cannot_overreserve_last_request(self):
        self.consume_except(self.budget(),MAX_CHARGE)
        barrier=threading.Barrier(2)
        results=[]
        def reserve():
            budget=TrialBudget(self.path,APPROVED_ACCOUNT,NOW)
            try:
                barrier.wait(timeout=5)
                budget.reserve('EURUSD',MODEL,NOW)
                results.append('allowed')
            except TrialStopped:
                results.append('blocked')
            finally:
                budget.close()
        threads=[threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertCountEqual(results,['allowed','blocked'])
        self.assertEqual(self.budgets[0].status(NOW)['accounted_usd'],20)
    def test_missing_invalid_or_other_model_usage_stops_and_keeps_reservation(self):
        for update in ({'usage':None},{'usage':{'input_tokens':True,'output_tokens':1}},
                       {'model':'unpriced-model'},{'usage':{'input_tokens':1,'output_tokens':2001}}):
            with self.subTest(update=update), tempfile.TemporaryDirectory() as directory:
                budget=TrialBudget(directory+'/trial.sqlite',APPROVED_ACCOUNT,NOW)
                try:
                    charge=budget.reserve('US30',MODEL,NOW)
                    data=response()
                    data.update(update)
                    with self.assertRaisesRegex(TrialStopped,'usage_unverified'):
                        budget.settle(charge,data)
                    self.assertEqual(budget.status(NOW)['accounted_usd'],MAX_CHARGE/1e9)
                    self.assertFalse(budget.status(NOW)['trial_active'])
                finally:
                    budget.close()
    def test_clock_rollback_stops_instead_of_extending_trial(self):
        budget=self.budget()
        budget.status(NOW+100)
        self.assertEqual(budget.status(NOW+10)['stop_reason'],'clock_moved_backwards')
    def test_read_only_report_tracks_decisions_and_equity_without_renewal(self):
        budget=self.budget()
        budget.record('analysis_ready',dict(epic='GOLD',action='WAIT'))
        budget.record('entry_confirmed',dict(epic='US30'))
        budget.observe_account(NOW,1000,0)
        budget.observe_account(NOW+100,1002,1)
        before=budget.db.execute('SELECT last_seen,stop_reason FROM trial_runs').fetchone()
        report=TrialBudget.report(self.path,APPROVED_ACCOUNT,NOW+DURATION)
        self.assertEqual(report['stop_reason'],'duration_elapsed')
        self.assertEqual(report['equity_observation']['equity_change'],2)
        self.assertEqual(report['equity_observation']['open_positions'],1)
        self.assertEqual(len(report['metrics']),2)
        self.assertEqual(budget.db.execute('SELECT last_seen,stop_reason FROM trial_runs').fetchone(),before)
    def test_analysis_requests_reserve_before_http_and_settle_wait(self):
        budget=self.budget()
        calls=[]
        def opener(request,timeout):
            self.assertEqual(budget.status(NOW)['unreconciled_usd'],MAX_CHARGE/1e9)
            calls.append(json.loads(request.data))
            return Response(response())
        client=AnalysisClient('private',MODEL,self.tmp.name+'/ai.sqlite',daily_calls=None,
                              opener=opener,trial=budget)
        try:
            self.assertEqual(client.analyze(context(),NOW)['action'],'WAIT')
            self.assertEqual(len(calls),1)
            self.assertEqual(budget.status(NOW)['accounted_usd'],.0032)
            self.consume_except(budget,MAX_CHARGE-1)
            with self.assertRaises(TrialStopped):
                client.analyze(context(),NOW)
            self.assertEqual(len(calls),1)
        finally:
            client.db.close()
    def test_http_failure_is_not_refunded_or_automatically_retried(self):
        budget=self.budget()
        def fail(*args,**kwargs):
            raise HTTPError('https://secret.invalid',429,'private',{},None)
        client=AnalysisClient('private',MODEL,self.tmp.name+'/ai.sqlite',daily_calls=None,
                              opener=fail,trial=budget)
        try:
            with self.assertRaisesRegex(AnalysisError,'HTTP status 429'):
                client.analyze(context(),NOW)
            self.assertEqual(budget.status(NOW)['unreconciled_usd'],MAX_CHARGE/1e9)
        finally:
            client.db.close()
    def test_expiry_after_reservation_prevents_the_paid_http_request(self):
        budget=self.budget()
        clock=[NOW]
        original=budget.reserve
        calls=[]
        def reserve(*args):
            charge=original(*args)
            clock[0]=NOW+DURATION
            return charge
        client=AnalysisClient('private',MODEL,self.tmp.name+'/ai.sqlite',daily_calls=None,
                              opener=lambda *a,**kw:calls.append(a),trial=budget)
        try:
            with patch.object(budget,'reserve',reserve),patch('time.time',lambda:clock[0]):
                with self.assertRaisesRegex(TrialStopped,'duration_elapsed'):
                    client.analyze(context())
            self.assertEqual(calls,[])
        finally:
            client.db.close()
    def test_unpriced_model_cannot_reserve_or_call_http(self):
        budget=self.budget()
        client=AnalysisClient('private','other-model',self.tmp.name+'/ai.sqlite',daily_calls=None,
                              opener=lambda *a,**kw:self.fail('Unexpected HTTP'),trial=budget)
        try:
            with self.assertRaisesRegex(TrialStopped,'unpriced_trial_model'):
                client.analyze(context(),NOW)
            self.assertEqual(budget.status(NOW)['requests'],0)
        finally:
            client.db.close()
    def test_incomplete_and_invalid_recommendations_still_account_usage(self):
        budget=self.budget()
        client=AnalysisClient('private',MODEL,self.tmp.name+'/ai.sqlite',daily_calls=None,
                              opener=lambda *a,**kw:Response(response(status='incomplete')),trial=budget)
        try:
            with self.assertRaisesRegex(AnalysisError,'incomplete'):
                client.analyze(context(),NOW)
            self.assertEqual(budget.status(NOW)['accounted_usd'],.0032)
        finally:
            client.db.close()


class TrialWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=NOW
        self.events=[]
        self.ai_calls=[]
        self.workers=[]
        self.transport=LiveStateTransport()
        self.transport.account=APPROVED_ACCOUNT
        self.env=dict(BOT_ACCOUNT_ID=APPROVED_ACCOUNT,CAP_API_KEY='private',CAP_IDENTIFIER='private',
                      CAP_API_PASSWORD='private',OPENAI_API_KEY='private',OPENAI_MODEL=MODEL,
                      EODHD_API_KEY='private',SMART_MARKETS='broad',SMART_MODE='demo',SMART_ARMED='DEMO_ONLY',
                      SMART_EXCLUSIVE_ACCOUNT=APPROVED_ACCOUNT,SMART_AI_CALLS_PER_DAY='4',
                      SMART_STATE_DIR=self.tmp.name+'/state',BOT_STATE_PATH=self.tmp.name+'/old.sqlite')
        clock_patch=patch('time.time',lambda:self.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.ai_action='WAIT'
        self.expire_during_http=False
    def tearDown(self):
        for worker in self.workers:
            for name in ('journal','daily','ai','schedule','trial'):
                value=getattr(worker.local,name,None)
                if value and hasattr(value,'db'):
                    value.db.close()
        self.tmp.cleanup()
    def make(self):
        cfg=Config(self.env)
        def broker(daily):
            return CapitalDemo(key='private',user='private',password='private',account_id=APPROVED_ACCOUNT,
                               daily_controller=daily,armed=True,opener=self.transport,clock=lambda:self.now,
                               sleep=lambda _:None,entry_guard=lambda:worker.trial_budget().require_entry(self.now))
        def ai():
            def opener(request,timeout):
                self.ai_calls.append(json.loads(request.data))
                if self.expire_during_http:
                    self.now=NOW+DURATION
                return Response(response(self.ai_action))
            return AnalysisClient('private',cfg.model,str(cfg.directory/'analysis.sqlite'),daily_calls=None,
                                  opener=opener,allow_missing_news=True,trial=worker.trial_budget())
        class News:
            def fetch(_self,symbol):
                return dict(status='cached',articles=[article('Euro',['EURUSD.FOREX'])])
        worker=Worker(cfg,broker,ai,News,clock=lambda:self.now,
                      emit=lambda event,**fields:self.events.append((event,fields)))
        self.workers.append(worker)
        return worker
    def collect(self,collector,epic):
        data=trending(epic)
        delta=self.now-NOW
        data.update(bid=1.10,ask=1.1001,quote_time=self.now,collected_at=self.now)
        for frame in data['timeframes'].values():
            for candle in frame['candles']:
                candle['t']+=delta
        return data
    def sweep(self,worker):
        with patch.object(MarketCollector,'collect_market',lambda collector,epic:self.collect(collector,epic)):
            for _ in MARKETS:
                worker.opportunities()
    def prime(self,worker):
        worker.positions()
        worker.gate.success('positions',self.now)
        worker.news()
        worker.gate.success('news',self.now)
    def test_approved_account_activates_bounded_trial_without_changing_other_accounts(self):
        cfg=Config(dict(self.env,OPENAI_MODEL='different-model',SMART_ANALYSIS_MODE='budgeted'))
        self.assertTrue(cfg.trial)
        self.assertTrue(cfg.continuous)
        self.assertEqual(cfg.analysis_mode,'trial')
        self.assertEqual(cfg.model,MODEL)
        self.assertEqual(len(cfg.symbols),21)
        self.assertIsNone(cfg.calls)
        other=Config(dict(self.env,BOT_ACCOUNT_ID='other',SMART_EXCLUSIVE_ACCOUNT='other'))
        self.assertFalse(other.trial)
        self.assertEqual(other.calls,4)
        with self.assertRaises(ValueError):
            Config(dict(self.env,BOT_ACCOUNT_ID='other',SMART_EXCLUSIVE_ACCOUNT='other',SMART_TRIAL_ENABLED='1'))
    def test_all_21_usage_counted_and_no_same_candle_repeat_after_restart(self):
        worker=self.make()
        self.sweep(worker)
        self.assertEqual(len(self.ai_calls),21)
        status=worker.trial_budget().status(self.now)
        self.assertAlmostEqual(status['accounted_usd'],21*.0032)
        self.sweep(self.make())
        self.assertEqual(len(self.ai_calls),21)
        self.now+=900
        self.sweep(worker)
        self.assertEqual(len(self.ai_calls),42)
        metrics=worker.trial_budget().summary(self.now)['metrics']
        self.assertEqual(sum(m['count'] for m in metrics if m['event']=='analysis_ready' and m['detail']=='WAIT'),42)
    def test_expired_trial_blocks_paid_calls_and_new_entries_even_after_restart(self):
        worker=self.make()
        self.now+=DURATION
        self.sweep(worker)
        self.sweep(self.make())
        self.assertEqual(self.ai_calls,[])
        self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls))
        self.assertIn('trial_stopped',[e for e,_ in self.events])
    def test_last_reservation_exhaustion_blocks_following_markets(self):
        worker=self.make()
        budget=worker.trial_budget()
        budget.db.execute('INSERT INTO trial_calls VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)',
                          ('previous',TRIAL_ID,APPROVED_ACCOUNT,'US30',NOW,LIMIT_NANO-1_000_000,LIMIT_NANO-1_000_000))
        budget.db.commit()
        self.sweep(worker)
        self.assertEqual(len(self.ai_calls),0)
        self.assertEqual(budget.status(self.now)['stop_reason'],'budget_exhausted')
    def test_duration_during_ai_reply_cannot_authorize_new_order(self):
        worker=self.make()
        self.prime(worker)
        self.ai_action='BUY'
        self.expire_during_http=True
        self.sweep(worker)
        self.assertEqual(len(self.ai_calls),1)
        self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls))
        self.assertEqual(worker.trial_budget().status(self.now)['accounted_usd'],.0032)
    def test_protection_closes_owned_position_after_trial_expiry(self):
        worker=self.make()
        self.prime(worker)
        self.ai_action='BUY'
        with patch.object(MarketCollector,'collect_market',lambda collector,epic:self.collect(collector,epic)):
            worker.opportunities()
        self.assertEqual(len(worker.local.journal.owned()),1)
        self.now+=DURATION
        # Fresh quotes for the position watcher, independent of AI and news.
        self.transport.stop_missing=True
        with patch.object(worker.local.broker,'market_quote',return_value=dict(bid=1.10,ask=1.1001,quote_time=self.now)):
            worker.positions()
        self.assertTrue(self.transport.closed)
        self.assertFalse(worker.local.journal.owned())
        self.sweep(worker)
        self.assertEqual(len(self.ai_calls),1)
    def test_final_pre_http_entry_guard_veto_is_known_unsent_and_not_uncertain(self):
        worker=self.make()
        self.prime(worker)
        self.ai_action='BUY'
        broker=worker.local.broker
        original=broker.submit_entry
        def expire(plan):
            # Still fresh market data, but the trial expires in this boundary.
            worker.trial_budget().db.execute('UPDATE trial_runs SET stop_reason=?',('duration_elapsed',))
            worker.trial_budget().db.commit()
            return original(plan)
        with patch.object(broker,'submit_entry',expire), patch.object(
                MarketCollector,'collect_market',lambda collector,epic:self.collect(collector,epic)):
            worker.opportunities()
        self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls))
        self.assertFalse(worker.local.journal.unresolved())
        self.assertEqual(worker.local.journal.db.execute('SELECT status FROM smart_entries').fetchone()[0],'blocked')


if __name__=='__main__':
    unittest.main()
