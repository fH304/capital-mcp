import copy
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request

from .capital import BASE, CapitalDemo, CapitalError
from .coordinator import CoordinationError, DemoCoordinator, EntryPreflightBlocked, plan_entry
from .fx import ROUTES, conversion
from .runner import Worker
from .test_analysis import NOW, context, recommendation
from .test_capital import Response, market
from .test_runner import LiveStateTransport


def contract(epic, currency, bid, ask, kind='INDICES', minimum=.001, step=.001):
    data = market()
    data['instrument'].update(epic=epic, currency=currency, type=kind,
                              marginFactor=5 if kind=='INDICES' else 3.333333)
    data['snapshot'].update(bid=bid, offer=ask)
    for name, value in (('minDealSize',minimum),('minSizeIncrement',step),
                        ('maxStopOrProfitDistance',100000)):
        data['dealingRules'][name]['value']=value
    return data


class FXTransport(LiveStateTransport):
    def __init__(self):
        super().__init__()
        self.markets={
            'DE40':contract('DE40','EUR',19000,19004),
            'UK100':contract('UK100','GBP',8000,8003,minimum=.01,step=.01),
            'J225':contract('J225','JPY',30000,30010,minimum=.1,step=.01),
        }
        for epic,bid,ask in (('EURUSD',1.10,1.101),('GBPUSD',1.25,1.251),
                             ('AUDUSD',.65,.6501),('NZDUSD',.60,.6001),
                             ('USDJPY',150,150.02),('USDCHF',.92,.921),('USDCAD',1.35,1.351)):
            self.markets[epic]=contract(epic,epic[3:],bid,ask,'CURRENCIES',100,100)
        self.failed=set()
        self.position_currency=None

    def actual(self):
        result=super().actual()
        result['position']['currency']=(self.position_currency or
                                       self.markets[self.position['epic']]['instrument']['currency'])
        return result

    def __call__(self,request,timeout):
        path=request.full_url.removeprefix(BASE)
        if path.startswith('/markets/'):
            self.calls.append(('GET',request.full_url,None))
            epic=path.rsplit('/',1)[1]
            if epic in self.failed:
                raise TimeoutError('unavailable FX quote')
            return Response(copy.deepcopy(self.markets[epic]))
        if path=='/positions/deal-1' and request.get_method()=='GET':
            return Response(self.actual())
        return super().__call__(request,timeout)


