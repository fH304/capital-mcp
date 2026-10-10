import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from .capital import BASE, CapitalDemo, CapitalError
from .coordinator import CoordinationError, DemoCoordinator
from .exposure import entry_limits, ExposureError
from .runner import Config, Worker
from .test_analysis import NOW, context, recommendation
from .test_capital import Response
from .test_coordinator import snapshot
from .test_marketdata import PriceTransport
from .test_universe import trending
from .trial import TrialEntryBlocked


def data():
    return dict(snapshot(),hedging_mode=True,leverage=200,fx_rate=1,
                account_currency='USD',quote_currency='USD',conversion_time=NOW)


def leg():
    plan=dict(account_id='demo-1',epic='EURUSD',direction='BUY',size=100,
              stop_level=1.09,target_level=1.15,planned_risk=1,actual_risk=1,
              required_margin=.55,actual_margin=.55,leverage=200,
              quote_currency='USD',quote_to_account=1,group_equity=1000,
              signal_candle=NOW-1800)
    actual=dict(position=dict(dealId='old',direction='BUY',size=100,level=1.10,
                             stopLevel=1.09,profitLevel=1.15,leverage=200,
                             contractSize=1,currency='USD'),market=dict(epic='EURUSD'))
    return plan,actual


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.data=data()
        self.plan,self.actual=leg()
        self.data.update(bid=1.101,ask=1.102,positions=[self.actual])
        self.owned={'old':self.plan}
    def limits(self):
        return entry_limits(self.data,self.owned,direction='BUY',assessment='positive',candle=NOW-900)
    def test_second_leg_uses_remaining_combined_budget(self):
        limits=self.limits()
        self.assertAlmostEqual(limits['existing_risk'],1.1)
        self.assertAlmostEqual(limits['risk_budget'],1.25)
        self.assertEqual(limits['pair_risk_limit'],2.5)
    def test_remaining_pair_budget_reduces_next_trade(self):
        self.data.update(bid=1.112,ask=1.113)
        self.assertAlmostEqual(self.limits()['risk_budget'],.3)
    def test_profits_do_not_increase_original_group_equity(self):
        self.data['equity']=1200
        self.assertEqual(self.limits()['pair_risk_limit'],2.5)
    def test_lower_equity_reduces_combined_cap(self):
        self.data['equity']=800
        self.assertEqual(self.limits()['pair_risk_limit'],2)
    def test_current_fx_increases_original_risk_reservation(self):
        self.data['quote_to_account']=2
        self.assertAlmostEqual(self.limits()['risk_budget'],.3)
    def test_historical_conversion_is_not_released_by_favourable_fx(self):
        self.plan['quote_to_account']=2
        self.assertAlmostEqual(self.limits()['existing_risk'],2.2)
    def test_existing_margin_uses_actual_old_leverage(self):
        self.plan['leverage']=self.actual['position']['leverage']=10
        self.assertAlmostEqual(self.limits()['existing_margin'],11.02)
    def test_legacy_journal_without_group_metadata_is_not_adopted(self):
        del self.plan['group_equity']
        with self.assertRaises(ValueError): self.limits()
    def test_missing_hedging_unknown_owner_working_orders_or_full_group_blocks(self):
        for key,val in [('hedging_mode',False),('working_orders',[{}]),
                        ('positions',[self.actual,self.actual])]:
            original=copy.deepcopy(self.data)
            self.data[key]=val
            with self.subTest(key=key),self.assertRaises(ExposureError): self.limits()
            self.data=original
        self.owned={}
        with self.assertRaises(ExposureError): self.limits()
    def test_changed_identity_or_protection_blocks_addition(self):
        for key,val in [('size',101),('direction','SELL'),('stopLevel',None),
                        ('stopLevel',1.08),('profitLevel',1.16),('leverage',100),
                        ('currency','EUR'),('contractSize',10)]:
            old=self.actual['position'][key]
            self.actual['position'][key]=val
            with self.subTest(key=key),self.assertRaises(ExposureError): self.limits()
            self.actual['position'][key]=old
    def test_same_candle_and_non_positive_assessment_do_not_add(self):
        self.plan['signal_candle']=NOW-900
        with self.assertRaises(ExposureError): self.limits()
        self.plan['signal_candle']=NOW-1800
        with self.assertRaises(ExposureError):
            entry_limits(self.data,self.owned,direction='BUY',assessment='acceptable',candle=NOW-900)
    def test_no_averaging_down_or_reentry_at_flat_price(self):
        for bid in (1.10,1.099):
            self.data['bid']=bid
            with self.assertRaises(ExposureError): self.limits()
    def test_daily_remaining_and_margin_are_shared_by_both_legs(self):
        self.data['daily_remaining_risk']=1.5
        self.assertAlmostEqual(self.limits()['risk_budget'],.4)
        self.data['free_margin']=1
        with self.assertRaises(ExposureError): self.limits()
    def test_other_pair_or_direction_is_not_diversification(self):
        self.actual['market']['epic']='GBPUSD'
        with self.assertRaises(ExposureError): self.limits()


