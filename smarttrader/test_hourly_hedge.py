import hashlib
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .coordinator import DemoCoordinator
from .capital import CapitalDemo
from .hourly_hedge import hour_start, market_epics, plan_entry, should_close
from .runner import Worker
from .monitor import EntryGate
from .test_exposure import PairTransport
from .trial import APPROVED_ACCOUNT


NOW = 1800000120.0
HOUR = hour_start(NOW)


def snapshot(positions=(), owned=None):
    return dict(environment='demo',account_id=APPROVED_ACCOUNT,epic='EURUSD',
                equity=1000,free_margin=1000,account_time=NOW,positions=list(positions),
                positions_time=NOW,working_orders=[],orders_time=NOW,contract_time=NOW,
                bid=1.1,ask=1.1001,quote_time=NOW,daily_stopped=False,
                quote_currency='USD',account_currency='USD',
                daily_remaining_risk=10,tradeable=True,point_value=1,
                quote_to_account=1,fx_rate=1,conversion_time=NOW,
                margin_per_unit=.0055005,min_size=1,size_step=1,max_size=100000,
                hedging_mode=True,leverage=200,margin_factor=.005,
                position_quotes={p['position']['dealId']:
                    dict(epic='EURUSD',quote_currency='USD',bid=1.1,ask=1.1001,
                         quote_time=NOW,conversion_time=NOW,quote_to_account=1,fx_rate=1,
                         margin_factor=.005)
                    for p in positions})


class FakeBroker:
    environment='demo'
    account_id=APPROVED_ACCOUNT

    def __init__(self):
        self.positions=[]
        self.submitted=[]
        self.closed=[]

    def snapshot(self,epic):
        assert epic=='EURUSD'
        return snapshot(self.positions)

    def hourly_stop(self,snap,direction):
        return 1.0989 if direction=='BUY' else 1.1012

    def submit_entry(self,plan):
        self.submitted.append(dict(plan))
        return 'ref-'+str(len(self.submitted))

    def confirm_entry(self,ref):
        p=self.submitted[-1]
        deal='deal-'+str(len(self.submitted))
        self.positions.append(dict(position=dict(dealId=deal,size=p['size'],
            direction=p['direction'],level=1.1001 if p['direction']=='BUY' else 1.1,
            stopLevel=p['stop_level'],profitLevel=None,leverage=200,
            currency='USD',contractSize=1,upl=-.1),market=dict(epic='EURUSD')))
        return dict(status='confirmed',account_id=APPROVED_ACCOUNT,deal_id=deal,
                    epic='EURUSD',direction=p['direction'],size=p['size'],
                    stop_level=p['stop_level'],target_level=None,leverage=200,
                    actual_risk=p['planned_risk'],actual_margin=p['required_margin'],
                    existing_risk=p['existing_risk'],existing_margin=p['existing_margin'])

    def submit_close(self,deal,plan):
        self.closed.append(deal)
        return 'close-'+deal

    def confirm_close(self,ref,deal):
        self.positions=[p for p in self.positions if p['position']['dealId']!=deal]
        return dict(status='closed',account_id=APPROVED_ACCOUNT,deal_id=deal)