class FXTests(unittest.TestCase):
    def setUp(self):
        self.transport=FXTransport()
        self.now=NOW
        self.broker=self.adapter()
        self.tmp=tempfile.TemporaryDirectory()
        self.path=self.tmp.name+'/orders.sqlite'
        self.journal=DemoCoordinator(self.path,'demo-1')

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def adapter(self):
        result=CapitalDemo(key='private',user='private',password='private',account_id='demo-1',
                           daily_controller=lambda e,t:dict(stopped=False,remaining=20),armed=True,
                           opener=self.transport,clock=lambda:self.now,sleep=lambda _:None)
        result.login()
        return result

    def candidate(self,epic='DE40',direction='BUY',loss=None):
        snap=self.transport.markets[epic]['snapshot']
        c=dict(context(),epic=epic,bid=snap['bid'],ask=snap['offer'])
        entry=c['ask'] if direction=='BUY' else c['bid']
        loss=loss or {'DE40':100,'UK100':70,'J225':100,'USDJPY':.32,
                     'USDCHF':.003,'USDCAD':.004}.get(epic,.01)
        r=dict(recommendation(),action=direction,
               stop_level=entry-loss if direction=='BUY' else entry+loss,
               target_level=entry+3*loss if direction=='BUY' else entry-3*loss)
        return r,c

    def process(self,epic='DE40',direction='BUY'):
        r,c=self.candidate(epic,direction)
        self.transport.fill=c['ask'] if direction=='BUY' else c['bid']
        return self.journal.process('bar-1',r,c,self.broker,now=self.now,armed=True)

    def test_direct_and_inverse_rates_include_loss_fee_but_not_margin_fee(self):
        for currency,epic in ROUTES.items():
            with self.subTest(currency=currency):
                data=self.broker.snapshot('J225' if currency=='JPY' else 'DE40')
                q=self.broker._quote(self.transport.markets[epic],epic)
                fx=conversion(currency,q,NOW)
                expected=q['ask'] if epic.endswith('USD') else 1/q['bid']
                self.assertAlmostEqual(fx['fx_rate'],expected)
                self.assertAlmostEqual(fx['quote_to_account'],expected*1.007)
        self.assertAlmostEqual(data['margin_per_unit'],19004*.05*1.101)
        usd=conversion('USD',None,NOW)
        self.assertEqual(usd['quote_to_account'],1)
        self.assertEqual(usd['conversion_fee_buffer'],0)

    def test_indices_and_non_usd_forex_have_verified_execution_routes(self):
        for epic in ('DE40','UK100','J225','USDJPY','USDCHF','USDCAD'):
            with self.subTest(epic=epic):
                self.assertTrue(self.broker.observation_quote(epic)['execution_supported'])
                s=self.broker.snapshot(epic)
                r,c=self.candidate(epic)
                plan=plan_entry(r,c,s,NOW)
                self.assertEqual(plan['account_currency'],'USD')
                self.assertEqual(plan['quote_currency'],self.transport.markets[epic]['instrument']['currency'])
                self.assertLessEqual(plan['planned_risk'],1.25)
                self.assertLessEqual(plan['required_margin'],100)
                self.assertAlmostEqual(plan['planned_risk'],plan['size']*(c['ask']-r['stop_level'])*s['quote_to_account'])
                self.assertAlmostEqual(plan['required_margin'],plan['size']*s['margin_per_unit'])

    def test_foreign_orders_confirm_and_owned_positions_close_after_restart_without_fx(self):
        for epic,direction in (('DE40','BUY'),('J225','SELL')):
            with self.subTest(epic=epic), tempfile.TemporaryDirectory() as directory:
                t=FXTransport()
                self.transport=t
                b=self.adapter()
                r,c=self.candidate(epic,direction)
                t.fill=c['ask'] if direction=='BUY' else c['bid']
                path=directory+'/orders.sqlite'
                j=DemoCoordinator(path,'demo-1')
                result=j.process('bar-1',r,c,b,now=NOW,armed=True)
                self.assertEqual(result['status'],'confirmed')
                self.assertLessEqual(result['plan']['actual_risk'],1.25)
                self.assertLessEqual(result['plan']['actual_margin'],100)
                fx_epic=result['plan']['fx_epic']
                j.close()
                j=DemoCoordinator(path,'demo-1')
                try:
                    t.failed.add(fx_epic)
                    # J225's price remains available even though USDJPY fails.
                    b=self.adapter()
                    self.assertIn('deal-1',j.reconcile(b.account_state())['owned'])
                    self.assertEqual(j.close_position('deal-1','missing_broker_stop',b,armed=True)['status'],'closed')
                    self.assertFalse(j.owned())
                    self.assertEqual(sum(m=='DELETE' for m,_,_ in t.calls),1)
                finally:
                    j.close()

    def test_stale_missing_crossed_delayed_and_mismatched_fx_never_allow_entry(self):
        for change in ('stale','missing','crossed','delayed','epic','currency','type','scale','nan'):
            with self.subTest(change=change):
                self.transport=FXTransport()
                b=self.adapter()
                m=self.transport.markets['EURUSD']
                if change=='stale':
                    m['snapshot']['updateTimeUTC']=datetime.fromtimestamp(NOW-31,timezone.utc).isoformat()
                elif change=='missing': self.transport.failed.add('EURUSD')
                elif change=='crossed': m['snapshot']['offer']=1
                elif change=='delayed': m['snapshot']['delayTime']=15
                elif change=='epic': m['instrument']['epic']='GBPUSD'
                elif change=='currency': m['instrument']['currency']='EUR'
                elif change=='type': m['instrument']['type']='INDICES'
                elif change=='scale': m['snapshot']['scalingFactor']=100
                elif change=='nan': m['snapshot']['bid']=float('nan')
                self.assertFalse(b.observation_quote('DE40')['execution_supported'])
                with self.assertRaises((CapitalError,CoordinationError)):
                    b.snapshot('DE40')
                self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls))

    def test_order_revalidation_converts_loss_and_margin_before_post(self):
        for size,loss in ((.012,100),(.1,4.1)):
            with self.subTest(size=size):
                s=self.broker.snapshot('DE40')
                r,c=self.candidate(loss=loss)
                plan=plan_entry(r,c,s,NOW)
                plan['size']=size
                # Raw EUR risk/margin fits; converting to USD breaches a cap.
                self.assertLessEqual(size*loss,1.25)
                self.assertLessEqual(size*c['ask']*.05,100)
                with self.assertRaisesRegex(CapitalError,'Risk or margin'):
                    self.broker.submit_entry(plan)
        self.assertIsNone(self.transport.position)

    def test_conversion_change_after_fill_is_checked_for_risk_and_margin(self):
        for loss in (100,4.1):
            with self.subTest(loss=loss):
                self.transport=FXTransport()
                b=self.adapter()
                r,c=self.candidate(loss=loss)
                self.transport.fill=c['ask']
                s=b.snapshot('DE40')
                plan=plan_entry(r,c,s,NOW)
                ref=b.submit_entry(plan)
                self.transport.markets['EURUSD']['snapshot'].update(bid=2,offer=2.001)
                with self.assertRaisesRegex(CapitalError,'Actual position differs'):
                    b.confirm_entry(ref)
                self.assertIn(ref,b.pending)
                self.assertEqual(sum(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls),1)

    def test_position_currency_mismatch_remains_uncertain(self):
        self.transport.position_currency='USD'
        with self.assertRaises(CoordinationError): self.process()
        self.assertTrue(self.journal.unresolved())

    def test_currency_mismatch_in_owned_position_prevents_adoption_and_close(self):
        self.process()
        self.transport.position_currency='GBP'
        self.assertEqual(self.journal.reconcile(self.broker.account_state())['changed'],['deal-1'])
        with self.assertRaises(CapitalError):
            self.broker.submit_close('deal-1',self.journal.owned()['deal-1'])
        self.assertFalse(self.transport.closed)

    def test_expiry_during_local_pacing_is_known_unsent(self):
        self.broker.entry_guard=lambda:setattr(self,'now',NOW+31)
        result=self.process()
        self.assertEqual(result['status'],'blocked')
        self.assertFalse(self.journal.unresolved())
        self.assertIsNone(self.transport.position)

    def test_conversion_expiry_is_checked_independently_of_contract_quote(self):
        s=self.broker.snapshot('DE40')
        s['conversion_time']=NOW-31
        r,c=self.candidate()
        with self.assertRaisesRegex(CoordinationError,'currency conversion'):
            plan_entry(r,c,s,NOW)
        with self.assertRaises(EntryPreflightBlocked):
            self.broker._entry_fresh(s)

    def test_scan_cache_expires_and_entry_requires_a_new_conversion_quote(self):
        self.assertTrue(self.broker.observation_quote('DE40')['execution_supported'])
        self.transport.failed.add('EURUSD')
        # Within 30s, observation may reuse the validated quote, entry may not.
        self.assertTrue(self.broker.observation_quote('DE40')['execution_supported'])
        with self.assertRaises(CapitalError): self.broker.snapshot('DE40')
        self.now+=31
        self.transport.markets['DE40']['snapshot']['updateTimeUTC']=datetime.fromtimestamp(self.now,timezone.utc).isoformat()
        self.assertFalse(self.broker.observation_quote('DE40')['execution_supported'])

    def test_global_pacing_checks_freshness_before_http(self):
        worker=object.__new__(Worker)
        worker.rate_lock=threading.Lock()
        worker.next_request=worker.next_login=0
        worker.config=SimpleNamespace(trial=False)
        request=Request(BASE+'/positions',method='POST')
        s=self.broker.snapshot('DE40')
        request.capital_preflight=lambda:self.broker._entry_fresh(s)
        with patch('time.sleep',side_effect=lambda _:setattr(self,'now',NOW+31)), patch('urllib.request.build_opener') as opener:
            with self.assertRaises(EntryPreflightBlocked):
                worker.paced_open(request,20)
            opener.assert_not_called()

    def test_bad_pure_fx_inputs_and_future_quotes_are_rejected(self):
        q=self.broker._quote(self.transport.markets['USDJPY'],'USDJPY')
        for key,value in (('bid',0),('bid',True),('bid',5e-324),('ask',float('inf')),
                          ('quote_time',NOW+1),('quote_time',None)):
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                conversion('JPY',dict(q,**{key:value}),NOW)
        with self.assertRaises(ValueError): conversion('XYZ',q,NOW)

    def test_no_fx_requests_for_existing_usd_contracts(self):
        quote=self.broker.snapshot('EURUSD')
        self.assertEqual(quote['quote_to_account'],1)
        markets=[u.rsplit('/',1)[1] for m,u,_ in self.transport.calls if '/markets/' in u]
        self.assertEqual(markets,['EURUSD'])