class PairTransport(PriceTransport):
    def __init__(self,clock):
        super().__init__()
        self.clock=clock
        self.positions={}
        self.references={}
        self.sequence=0
        self.leverage=200
        self.hedging=True
        self.orders=[]
        self.fail_post=False
        self.bad_confirmed_leverage=False
        self.equity=1000
        self.closed_references=set()
    def __call__(self,request,timeout):
        path=request.full_url.removeprefix(BASE)
        method=request.get_method()
        self.calls.append((method,request.full_url,request.data))
        if path=='/accounts':
            used=sum(p['position']['size']*p['position']['level']/p['position']['leverage']
                     for p in self.positions.values())
            result=dict(accounts=[dict(accountId=self.account,currency='USD',status='ENABLED',
                         accountType='CFD',balance=dict(balance=self.equity,available=self.equity-used))])
        elif path=='/accounts/preferences':
            result=dict(hedgingMode=self.hedging,leverages={
                'CURRENCIES':dict(current=self.leverage,available=[1,10,20,100,200])})
        elif path.startswith('/markets/'):
            result=copy.deepcopy(self.market)
            result['instrument']['marginFactor']=100/self.leverage
            result['snapshot']['updateTimeUTC']=datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()
        elif path=='/workingorders': result={'workingOrders':copy.deepcopy(self.orders)}
        elif path=='/positions' and method=='GET': result={'positions':list(self.positions.values())}
        elif path=='/positions' and method=='POST':
            self.sequence+=1
            deal,ref='deal-'+str(self.sequence),'o_'+str(self.sequence)
            p=json.loads(request.data)
            fill=self.market['snapshot']['offer' if p['direction']=='BUY' else 'bid']
            self.positions[deal]=dict(position=dict(dealId=deal,currency='USD',contractSize=1,
                  direction=p['direction'],size=p['size'],level=fill,leverage=self.leverage,
                  stopLevel=p['stopLevel'],profitLevel=p['profitLevel']),market=dict(epic=p['epic']))
            self.references[ref]=deal
            if self.fail_post: raise TimeoutError('transport outcome unknown')
            result={'dealReference':ref}
        elif path.startswith('/confirms/'):
            ref=path.rsplit('/',1)[1]
            result=dict(dealStatus='ACCEPTED',affectedDeals=[dict(dealId=self.references[ref],
                        status='CLOSED' if ref in self.closed_references else 'OPENED')])
        elif path.startswith('/positions/') and method=='DELETE':
            deal=path.rsplit('/',1)[1]
            self.positions.pop(deal)
            ref='close-'+deal
            self.references[ref]=deal
            self.closed_references.add(ref)
            result={'dealReference':ref}
        elif path.startswith('/positions/') and method=='GET':
            result=copy.deepcopy(self.positions[path.rsplit('/',1)[1]])
            if self.bad_confirmed_leverage: result['position']['leverage']=3
        else:
            return super().__call__(request,timeout)
        return Response(copy.deepcopy(result))


class PairIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now=NOW
        self.tmp=tempfile.TemporaryDirectory()
        self.path=self.tmp.name+'/orders.sqlite'
        self.transport=PairTransport(lambda:self.now)
        self.journal=DemoCoordinator(self.path,'demo-1',2)
        self.broker=self.adapter()
    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()
    def adapter(self):
        b=CapitalDemo(key='private',user='private',password='private',account_id='demo-1',
                      daily_controller=lambda e,t:dict(stopped=False,remaining=20),armed=True,
                      opener=self.transport,clock=lambda:self.now,sleep=lambda _:None)
        b.configure_entries(2,self.journal.owned)
        b.login()
        return b
    def context(self):
        c=dict(trending(),**context())
        c.update(bid=self.transport.market['snapshot']['bid'],ask=self.transport.market['snapshot']['offer'],
                 quote_time=self.now,collected_at=self.now,quote_currency='USD')
        for f in c['timeframes'].values():
            for bar in f['candles']: bar['t']+=self.now-NOW
        return c
    def process(self,armed=True,result=None):
        c=self.context()
        candle=c['timeframes']['MINUTE_15']['candles'][-1]['t']
        return self.journal.process(str(candle),result or recommendation(),c,self.broker,
                                    now=self.now,armed=armed)
    def later(self):
        self.now+=900
        self.transport.market['snapshot'].update(bid=1.102,offer=1.103)
    def posts(self):
        return sum(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls)
    def restart(self):
        self.journal.close()
        self.journal=DemoCoordinator(self.path,'demo-1',2)
        self.broker=self.adapter()
    def test_two_fresh_staged_entries_share_cap_and_third_is_blocked(self):
        first=self.process()
        self.assertEqual(first['status'],'confirmed')
        self.later()
        second=self.process()
        self.assertEqual(second['status'],'confirmed')
        self.assertLessEqual(second['plan']['combined_planned_risk'],2.5+1e-9)
        self.assertLessEqual(second['plan']['combined_actual_risk'],2.5+1e-9)
        self.assertLess(second['plan']['planned_risk'],1.25)
        self.later()
        self.assertEqual(self.process()['status'],'blocked')
        self.assertEqual(self.posts(),2)
    def test_second_entry_needs_later_candle(self):
        self.process()
        self.transport.market['snapshot'].update(bid=1.102,offer=1.103)
        self.assertEqual(self.process()['status'],'blocked')
        self.assertEqual(self.posts(),1)
    def test_preview_does_not_open_or_change_preferences(self):
        self.assertEqual(self.process(False)['status'],'preview')
        self.assertEqual(self.posts(),0)
        self.assertFalse(any(m=='PUT' for m,_,_ in self.transport.calls))
    def test_missing_hedging_or_malformed_leverage_never_submits(self):
        self.transport.hedging=False
        with self.assertRaises(CapitalError): self.process()
        self.assertEqual(self.posts(),0)
    def test_restart_keeps_group_ownership_and_two_position_limit(self):
        self.process()
        self.restart()
        self.later()
        self.assertEqual(self.process()['status'],'confirmed')
        self.restart()
        self.later()
        self.assertEqual(self.process()['status'],'blocked')
        self.assertEqual(self.posts(),2)
    def test_actual_old_and_new_leverages_are_recorded_separately(self):
        self.process()
        self.later()
        self.transport.leverage=100  # externally chosen; bot never sends PUT
        self.assertEqual(self.process()['status'],'confirmed')
        self.assertEqual({p['leverage'] for p in self.journal.owned().values()},{100,200})
        self.assertFalse(any(m=='PUT' for m,_,_ in self.transport.calls))
    def test_losing_leg_and_acceptable_only_signal_cannot_add(self):
        self.process()
        self.now+=900
        self.assertEqual(self.process()['status'],'blocked')
        self.transport.market['snapshot'].update(bid=1.102,offer=1.103)
        r=dict(recommendation(),assessment='acceptable')
        self.assertEqual(self.process(result=r)['status'],'blocked')
        self.assertEqual(self.posts(),1)
    def test_changed_stop_blocks_second_entry(self):
        self.process()
        self.later()
        self.transport.positions['deal-1']['position']['stopLevel']=None
        self.assertEqual(self.process()['status'],'blocked')
        self.assertEqual(self.posts(),1)
    def test_unknown_leg_is_not_adopted(self):
        self.process()
        self.later()
        self.transport.positions['deal-1']['position']['dealId']='foreign'
        self.assertEqual(self.process()['status'],'blocked')
        self.assertEqual(self.posts(),1)
    def test_ambiguous_second_post_blocks_after_restart_without_retry(self):
        self.process()
        self.later()
        self.transport.fail_post=True
        with self.assertRaises(CoordinationError): self.process()
        self.restart()
        self.later()
        with self.assertRaises(CoordinationError): self.process()
        self.assertTrue(self.journal.unresolved())
        self.assertEqual(self.posts(),2)
    def test_leverage_confirmation_mismatch_is_uncertain(self):
        self.transport.bad_confirmed_leverage=True
        with self.assertRaises(CoordinationError): self.process()
        self.assertTrue(self.journal.unresolved())
    def test_refresh_after_reservation_detects_new_working_order_before_post(self):
        original=self.broker.submit_entry
        def submit(plan):
            self.transport.orders=[{'workingOrderData':{'dealId':'foreign'}}]
            return original(plan)
        self.broker.submit_entry=submit
        self.assertEqual(self.process()['status'],'blocked')
        self.assertFalse(self.journal.unresolved())
        self.assertEqual(self.posts(),0)
    def test_sell_legs_use_ask_for_existing_downside_and_share_same_cap(self):
        r=dict(recommendation(),action='SELL',stop_level=1.111,target_level=1.07)
        self.assertEqual(self.process(result=r)['status'],'confirmed')
        self.now+=900
        self.transport.market['snapshot'].update(bid=1.098,offer=1.099)
        result=self.process(result=r)
        self.assertEqual(result['status'],'confirmed')
        self.assertLessEqual(result['plan']['combined_actual_risk'],2.5+1e-9)
        self.assertEqual(self.posts(),2)
    def test_leverage_change_during_reservation_is_known_unsent(self):
        original=self.broker.submit_entry
        def submit(plan):
            self.transport.leverage=100
            return original(plan)
        self.broker.submit_entry=submit
        self.assertEqual(self.process()['status'],'blocked')
        self.assertFalse(self.journal.unresolved())
        self.assertEqual(self.posts(),0)
    def test_price_change_invalidating_target_is_known_unsent(self):
        original=self.broker.submit_entry
        def submit(plan):
            self.transport.market['snapshot'].update(bid=1.12,offer=1.121)
            return original(plan)
        self.broker.submit_entry=submit
        self.assertEqual(self.process()['status'],'blocked')
        self.assertFalse(self.journal.unresolved())
        self.assertEqual(self.posts(),0)
    def test_protective_closure_of_both_owned_legs_remains_available(self):
        self.process()
        self.later()
        self.process()
        for p in self.transport.positions.values(): p['position']['stopLevel']=None
        env=dict(BOT_ACCOUNT_ID='demo-1',CAP_API_KEY='private',CAP_IDENTIFIER='private',
            CAP_API_PASSWORD='private',OPENAI_API_KEY='private',OPENAI_MODEL='fixture',EODHD_API_KEY='private',
            SMART_STATE_DIR=self.tmp.name,BOT_STATE_PATH=self.tmp.name+'/old.sqlite',SMART_DAILY_LOSS_LIMIT='20',
            SMART_PAIR_ENTRIES='2',SMART_MODE='demo',SMART_ARMED='DEMO_ONLY',SMART_EXCLUSIVE_ACCOUNT='demo-1')
        def factory(daily):
            b=self.adapter()
            b.daily_controller=daily
            return b
        w=Worker(Config(env),broker_factory=factory,clock=lambda:self.now,emit=lambda *args,**kw:None)
        try:
            self.assertTrue(w.positions())
            self.assertFalse(self.transport.positions)
            self.assertFalse(self.journal.owned())
            self.assertEqual(sum(m=='DELETE' for m,_,_ in self.transport.calls),2)
        finally:
            for name in ('journal','daily'):
                obj=getattr(w.local,name,None)
                if obj: obj.close()
    def test_expiry_guard_still_vetoes_staged_entries_before_http(self):
        self.process()
        self.later()
        def expired(): raise TrialEntryBlocked('duration_elapsed')
        self.broker.entry_guard=expired
        self.assertEqual(self.process()['status'],'blocked')
        self.assertFalse(self.journal.unresolved())
        self.assertEqual(self.posts(),1)
    def test_stale_snapshot_does_not_erase_a_new_journal_owned_leg(self):
        stale=self.broker.snapshot('EURUSD')
        self.process()
        self.broker.snapshot=lambda epic:stale
        self.assertEqual(self.process()['status'],'blocked')
        self.assertIn('deal-1',self.journal.owned())
        self.assertEqual(self.posts(),1)


