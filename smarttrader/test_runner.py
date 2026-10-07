import json
import tempfile
import unittest
from unittest.mock import patch

from .analysis import AnalysisClient
from .capital import BASE, CapitalDemo, CapitalError
from .coordinator import CoordinationError, DemoCoordinator
from .runner import Config, Worker, WorkerLock
from .test_analysis import NOW, Response as AIResponse, context
from .test_capital import Response
from .test_marketdata import PriceTransport


class LiveStateTransport(PriceTransport):
    def __init__(self):
        super().__init__()
        self.closed=False
        self.close_fail=False
        self.unknown=False
        self.equity=1000
    def actual(self):
        p=self.position
        return dict(position=dict(dealId='deal-1',currency='USD',contractSize=1,
                    direction=p['direction'],size=p['size'],level=self.fill,
                    stopLevel=None if self.stop_missing else p['stopLevel'],profitLevel=p['profitLevel']),
                    market=dict(epic=p['epic']))
    def __call__(self,request,timeout):
        path=request.full_url.removeprefix(BASE)
        if path=='/accounts':
            return Response(dict(accounts=[dict(accountId=self.account,currency='USD',status='ENABLED',accountType='CFD',
                        balance=dict(balance=self.equity,available=self.equity,profitLoss=0))]))
        if request.get_method()=='DELETE':
            self.calls.append(('DELETE',request.full_url,request.data))
            self.closed=True
            if self.close_fail:
                raise TimeoutError('credential-bearing detail')
            return Response({'dealReference':'close-1'})
        if path=='/confirms/close-1':
            return Response(dict(dealStatus='ACCEPTED',affectedDeals=[dict(dealId='deal-1',status='CLOSED')]))
        if path=='/positions' and request.get_method()=='GET':
            self.calls.append(('GET',request.full_url,None))
            actual=[self.actual()] if self.position and not self.closed else []
            if self.unknown:
                actual=[dict(position=dict(dealId='foreign',direction='BUY',size=1),market=dict(epic='EURUSD'))]
            return Response({'positions':actual})
        return super().__call__(request,timeout)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.transport=LiveStateTransport()
        self.events=[]
        self.ai_calls=[]
        self.env=dict(BOT_ACCOUNT_ID='demo-1',CAP_API_KEY='private',CAP_IDENTIFIER='private',
                      CAP_API_PASSWORD='private',OPENAI_API_KEY='private',OPENAI_MODEL='gpt-4.1-mini',
                      EODHD_API_KEY='private',SMART_STATE_DIR=self.tmp.name+'/state',
                      BOT_STATE_PATH=self.tmp.name+'/old.sqlite',SMART_DAILY_LOSS_LIMIT='250')
        self.workers=[]
    def tearDown(self):
        for w in self.workers:
            for name in ('journal','daily','ai','news'):
                obj=getattr(w.local,name,None)
                if obj and hasattr(obj,'db'):
                    obj.db.close()
            if hasattr(w.local,'scans'):
                w.local.scans.close()
        self.tmp.cleanup()
    def make(self,armed=False):
        env=dict(self.env)
        if armed:
            env.update(SMART_MODE='demo',SMART_ARMED='DEMO_ONLY',SMART_EXCLUSIVE_ACCOUNT='demo-1')
        cfg=Config(env)
        def broker(daily):
            return CapitalDemo(key='private',user='private',password='private',account_id='demo-1',
                    daily_controller=daily,armed=cfg.armed,opener=self.transport,clock=lambda:NOW,sleep=lambda _:None)
        def ai_open(request,timeout):
            self.ai_calls.append(json.loads(request.data))
            return AIResponse()
        def ai():
            return AnalysisClient('private',cfg.model,str(cfg.directory/'analysis.sqlite'),opener=ai_open)
        article=dict(context()['articles'][0],title='Euro news',content='Evidence',symbols=['EURUSD.FOREX'])
        class News:
            def fetch(self,symbol):
                return dict(status='fetched',articles=[article])
        w=Worker(cfg,broker,ai,News,clock=lambda:NOW,emit=lambda event,**fields:self.events.append((event,fields)))
        self.workers.append(w)
        return w
    def cycle(self,w):
        for name in ('positions','news','opportunities'):
            usable=w.job(name)()
            if usable:
                w.gate.success(name,NOW)
            else:
                w.gate.failure(name)
    @patch('time.time',return_value=NOW)
    def test_preview_full_data_to_openai_to_sizing_without_orders(self,_):
        w=self.make()
        self.cycle(w)
        self.assertEqual(len(self.ai_calls),1)
        self.assertIn('entry_preview',[e for e,_ in self.events])
        self.assertIsNone(self.transport.position)
        self.assertFalse(any(m=='DELETE' for m,_,_ in self.transport.calls))
        w.opportunities()
        self.assertEqual(len(self.ai_calls),1)  # persistent candle reservation
    @patch('time.time',return_value=NOW)
    def test_demo_open_then_missing_stop_close_and_no_resend(self,_):
        w=self.make(True)
        self.cycle(w)
        self.assertEqual(len(w.local.journal.owned()),1)
        self.transport.stop_missing=True
        w.positions()
        self.assertTrue(self.transport.closed)
        self.assertFalse(w.local.journal.owned())
        w.positions()
        self.assertEqual(sum(m=='DELETE' for m,_,_ in self.transport.calls),1)
        self.assertIn('closed',[e for e,_ in self.events])
    @patch('time.time',return_value=NOW)
    def test_close_timeout_persists_and_blocks_after_restart(self,_):
        w=self.make(True)
        self.cycle(w)
        self.transport.stop_missing=self.transport.close_fail=True
        with self.assertRaises(CoordinationError):
            w.positions()
        restarted=self.make(True)
        self.assertFalse(restarted.positions())
        self.assertTrue(restarted.local.journal.unresolved())
        self.assertEqual(sum(m=='DELETE' for m,_,_ in self.transport.calls),1)
        row=restarted.local.journal.db.execute('SELECT status,reference FROM smart_exits').fetchone()
        self.assertEqual(row,('uncertain',None))
    @patch('time.time',return_value=NOW)
    def test_unknown_positions_are_never_adopted_or_closed(self,_):
        self.transport.unknown=True
        w=self.make(True)
        self.cycle(w)
        self.assertEqual(len(self.ai_calls),0)
        self.assertFalse(any(m=='DELETE' for m,_,_ in self.transport.calls))
        self.assertFalse(w.gate.may_enter(NOW))
    @patch('time.time',return_value=NOW)
    def test_preview_does_not_close_previously_owned_position(self,_):
        live=self.make(True)
        self.cycle(live)
        self.transport.stop_missing=True
        preview=self.make()
        preview.positions()
        self.assertIn('close_preview',[e for e,_ in self.events])
        self.assertFalse(self.transport.closed)
    def test_old_and_new_workers_share_exclusive_lock(self):
        path=self.env['BOT_STATE_PATH']+'.lock'
        first=WorkerLock(path)
        try:
            with self.assertRaises(RuntimeError):
                WorkerLock(path)
        finally:
            first.close()
        second=WorkerLock(path)
        second.close()
    def test_live_and_incomplete_arming_refused(self):
        for update in ({'CAP_ENV':'live'},{'SMART_MODE':'live'},{'SMART_MODE':'demo'},
                       {'SMART_MODE':'demo','SMART_ARMED':'DEMO_ONLY','SMART_EXCLUSIVE_ACCOUNT':'wrong'}):
            with self.assertRaises(ValueError):
                Config(dict(self.env,**update))
    @patch('time.time',return_value=NOW)
    def test_changed_owned_size_not_closed(self,_):
        w=self.make(True)
        self.cycle(w)
        self.transport.position['size']+=1
        self.assertFalse(w.positions())
        self.assertFalse(any(m=='DELETE' for m,_,_ in self.transport.calls))
    @patch('time.time',return_value=NOW)
    def test_closed_position_reconciles_without_fake_profit(self,_):
        w=self.make(True)
        self.cycle(w)
        self.transport.closed=True
        w.positions()
        self.assertFalse(w.local.journal.owned())
        self.assertEqual(w.local.journal.db.execute('SELECT status FROM smart_entries').fetchone()[0],'closed')
        self.assertFalse(any(m=='DELETE' for m,_,_ in self.transport.calls))

    @patch('time.time',return_value=NOW)
    def test_daily_loss_closes_owned_position_and_latches_entry_stop(self,_):
        w=self.make(True)
        self.cycle(w)
        self.transport.equity=749
        w.positions()
        self.assertTrue(self.transport.closed)
        self.transport.equity=1000
        w.positions()
        self.assertTrue(w.gate.daily_stopped)
        self.assertFalse(w.gate.may_enter(NOW))
    @patch('time.time',return_value=NOW)
    def test_restart_preserves_analysis_spacing(self,_):
        w=self.make()
        self.cycle(w)
        restarted=self.make()
        self.cycle(restarted)
        self.assertEqual(len(self.ai_calls),1)
        self.assertIn('analysis_scheduled',[e for e,_ in self.events])
    @patch('time.time',return_value=NOW)
    def test_unarmed_low_level_close_is_rejected(self,_):
        w=self.make()
        broker,_=w.resources()
        with self.assertRaises(CapitalError):
            broker._request('DELETE','/positions/deal-1')
