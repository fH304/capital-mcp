import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from .analysis import AnalysisClient, AnalysisError
from .capital import CapitalDemo, CapitalError
from .marketdata import MarketCollector
from .runner import Config, Worker
from .test_analysis import NOW, recommendation
from .test_runner import LiveStateTransport
from .test_universe import MARKETS, article, trending


def wait():
    return dict(action='WAIT',assessment='reject',stop_level=0,target_level=0,
                reason='Technical monitoring; no entry candidate',article_ids=[])


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=NOW
        clock_patch=patch('time.time',lambda:self.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.env=dict(BOT_ACCOUNT_ID='demo-1',CAP_API_KEY='private',CAP_IDENTIFIER='private',CAP_API_PASSWORD='private',
                      OPENAI_API_KEY='private',OPENAI_MODEL='fixture',EODHD_API_KEY='private',SMART_MARKETS='broad',
                      SMART_ANALYSIS_MODE='continuous',
                      SMART_AI_CALLS_PER_DAY='4',SMART_STATE_DIR=self.tmp.name+'/state',
                      BOT_STATE_PATH=self.tmp.name+'/old.sqlite')
        self.transport=LiveStateTransport()
        self.events=[]
        self.contexts=[]
        self.workers=[]
        self.closed=set()
        self.failed=set()
        self.buy=set()
        self.news=[article('Euro', ['EURUSD.FOREX'])]
    def tearDown(self):
        for worker in self.workers:
            for name in ('journal','daily','ai','news','schedule','spread_study'):
                obj=getattr(worker.local,name,None)
                if obj and hasattr(obj,'db'):
                    obj.db.close()
        self.tmp.cleanup()
    def make(self,real_ai=False):
        cfg=Config(self.env)
        def broker(daily):
            return CapitalDemo(key='private',user='private',password='private',account_id='demo-1',
                               daily_controller=daily,armed=cfg.armed,opener=self.transport,
                               clock=lambda:self.now,sleep=lambda _:None)
        outer=self
        class AI:
            def analyze(self,ctx):
                outer.contexts.append(copy.deepcopy(ctx))
                if ctx['epic'] in outer.failed:
                    raise AnalysisError('OpenAI HTTP status 429')
                if ctx['epic'] in outer.buy:
                    return recommendation()
                return wait()
        class News:
            def fetch(self,symbol):
                return dict(status='cached',articles=outer.news)
        def ai_factory():
            if not real_ai:
                return AI()
            class Response:
                def __enter__(self):
                    return self
                def __exit__(self,*args):
                    pass
                def read(self,size):
                    return json.dumps(dict(status='completed',output=[dict(type='message',content=[
                        dict(type='output_text',text=json.dumps(wait()))])])).encode()
            def opener(request,timeout):
                payload=json.loads(request.data)
                self.assertNotIn('tools',payload)
                self.contexts.append(json.loads(payload['input']))
                return Response()
            return AnalysisClient('private',cfg.model,str(cfg.directory/'analysis.sqlite'),
                                  daily_calls=cfg.calls,allow_missing_news=cfg.continuous,opener=opener)
        worker=Worker(cfg,broker,ai_factory,News,clock=lambda:self.now,
                      emit=lambda event,**fields:self.events.append((event,fields)))
        self.workers.append(worker)
        return worker
    def collect(self,collector,epic):
        if epic in self.closed:
            raise CapitalError('Closed market')
        data=trending(epic)
        delta=self.now-NOW
        data.update(bid=1.10,ask=1.1001,quote_time=self.now,collected_at=self.now)
        for candle in data['timeframes']['MINUTE_15']['candles']:
            candle['t']+=delta
        # Monitoring must include contracts which cannot currently execute.
        if epic=='USDJPY':
            data.update(execution_supported=False,quote_currency='JPY')
        # Monitoring must also include markets which fail the entry ranking.
        if epic=='GBPUSD':
            data['timeframes']['HOUR']['candles'][-1]['c']=.9
            data['timeframes']['HOUR']['candles'][-1]['l']=.89
        return data
    def sweep(self,worker):
        with patch.object(MarketCollector,'collect_market',lambda collector,epic:self.collect(collector,epic)):
            for _ in MARKETS:
                worker.opportunities()
    def prime(self,worker):
        worker.positions()
        worker.gate.success('positions',self.now)
        if worker.news():
            worker.gate.success('news',self.now)
    def test_all_21_analyzed_without_news_entry_rank_or_account_readiness(self):
        self.news=[]
        self.transport.unknown=True
        worker=self.make()
        self.prime(worker)
        self.sweep(worker)
        self.assertEqual([ctx['epic'] for ctx in self.contexts],list(MARKETS))
        self.assertTrue(all(set(ctx['timeframes'])=={'MINUTE_15','HOUR','HOUR_4'} for ctx in self.contexts))
        self.assertTrue(all(ctx['articles']==[] for ctx in self.contexts))
        self.assertEqual(len([e for e,_ in self.events if e=='analysis_ready']),21)
        self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls))
    def test_wait_and_restart_do_not_repeat_candle_but_next_candle_covers_all_markets(self):
        worker=self.make()
        self.sweep(worker)
        self.sweep(worker)
        self.sweep(self.make())
        self.assertEqual(len(self.contexts),21)
        self.now+=900
        self.sweep(worker)
        self.assertEqual(len(self.contexts),42)
        self.assertEqual({ctx['epic'] for ctx in self.contexts[21:]},set(MARKETS))
    def test_one_closed_market_and_one_ai_failure_do_not_block_other_19(self):
        self.closed={'US30'}
        self.failed={'EURUSD'}
        worker=self.make()
        self.sweep(worker)
        self.assertEqual(len(self.contexts),20)
        self.assertEqual(len([e for e,_ in self.events if e=='analysis_ready']),19)
        failure=[f for e,f in self.events if e=='analysis_failed'][0]
        self.assertEqual(failure['http_status'],429)
        self.assertNotIn('private',json.dumps(self.events))
        self.sweep(worker)
        self.assertEqual(len(self.contexts),20)
        self.now+=900
        self.failed.clear()
        self.sweep(worker)
        self.assertEqual(len(self.contexts),40)
    def test_legacy_six_hour_schedule_is_ignored_without_deleting_it(self):
        worker=self.make()
        path=str(worker.config.directory/'scans.sqlite')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE analysis_schedule(account TEXT PRIMARY KEY,next_at REAL NOT NULL)')
            db.execute('INSERT INTO analysis_schedule VALUES(?,?)',('demo-1',NOW+21600))
        self.sweep(worker)
        self.assertEqual(len(self.contexts),21)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT next_at FROM analysis_schedule').fetchone()[0],NOW+21600)
    def test_continuous_requires_explicit_activation_and_does_not_raise_existing_cost_on_deploy(self):
        cfg=Config(self.env)
        self.assertEqual(cfg.analysis_mode,'continuous')
        self.assertIsNone(cfg.calls)
        self.assertEqual(Config(dict(self.env,SMART_ANALYSIS_MODE='budgeted')).calls,4)
        existing=dict(self.env)
        del existing['SMART_ANALYSIS_MODE']
        self.assertEqual(Config(existing).analysis_mode,'budgeted')
        self.assertEqual(Config(existing).calls,4)
        for update in ({'SMART_ANALYSIS_MODE':'invalid'},{'SMART_ANALYSIS_INTERVAL_SECONDS':'-1'}):
            with self.assertRaises(ValueError):
                Config(dict(self.env,**update))
    def test_real_responses_client_analyzes_all_21_despite_exhausted_old_budget(self):
        worker=self.make(real_ai=True)
        path=str(worker.config.directory/'analysis.sqlite')
        day=datetime.fromtimestamp(NOW,timezone.utc).date().isoformat()
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE ai_budget(day TEXT PRIMARY KEY,used INTEGER NOT NULL)')
            db.execute('INSERT INTO ai_budget VALUES(?,1000)',(day,))
        self.sweep(worker)
        self.assertEqual(len(self.contexts),21)
        self.assertEqual({ctx['epic'] for ctx in self.contexts},set(MARKETS))
        self.assertFalse(any(e=='analysis_failed' for e,_ in self.events))
        self.assertEqual(worker.local.ai.db.execute('SELECT used FROM ai_budget').fetchone()[0],1021)
    def test_preview_entry_runs_once_while_analysis_continues_all_21(self):
        self.buy={'EURUSD'}
        worker=self.make()
        self.prime(worker)
        self.sweep(worker)
        self.assertEqual(len(self.contexts),21)
        self.assertIn('entry_preview',[e for e,_ in self.events])
        self.sweep(worker)
        self.assertEqual(len([e for e,_ in self.events if e=='entry_preview']),1)
        self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls))
    def test_analysis_continues_when_existing_position_blocks_a_buy(self):
        self.transport.unknown=True
        self.buy={'EURUSD'}
        worker=self.make()
        self.prime(worker)
        self.sweep(worker)
        self.assertEqual(len(self.contexts),21)
        self.assertIn('entry_blocked',[e for e,_ in self.events])
        self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls))
    def test_demo_confirms_only_one_order_and_keeps_analyzing_after_entry(self):
        self.env.update(SMART_MODE='demo',SMART_ARMED='DEMO_ONLY',SMART_EXCLUSIVE_ACCOUNT='demo-1')
        self.buy={'EURUSD'}
        worker=self.make()
        self.prime(worker)
        self.sweep(worker)
        self.assertEqual(len(self.contexts),21)
        self.assertIn('entry_confirmed',[e for e,_ in self.events])
        self.assertEqual(len(worker.local.journal.owned()),1)
        self.now+=900
        self.sweep(worker)
        self.assertEqual(len(self.contexts),42)
        self.assertEqual(sum(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls),1)
    def test_additional_interval_is_per_market_not_shared(self):
        self.env['SMART_ANALYSIS_INTERVAL_SECONDS']='3600'
        worker=self.make()
        self.sweep(worker)
        self.assertEqual(len(self.contexts),21)
        self.now+=900
        self.sweep(worker)
        self.assertEqual(len(self.contexts),21)
        self.now+=2700
        self.sweep(worker)
        self.assertEqual(len(self.contexts),42)