class HourlyHedgeTests(unittest.TestCase):
    def test_worker_closes_unprotected_hourly_position_at_target_and_hour_end(self):
        with patch('smarttrader.trial.ACCOUNT_SHA256',
                   hashlib.sha256(APPROVED_ACCOUNT.encode()).hexdigest()):
            for target,when,expected in ((.08,NOW,'hourly_profit_target'),
                                         (-2,HOUR+3600,'hourly_window_end')):
                with self.subTest(expected=expected),tempfile.TemporaryDirectory() as directory:
                    journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0,
                                            allow_technical_entry=True)
                    try:
                        broker=FakeBroker()
                        result=journal.process_hourly('EURUSD','BUY',broker,NOW,True)
                        self.assertEqual(result['status'],'confirmed')
                        broker.positions[0]['position']['upl']=target
                        broker.account_state=lambda:dict(account_id=APPROVED_ACCOUNT,
                            positions=broker.positions,working_orders=[],equity=1000,
                            daily=dict(stopped=False),observed_at=when)
                        broker.market_quote=(lambda _:dict(bid=1.1,ask=1.1001,quote_time=when))
                        worker=Worker.__new__(Worker)
                        worker.resources=lambda:(broker,journal)
                        worker.config=SimpleNamespace(trial=False,armed=True,mode='demo',strategy='hourly_hedge',
                            analysis_mode='rules',symbols=[],calls=0,entry_evidence='technical')
                        worker.clock=lambda:when
                        worker.gate=SimpleNamespace(stopped=lambda _:None)
                        worker.trend_exit_probe={}
                        events=[]
                        worker.emit=lambda event,**fields:events.append((event,fields))
                        worker._positions()
                        self.assertEqual(broker.closed,[result['deal_id']])
                        self.assertTrue(any(fields.get('reason')==expected for _,fields in events))
                    finally:
                        journal.close()

    def test_worker_closes_owned_legacy_trend_on_hourly_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0)
            try:
                old=dict(account_id=APPROVED_ACCOUNT,epic='EURUSD',direction='BUY',
                         size=100,stop_level=1.09,target_level=1.15,quote_currency='USD')
                with journal.db:
                    journal.db.execute('INSERT INTO smart_entries (signal,account,status,plan,deal) '
                                       'VALUES (?,?,?,?,?)',
                                       ('old',APPROVED_ACCOUNT,'confirmed',json.dumps(old),'old-deal'))
                broker=FakeBroker()
                broker.positions=[dict(position=dict(dealId='old-deal',direction='BUY',
                    size=100,stopLevel=1.09,level=1.10,currency='USD',contractSize=1),
                    market=dict(epic='EURUSD'))]
                broker.account_state=lambda:dict(account_id=APPROVED_ACCOUNT,
                    positions=broker.positions,working_orders=[],equity=1000,
                    daily=dict(stopped=False),observed_at=NOW)
                worker=Worker.__new__(Worker)
                worker.resources=lambda:(broker,journal)
                worker.config=SimpleNamespace(trial=False,armed=True,mode='demo',strategy='hourly_hedge',
                    analysis_mode='rules',symbols=[],calls=0,entry_evidence='technical')
                worker.clock=lambda:NOW
                worker.gate=SimpleNamespace(stopped=lambda _:None)
                worker.trend_exit_probe={}
                worker.emit=lambda *args,**kwargs:None
                worker._positions()
                self.assertEqual(broker.closed,['old-deal'])
            finally:
                journal.close()

    def test_hourly_sweep_attempts_both_sides_of_all_markets_once_per_hour(self):
        worker=Worker.__new__(Worker)
        current=[NOW]
        events=[]
        orders=[]
        worker.clock=lambda:current[0]
        worker.hourly_scan_hour=None
        worker.config=SimpleNamespace(symbols={'EURUSD':'EURUSD','GBPUSD':'GBPUSD',
                                                'GOLD':'GOLD'},risk_mode='fixed',armed=True)
        worker.gate=EntryGate({'positions':30,'opportunities':3600})
        worker.order_lock=threading.RLock()
        worker.trial_active=lambda:True
        worker.emit=lambda event,**fields:events.append((event,fields))
        worker.positions=lambda:orders.append(('close_due',current[0])) or True
        worker.resources=lambda:(object(),SimpleNamespace(process_hourly=lambda epic,direction,*args,**kwargs:
                            orders.append((epic,direction)) or {'status':'confirmed'}))
        worker.entry_state=lambda broker,journal:(dict(daily=dict(stopped=False,ready=True)),{})
        worker.capacity_reason=lambda epic,state,reconciled:None

        self.assertTrue(worker.hourly_opportunities())
        self.assertEqual(orders,[('close_due',NOW),('EURUSD','BUY'),('EURUSD','SELL'),
                                 ('GBPUSD','BUY'),('GBPUSD','SELL'),('GOLD','BUY'),('GOLD','SELL')])
        self.assertTrue(worker.hourly_opportunities())
        self.assertEqual(len(orders),7)
        current[0]+=3600
        self.assertTrue(worker.hourly_opportunities())
        self.assertEqual(orders[7],('close_due',NOW+3600))
        self.assertEqual(orders[8:],orders[1:7])
        self.assertEqual(len([event for event,_ in events if event=='hourly_sweep_finished']),2)
        current[0]+=3600
        worker.entry_state=lambda broker,journal:(dict(daily=dict(stopped=True,ready=False)),{})
        self.assertTrue(worker.hourly_opportunities())
        self.assertEqual(len(orders),15)  # due positions checked, but no new entries
        self.assertIn('daily_loss_limit',[fields.get('reason') for event,fields in events
                                           if event=='hourly_sweep_deferred'])

    def test_entry_uses_clock_after_broker_snapshot_for_quote_freshness(self):
        broker=FakeBroker()
        broker.clock=lambda: NOW+2
        old_snapshot=broker.snapshot
        def late_snapshot(epic):
            result=old_snapshot(epic)
            result['conversion_time']=NOW+1
            result['quote_time']=NOW+1
            return result
        broker.snapshot=late_snapshot
        with patch('smarttrader.trial.ACCOUNT_SHA256',
                   hashlib.sha256(APPROVED_ACCOUNT.encode()).hexdigest()):
            with tempfile.TemporaryDirectory() as directory:
                journal=DemoCoordinator(directory+'/journal.sqlite',APPROVED_ACCOUNT,
                                        max_positions=0,allow_technical_entry=True)
                try:
                    result=journal.process_hourly('EURUSD','BUY',broker,NOW,True)
                    self.assertEqual(result['status'],'confirmed')
                finally:
                    journal.close()

    def test_catalogue_selects_supported_fx_and_metals(self):
        markets=[dict(epic='EURJPY',symbol='EUR/JPY',instrumentType='CURRENCIES'),
                 dict(epic='GBPUSD',symbol='GBP/USD',instrumentType='CURRENCIES'),
                 dict(epic='NZDJPY',symbol='NZD/JPY',instrumentType='CURRENCIES'),
                 dict(epic='NZDMXN',symbol='NZD/MXN',instrumentType='CURRENCIES'),
                 dict(epic='PALLADIUM',instrumentName='Palladium',instrumentType='COMMODITIES'),
                 dict(epic='BTCUSD',symbol='BTC/USD',instrumentType='CRYPTOCURRENCIES')]
        self.assertEqual(market_epics(markets),['EURJPY','GBPUSD','NZDJPY','PALLADIUM'])

    def test_former_size_and_two_opposite_legs_share_sizing_allowance(self):
        first=plan_entry(snapshot(),{},'BUY',NOW,1.0989)
        self.assertEqual(first['strategy'],'hourly_hedge_v1')
        self.assertIsNone(first['stop_level'])
        self.assertEqual(first['risk_basis'],'hourly_reference_unbounded')
        self.assertLess(first['size'],1000)
        self.assertLessEqual(first['planned_risk'],.5)
        owned={'deal-1':dict(first,actual_risk=first['planned_risk'],
                             actual_margin=first['required_margin'])}
        held=dict(position=dict(dealId='deal-1',size=first['size'],direction='BUY',
                                level=1.1001,stopLevel=first['stop_level'],profitLevel=None,
                                leverage=200,currency='USD',contractSize=1),
                  market=dict(epic='EURUSD'))
        second=plan_entry(snapshot([held]),owned,'SELL',NOW,1.1012)
        self.assertEqual(second['hour_start'],first['hour_start'])
        self.assertEqual(second['direction'],'SELL')
        self.assertLess(second['combined_planned_risk'],2.5)
        with self.assertRaises(ValueError):
            plan_entry(snapshot([held]),owned,'BUY',NOW,1.0989)

    def test_former_size_does_not_round_up_to_broker_minimum(self):
        s=snapshot()
        s.update(min_size=1000,size_step=1000)
        with self.assertRaises(ValueError):
            plan_entry(s,{},'BUY',NOW,1.0989)

    def test_shared_margin_caps_former_volume_before_order(self):
        s=snapshot()
        s['margin_per_unit']=.5
        plan=plan_entry(s,{},'BUY',NOW,1.0989)
        self.assertLessEqual(plan['required_margin'],100)
        self.assertGreaterEqual(plan['size'],s['min_size'])

    def test_journal_confirms_both_once_then_profit_or_time_closes(self):
        with patch('smarttrader.trial.ACCOUNT_SHA256',
                   hashlib.sha256(APPROVED_ACCOUNT.encode()).hexdigest()):
            with tempfile.TemporaryDirectory() as directory:
                journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0,
                                        allow_technical_entry=True,allow_newsless_technical=True)
                broker=FakeBroker()
                try:
                    a=journal.process_hourly('EURUSD','BUY',broker,NOW,True)
                    b=journal.process_hourly('EURUSD','SELL',broker,NOW,True)
                    self.assertEqual([a['status'],b['status']],['confirmed','confirmed'])
                    self.assertEqual(len(broker.submitted),2)
                    self.assertEqual(journal.process_hourly('EURUSD','BUY',broker,NOW,True)['status'],
                                     'blocked')
                    self.assertEqual(len(broker.submitted),2)
                    self.assertIsNone(should_close(a['plan'],{'upl':.079},NOW))
                    self.assertEqual(should_close(a['plan'],{'upl':.08},NOW),
                                     'hourly_profit_target')
                    self.assertEqual(should_close(b['plan'],{'upl':-3},HOUR+3600),
                                     'hourly_window_end')
                    self.assertEqual(journal.close_position(a['deal_id'],'hourly_profit_target',
                                                            broker,True)['status'],'closed')
                    self.assertEqual(journal.close_position(b['deal_id'],'hourly_window_end',
                                                            broker,True)['status'],'closed')
                    self.assertEqual(broker.closed,[a['deal_id'],b['deal_id']])
                finally:
                    journal.close()

    def test_absent_confirmed_deal_resolves_uncertain_close_but_not_unknown_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0)
            try:
                with journal.db:
                    journal.db.execute('INSERT INTO smart_exits VALUES (?,?,?,?,?)',
                                       ('deal-gone',APPROVED_ACCOUNT,'uncertain','hourly_window_end','ref'))
                    journal.db.execute('INSERT INTO smart_entries VALUES (?,?,?,?,?,?)',
                                       ('signal',APPROVED_ACCOUNT,'uncertain','{}',None,'ref'))
                journal.reconcile(dict(account_id=APPROVED_ACCOUNT,positions=[],working_orders=[]))
                self.assertEqual(journal.review_summary(),{'entries_uncertain':1})
                self.assertTrue(journal.unresolved())
            finally:
                journal.close()

    def test_capital_adapter_confirms_both_legs_without_stop_or_take_profit(self):
        with patch('smarttrader.trial.ACCOUNT_SHA256',
                   hashlib.sha256(APPROVED_ACCOUNT.encode()).hexdigest()):
            with tempfile.TemporaryDirectory() as directory:
                journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0,
                                        allow_technical_entry=True,allow_newsless_technical=True)
                transport=PairTransport(lambda:NOW)
                transport.account=APPROVED_ACCOUNT
                transport.market['snapshot']['tickSize']=.0001
                broker=CapitalDemo(key='k',user='u',password='p',account_id=APPROVED_ACCOUNT,
                                   daily_controller=lambda equity,now:dict(stopped=False,remaining=10),
                                   armed=True,opener=transport,clock=lambda:NOW,sleep=lambda _:None)
                broker.configure_entries(0,journal.owned)
                broker.login()
                try:
                    a=journal.process_hourly('EURUSD','BUY',broker,NOW,True)
                    b=journal.process_hourly('EURUSD','SELL',broker,NOW,True)
                    self.assertEqual([a['status'],b['status']],['confirmed','confirmed'])
                    posts=[x for x in transport.calls if x[0]=='POST' and x[1].endswith('/positions')]
                    self.assertEqual(len(posts),2)
                    self.assertTrue(all(b'profitLevel' not in x[2] for x in posts))
                    self.assertTrue(all(b'stopLevel' not in x[2] for x in posts))
                    self.assertTrue(all(x['position']['stopLevel'] is None
                                        for x in transport.positions.values()))
                finally:
                    journal.close()

    def test_adapter_accepts_lower_actual_leverage_with_verified_margin(self):
        with patch('smarttrader.trial.ACCOUNT_SHA256',
                   hashlib.sha256(APPROVED_ACCOUNT.encode()).hexdigest()):
            with tempfile.TemporaryDirectory() as directory:
                journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0,
                                        allow_technical_entry=True,allow_newsless_technical=True)
                transport=PairTransport(lambda:NOW)
                transport.account=APPROVED_ACCOUNT
                transport.market_margin_factor=1
                transport.filled_leverage=100
                broker=CapitalDemo(key='k',user='u',password='p',account_id=APPROVED_ACCOUNT,
                                   daily_controller=lambda equity,now:dict(stopped=False,remaining=10),
                                   armed=True,opener=transport,clock=lambda:NOW,sleep=lambda _:None)
                broker.configure_entries(0,journal.owned)
                broker.login()
                try:
                    result=journal.process_hourly('EURUSD','BUY',broker,NOW,True)
                    self.assertEqual(result['status'],'confirmed')
                    self.assertEqual(result['plan']['leverage'],100)
                finally:
                    journal.close()

    def test_stale_uncertain_entry_releases_gate_only_after_two_clean_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0)
            try:
                with journal.db:
                    journal.db.execute('INSERT INTO smart_entries VALUES (?,?,?,?,?,?)',
                        ('old-signal',APPROVED_ACCOUNT,'uncertain',
                         json.dumps({'signal_candle':HOUR-7200}),None,None))
                state=dict(account_id=APPROVED_ACCOUNT,positions=[],working_orders=[],observed_at=NOW)
                self.assertTrue(journal.reconcile(state,NOW)['unresolved'])
                state['observed_at']=NOW+60
                self.assertTrue(journal.reconcile(state,NOW+60)['unresolved'])
                state['observed_at']=NOW+121
                self.assertEqual(journal.reconcile(state,NOW+121)['historical_review_released'],1)
                self.assertFalse(journal.unresolved())
                self.assertEqual(journal.db.execute('SELECT status FROM smart_entries').fetchone()[0],
                                 'inactive_unverified')
            finally:
                journal.close()

    def test_hourly_uncertain_never_retries_signal_after_clean_review(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0)
            try:
                with journal.db:
                    journal.db.execute('INSERT INTO smart_entries VALUES (?,?,?,?,?,?)',
                        ('hourly-signal',APPROVED_ACCOUNT,'uncertain',
                         json.dumps({'signal_candle':HOUR,'strategy':'hourly_hedge_v1'}),None,None))
                state=dict(account_id=APPROVED_ACCOUNT,positions=[],working_orders=[],observed_at=NOW)
                self.assertTrue(journal.reconcile(state,NOW)['unresolved'])
                state['observed_at']=NOW+121
                self.assertEqual(journal.reconcile(state,NOW+121)['historical_review_released'],1)
                self.assertFalse(journal.unresolved())
                self.assertEqual(journal.db.execute('SELECT signal,status FROM smart_entries').fetchone(),
                                 ('hourly-signal','inactive_unverified'))
            finally:
                journal.close()

    def test_exact_broker_reference_recovers_hourly_fill_with_lower_leverage(self):
        s=snapshot()
        s.update(margin_factor=.01,margin_per_unit=.011001)
        plan=plan_entry(s,{},'BUY',NOW,1.0989)
        held=dict(position=dict(dealId='deal-new',dealReference='ref-new',
                   direction='BUY',size=plan['size'],level=1.1001,
                   stopLevel=plan['stop_level'],profitLevel=None,leverage=100,
                   currency='USD',contractSize=1),market=dict(epic='EURUSD'))
        with tempfile.TemporaryDirectory() as directory:
            journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0)
            try:
                with journal.db:
                    journal.db.execute('INSERT INTO smart_entries VALUES (?,?,?,?,?,?)',
                        ('signal',APPROVED_ACCOUNT,'uncertain',json.dumps(plan),None,'ref-new'))
                state=dict(account_id=APPROVED_ACCOUNT,positions=[held],working_orders=[],observed_at=NOW)
                result=journal.reconcile(state,NOW)
                self.assertEqual(result['broker_confirmation_recovered'],1)
                self.assertEqual(result['unknown'],[])
                self.assertEqual(result['owned']['deal-new']['leverage'],100)
                self.assertFalse(result['unresolved'])
            finally:
                journal.close()

    def test_broker_confirmation_recovers_when_position_reference_differs(self):
        s=snapshot()
        s.update(margin_factor=.01,margin_per_unit=.011001)
        plan=plan_entry(s,{},'BUY',NOW,1.0989)
        held=dict(position=dict(dealId='deal-new',dealReference='position-ref',
                   direction='BUY',size=plan['size'],level=1.1001,
                   stopLevel=plan['stop_level'],profitLevel=None,leverage=100,
                   currency='USD',contractSize=1),market=dict(epic='EURUSD'))
        class ConfirmingBroker:
            def get(self,path):
                assert path=='/confirms/order-ref'
                return dict(dealStatus='ACCEPTED',affectedDeals=[dict(dealId='deal-new',status='OPENED')])
        with tempfile.TemporaryDirectory() as directory:
            journal=DemoCoordinator(directory+'/orders.sqlite',APPROVED_ACCOUNT,0)
            try:
                with journal.db:
                    journal.db.execute('INSERT INTO smart_entries VALUES (?,?,?,?,?,?)',
                        ('signal',APPROVED_ACCOUNT,'uncertain',json.dumps(plan),None,'order-ref'))
                state=dict(account_id=APPROVED_ACCOUNT,positions=[held],working_orders=[],observed_at=NOW)
                result=journal.reconcile(state,NOW,ConfirmingBroker())
                self.assertEqual(result['broker_confirmation_recovered'],1)
                self.assertEqual(result['unknown'],[])
            finally:
                journal.close()


if __name__=='__main__':
    unittest.main()