class PairWorkerTests(unittest.TestCase):
    def test_worker_routes_later_candidate_with_owned_first_leg_to_second_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            now=[NOW]
            transport=PairTransport(lambda:now[0])
            env=dict(BOT_ACCOUNT_ID='demo-1',CAP_API_KEY='private',CAP_IDENTIFIER='private',
                CAP_API_PASSWORD='private',OPENAI_API_KEY='private',OPENAI_MODEL='fixture',EODHD_API_KEY='private',
                SMART_STATE_DIR=folder+'/state',BOT_STATE_PATH=folder+'/old.sqlite',SMART_DAILY_LOSS_LIMIT='20',
                SMART_PAIR_ENTRIES='2',SMART_MODE='demo',SMART_ARMED='DEMO_ONLY',SMART_EXCLUSIVE_ACCOUNT='demo-1')
            cfg=Config(env)
            def factory(daily):
                return CapitalDemo(key='private',user='private',password='private',account_id='demo-1',
                    daily_controller=daily,armed=True,opener=transport,clock=lambda:now[0],sleep=lambda _:None)
            events=[]
            w=Worker(cfg,broker_factory=factory,clock=lambda:now[0],emit=lambda e,**kw:events.append((e,kw)))
            try:
                with patch('time.time',lambda:now[0]):
                    for index in range(3):
                        c=dict(trending(),**context())
                        c.update(bid=transport.market['snapshot']['bid'],ask=transport.market['snapshot']['offer'],quote_time=now[0])
                        for f in c['timeframes'].values():
                            for bar in f['candles']: bar['t']+=now[0]-NOW
                        b,j=w.resources()
                        state,reconciled=w.entry_state(b,j)
                        for name in ('positions','news','opportunities'): w.gate.success(name,now[0])
                        w.evaluate('EURUSD',c,c['articles'],state,reconciled,b,j,recommendation())
                        now[0]+=900
                        transport.market['snapshot'].update(bid=1.102,offer=1.103)
                self.assertEqual(sum(e=='entry_confirmed' for e,_ in events),2)
                self.assertEqual(events[-1][1]['reason'],'pair_position_limit')
            finally:
                for name in ('journal','daily','spread_study'):
                    obj=getattr(w.local,name,None)
                    if obj: obj.close()
    def test_configuration_refuses_more_than_two_and_preserves_other_account_default(self):
        env=dict(BOT_ACCOUNT_ID='demo-1',CAP_API_KEY='private',CAP_IDENTIFIER='private',CAP_API_PASSWORD='private',
                 OPENAI_API_KEY='private',OPENAI_MODEL='fixture',EODHD_API_KEY='private')
        self.assertEqual(Config(env).max_positions,1)
        with patch('smarttrader.runner.is_approved_account',return_value=True):
            self.assertEqual(Config(dict(env,SMART_MARKETS='broad')).max_positions,2)
        for value in ('0','3','200','bad'):
            with self.assertRaises(ValueError): Config(dict(env,SMART_PAIR_ENTRIES=value))
