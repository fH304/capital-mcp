import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .ema50_daily import Broker, Halt, Journal, Worker
from .metals_daily import Metals
from .test_ema50_daily import FakeBroker


class MetalsBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.silver_direction = 'SELL'
        self.silver_closed = self.delay_silver_open = False

    def read(self, path):
        if path == '/markets/SILVER':
            d = self.market()
            d['instrument'].update(epic='SILVER',marginFactor=10)
            d['snapshot'].update(bid=52,offer=52.05)
            if self.silver_closed:
                d['snapshot']['marketStatus'] = 'CLOSED'
            d['dealingRules'] = {k:dict(unit='POINTS',value=v) for k,v in
                (('minDealSize',1),('minSizeIncrement',.1),('maxDealSize',100000))}
            return d
        if path == '/prices/SILVER?resolution=DAY&max=500':
            direction = self.direction
            try:
                self.direction = self.silver_direction
                rows = self.prices()
            finally:
                self.direction = direction
            for row in rows:
                row['closePrice'] = {side:value/80 for side,value in row['closePrice'].items()}
            return dict(prices=rows)
        return super().read(path)

    def write(self, method, path, payload=None):
        try:
            result = super().write(method,path,payload)
        finally:
            if method == 'POST':
                self.positions[-1]['market']['epic'] = payload['epic']
        if method == 'POST' and payload['epic'] == 'SILVER' and self.delay_silver_open:
            self.confirms[result['dealReference']]['dealStatus'] = 'PENDING'
        return result


class MetalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.logs = MetalsBroker(),[]
        self.paths = {epic:str(Path(self.tmp.name)/(epic + '.sqlite')) for epic in ('GOLD','SILVER')}
        self.gold_journal = Journal(self.paths['GOLD'],self.api.account,'demo')
        self.gold = self.worker('GOLD',self.gold_journal)
        self.gold.cycle()  # The existing single-GOLD worker owns this exact deal.
        self.existing_gold = copy.deepcopy(self.api.positions[0])
        self.silver_journal = Journal(self.paths['SILVER'],self.api.account,'demo','SILVER')
        self.silver = self.worker('SILVER',self.silver_journal)
        self.portfolio = Metals(self.api,[self.gold,self.silver],log=self.log)

    def log(self,event,**fields): self.logs.append((event,fields))

    def worker(self,epic,journal):
        return Worker(self.api,journal,'demo',epic=epic,requested_size=2 if epic == 'SILVER' else None,
                      clock=lambda:self.api.now,sleep=lambda _:None,log=self.log)

    def reopen(self):
        self.gold_journal.db.close()
        self.silver_journal.db.close()
        self.gold_journal = Journal(self.paths['GOLD'],self.api.account,'demo')
        self.silver_journal = Journal(self.paths['SILVER'],self.api.account,'demo','SILVER')
        self.gold,self.silver = self.worker('GOLD',self.gold_journal),self.worker('SILVER',self.silver_journal)
        self.portfolio = Metals(self.api,[self.gold,self.silver],log=self.log)

    def tearDown(self):
        self.gold_journal.db.close()
        self.silver_journal.db.close()
        self.tmp.cleanup()

    def test_adds_two_ounces_silver_without_touching_existing_gold(self):
        self.portfolio.cycle()
        self.assertEqual(len(self.api.writes),2)
        self.assertEqual(self.api.writes[-1][2],dict(epic='SILVER',direction='SELL',size=2.0,guaranteedStop=False))
        self.assertEqual(self.api.positions[0],self.existing_gold)
        self.assertEqual(self.gold_journal.state['position']['size'],.01)
        self.assertEqual(self.silver_journal.state['position']['size'],2)
        self.assertEqual({p['market']['epic'] for p in self.api.positions},{'GOLD','SILVER'})

    def test_both_positions_hold_across_days_and_restart_without_duplicate(self):
        self.portfolio.cycle()
        self.api.now += 3*86400
        self.reopen()
        self.portfolio.cycle()
        self.assertEqual(len(self.api.writes),2)
        self.assertEqual(len(self.api.positions),2)

    def test_silver_reverses_independently_after_new_daily_close(self):
        self.portfolio.cycle()
        self.api.now += 86400
        self.api.silver_direction = 'BUY'
        self.portfolio.cycle()
        self.assertEqual([w[0] for w in self.api.writes],['POST','POST','DELETE','POST'])
        self.assertEqual(self.api.writes[-1][2]['epic'],'SILVER')
        self.assertEqual(self.api.writes[-1][2]['size'],2)
        self.assertEqual(self.api.positions[0],self.existing_gold)
        self.assertEqual(self.silver_journal.state['position']['direction'],'BUY')

    def test_restart_recovers_pending_silver_before_gold_exposure_validation(self):
        self.api.delay_silver_open = True
        self.portfolio.cycle()
        pending = self.silver_journal.state['pending']
        self.assertIsNotNone(pending)
        self.api.confirms[pending['reference']]['dealStatus'] = 'ACCEPTED'
        self.reopen()
        self.portfolio.cycle()
        self.assertEqual(len(self.api.writes),2)
        self.assertIsNone(self.silver_journal.state['pending'])
        self.assertEqual(self.api.positions[0],self.existing_gold)
        self.assertIsNone(self.gold_journal.state['halt'])

    def test_uncertain_silver_submission_halts_without_retrying_or_closing_gold(self):
        self.api.fail_open = True
        with self.assertRaises(Halt):
            self.portfolio.cycle()
        self.reopen()
        with self.assertRaises(Halt):
            self.portfolio.cycle()
        self.assertEqual(len(self.api.writes),2)
        self.assertTrue(self.gold_journal.state['halt'])
        self.assertTrue(self.silver_journal.state['halt'])
        self.assertEqual(self.api.positions[0],self.existing_gold)

    def test_foreign_forex_position_is_never_adopted_or_closed(self):
        manual = dict(position=dict(dealId='manual-eur',size=100,direction='BUY'),market=dict(epic='EURUSD'))
        self.api.positions.append(manual)
        with self.assertRaises(Halt):
            self.portfolio.cycle()
        self.assertEqual(len(self.api.writes),1)
        self.assertIn(manual,self.api.positions)

    def test_closed_silver_market_does_not_prevent_gold_daily_exit(self):
        self.api.silver_closed = True
        self.api.now += 86400
        self.api.direction = 'SELL'
        self.portfolio.cycle()
        self.assertEqual([w[0] for w in self.api.writes],['POST','DELETE','POST'])
        self.assertTrue(all(w[2] is None or w[2]['epic'] == 'GOLD' for w in self.api.writes))
        self.assertEqual(self.gold_journal.state['position']['direction'],'SELL')

    def test_silver_below_broker_minimum_cannot_send_any_order(self):
        self.silver.requested_size = .05
        with self.assertRaises(Halt):
            self.portfolio.cycle()
        self.assertEqual(len(self.api.writes),1)
        self.assertEqual(self.api.positions,[self.existing_gold])

    def test_metals_dispatch_precedes_gold_and_old_ai_configuration(self):
        from . import runner
        with patch.dict(os.environ,{'SMART_METALS_DAILY_EMA50':'1','SMART_GOLD_DAILY_EMA50':'1'}), \
                patch('smarttrader.metals_daily.main') as metals_main, \
                patch('smarttrader.ema50_daily.main',side_effect=AssertionError('old gold dispatch')), \
                patch.object(runner,'Config',side_effect=AssertionError('old ai config')):
            runner.main()
            metals_main.assert_called_once_with()

    def test_transport_opt_in_is_limited_to_gold_and_silver(self):
        requests = []
        class Response(io.BytesIO): headers = {}
        def opener(request,timeout): requests.append(request); return Response(b'{}')
        env = dict(CAP_ENV='demo',BOT_ACCOUNT_ID='123',SMART_MODE='demo',
                   SMART_ARMED='DEMO_ONLY',SMART_EXCLUSIVE_ACCOUNT='123',
                   CAP_API_KEY='fixture',CAP_IDENTIFIER='fixture',CAP_API_PASSWORD='fixture')
        api = Broker(env,opener=opener,sleep=lambda _:None,market_epics=('SILVER',))
        api.request('GET','/markets/SILVER')
        api.request('GET','/prices/SILVER?resolution=DAY&max=500')
        api.write('POST','/positions',dict(epic='SILVER',direction='SELL',size=2,guaranteedStop=False))
        with self.assertRaises(Halt):
            api.write('POST','/positions',dict(epic='EURUSD',direction='SELL',size=5000))
        with self.assertRaises(Halt):
            api.request('GET','/markets/GBPUSD')
        with self.assertRaises(Halt):
            Broker(env,market_epics=('EURUSD',))
        self.assertEqual(len(requests),3)
        self.assertTrue(all(r.full_url.startswith('https://demo-api-capital.backend-capital.com/') for r in requests))


if __name__ == '__main__':
    unittest.main()
