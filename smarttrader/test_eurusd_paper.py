import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .ema50_daily import Broker, Halt, LoginGate, Wait
from .eurusd_paper import (PaperLedger, PaperWorker, eur_quote,
                           observer_loop)
from .test_ema50_daily import FakeBroker


class PaperBroker(FakeBroker):
    armed = False
    paper_market_data = True

    def __init__(self):
        super().__init__()
        self.equity = 1000
        self.gold_direction = None
        self.bid, self.ask = 1.10, 1.1001
        self.reads = []
        self.positions = [dict(position=dict(dealId='existing-gold', currency='USD',
            contractSize=1, direction='SELL', size=.01, level=4001), market=dict(epic='GOLD'))]

    def eur_market(self):
        d = self.market()
        d['instrument'].update(epic='EURUSD', type='CURRENCIES')
        d['snapshot'].update(bid=self.bid, offer=self.ask)
        d['dealingRules'] = {k:dict(unit='POINTS',value=v) for k,v in
            (('minDealSize',100),('minSizeIncrement',100),('maxDealSize',45000000))}
        return d

    def read(self, path):
        self.reads.append(path)
        if path == '/accounts':
            d = super().read(path)
            d['accounts'][0]['balance']['balance'] = self.equity
            return d
        if path == '/markets/EURUSD':
            return self.eur_market()
        if path == '/prices/EURUSD?resolution=DAY&max=500':
            rows = self.prices()
            for row in rows:
                row['closePrice'] = {side:value / 4000 for side,value in row['closePrice'].items()}
            return dict(prices=rows)
        if path == '/prices/GOLD?resolution=DAY&max=500' and self.gold_direction:
            direction = self.direction
            try:
                self.direction = self.gold_direction
                return dict(prices=self.prices())
            finally:
                self.direction = direction
        return super().read(path)

    def write(self, *args, **kwargs):
        raise AssertionError('Paper observer attempted a broker mutation')


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'paper.sqlite')
        self.api, self.logs = PaperBroker(), []
        self.ledger = PaperLedger(self.path,self.api.account)
        self.worker = self.make_worker()

    def tearDown(self):
        self.ledger.db.close()
        self.tmp.cleanup()

    def make_worker(self):
        return PaperWorker(self.api,self.ledger,clock=lambda:self.api.now,
                            log=lambda e,**kw:self.logs.append((e,kw)))

    def test_first_fill_includes_spread_and_never_touches_gold(self):
        gold = copy.deepcopy(self.api.positions)
        self.worker.cycle()
        p = self.ledger.state['position']
        self.assertEqual((p['direction'],p['size'],p['entry']),('BUY',100,1.1001))
        s = self.logs[-1][1]
        self.assertAlmostEqual(s['eur_unrealized_usd'],-.01)
        self.assertAlmostEqual(s['combined_virtual_equity_usd'],999.965)
        self.assertAlmostEqual(s['observed_account_plus_eur_equity_usd'],999.99)
        self.assertEqual(s['gold_paper_position']['size'],.05)
        self.assertEqual(s['gold_reference']['size'],.01)
        self.assertAlmostEqual(s['gold_paper_total_usd'],-.025)
        self.assertFalse(s['fits_analogous_gold_10pct_notional_cap'])
        self.assertEqual(self.api.positions,gold)
        self.assertEqual(s['gold_reference']['deal_id'],'existing-gold')
        self.assertTrue(s['funding_excluded'])

    def test_sell_fill_and_mark_use_bid_then_ask(self):
        self.api.direction = 'SELL'
        self.worker.cycle()
        p = self.ledger.state['position']
        self.assertEqual(p['entry'],self.api.bid)
        self.api.bid, self.api.ask = 1.09,1.0901
        self.worker.cycle()
        self.assertAlmostEqual(self.logs[-1][1]['eur_total_usd'],.99)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM trades').fetchone()[0],2)

    def test_holds_same_position_over_days_and_keeps_start_baseline(self):
        self.worker.cycle()
        opened = dict(self.ledger.state['position'])
        start = self.ledger.state['started_at']
        self.api.now += 3*86400
        self.api.equity = 990
        self.worker.cycle()
        self.assertEqual(self.ledger.state['position'],opened)
        self.assertEqual(self.ledger.state['started_at'],start)
        self.assertEqual(self.ledger.state['initial_broker_equity'],1000)
        self.assertAlmostEqual(self.logs[-1][1]['combined_change_since_start_usd'],-.035)
        self.assertAlmostEqual(self.logs[-1][1]['observed_account_plus_eur_equity_usd'],989.99)

    def test_new_daily_reversal_closes_then_opens_once_at_correct_quotes(self):
        self.worker.cycle()
        self.api.now += 86400
        self.api.direction = 'SELL'
        self.api.bid, self.api.ask = 1.12,1.1201
        self.worker.cycle()
        self.worker.cycle()
        self.assertAlmostEqual(self.ledger.state['realized'],1.99)
        p = self.ledger.state['position']
        self.assertEqual((p['direction'],p['entry']),('SELL',1.12))
        self.assertAlmostEqual(self.logs[-1][1]['eur_total_usd'],1.98)
        events = [json.loads(r[0])['event'] for r in self.ledger.db.execute('SELECT body FROM trades ORDER BY id')]
        self.assertEqual(events,['eurusd_paper_opened','gold_paper_opened',
            'eurusd_paper_closed','eurusd_paper_opened','gold_paper_closed','gold_paper_opened'])
        self.assertEqual(self.ledger.state['gold_position']['size'],.05)
        self.assertAlmostEqual(self.ledger.state['gold_realized'],-.025)

    def test_same_closed_day_cannot_reverse_from_revised_data(self):
        self.worker.cycle()
        self.api.now += 301
        self.api.direction = 'SELL'
        with self.assertRaises(Wait):
            self.worker.cycle()
        self.assertEqual(self.ledger.state['position']['direction'],'BUY')
        self.assertEqual(self.ledger.state['realized'],0)

    def test_restart_retains_fill_baseline_and_realized_profit(self):
        self.worker.cycle()
        self.api.now += 86400
        self.api.direction,self.api.bid,self.api.ask = 'SELL',1.12,1.1201
        self.worker.cycle()
        before = copy.deepcopy(self.ledger.state)
        self.ledger.db.close()
        self.ledger = PaperLedger(self.path,self.api.account)
        self.make_worker().cycle()
        self.assertEqual(self.ledger.state,before)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM trades').fetchone()[0],6)

    def test_failed_database_commit_cannot_partially_apply_reversal(self):
        self.worker.cycle()
        before = copy.deepcopy(self.ledger.state)
        self.api.now += 86400
        self.api.direction = 'SELL'
        self.ledger.db.execute("CREATE TRIGGER fail_fill BEFORE INSERT ON trades BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.worker.cycle()
        self.assertEqual(self.ledger.state,before)
        self.ledger.db.close()
        self.ledger = PaperLedger(self.path,self.api.account)
        self.assertEqual(self.ledger.state,before)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM trades').fetchone()[0],2)

    def test_gold_and_eur_signals_and_reversals_are_independent(self):
        self.api.direction,self.api.gold_direction = 'SELL','BUY'
        self.worker.cycle()
        eur = dict(self.ledger.state['position'])
        self.assertEqual(self.ledger.state['gold_position']['direction'],'BUY')
        self.api.now += 86400
        self.api.gold_direction = 'SELL'
        self.worker.cycle()
        self.assertEqual(self.ledger.state['position'],eur)
        self.assertEqual(self.ledger.state['gold_position']['direction'],'SELL')
        self.assertEqual(self.api.positions[0]['position']['size'],.01)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM trades').fetchone()[0],4)

    def test_invalid_gold_size_cannot_partially_open_eur(self):
        self.ledger.gold_size = .055
        with self.assertRaises(Halt):
            self.worker.cycle()
        self.assertIsNone(self.ledger.state['position'])
        self.assertIsNone(self.ledger.state['gold_position'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM trades').fetchone()[0],0)

    def test_stale_quote_does_not_create_virtual_fill(self):
        d = self.api.eur_market()
        with self.assertRaises(Wait):
            eur_quote(d,self.api.now+121,0)
        self.assertIsNone(self.ledger.state['position'])

    def test_transport_allows_only_opted_in_unarmed_eur_market_reads(self):
        requests = []
        class Response(io.BytesIO):
            headers = {}
        def opener(request, timeout):
            requests.append(request)
            return Response(b'{}')
        env = dict(CAP_ENV='demo',BOT_ACCOUNT_ID='123',SMART_MODE='preview',
                   CAP_API_KEY='fixture',CAP_IDENTIFIER='fixture',CAP_API_PASSWORD='fixture')
        api = Broker(env,opener=opener,sleep=lambda _:None,paper_market_data=True)
        api.request('GET','/markets/EURUSD')
        api.request('GET','/prices/EURUSD?resolution=DAY&max=500')
        for method,path in [('POST','/positions'),('DELETE','/positions/existing-gold'),
                            ('POST','/workingorders'),('PUT','/accounts/preferences')]:
            with self.assertRaises(Halt):
                api.write(method,path,{})
        self.assertEqual(len(requests),2)
        self.assertTrue(all(r.method == 'GET' for r in requests))
        self.assertTrue(all(r.full_url.startswith('https://demo-api-capital.backend-capital.com/') for r in requests))
        with self.assertRaises(Halt):
            Broker(dict(env,SMART_MODE='demo'),paper_market_data=True)
        with self.assertRaises(Halt):
            Broker(env,opener=opener).request('GET','/markets/EURUSD')

    def test_shared_login_gate_spaces_separate_clients(self):
        now, waits = [0.0], []
        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
        gate = LoginGate(clock=lambda:now[0],sleep=sleep)
        gate.wait()
        gate.wait()
        self.assertAlmostEqual(now[0],1.1)
        self.assertEqual(waits,[0,1.1])

    def test_observer_failure_is_caught_and_loop_can_retry(self):
        class Stop:
            def __init__(self): self.checks,self.waits = 0,[]
            def is_set(self):
                self.checks += 1
                return self.checks > 2
            def wait(self,seconds): self.waits.append(seconds); return False
        stop, logs = Stop(),[]
        env = dict(SMART_MODE='demo')
        with patch('smarttrader.eurusd_paper.Broker',side_effect=RuntimeError('private error text')) as broker:
            observer_loop(env,Path(self.tmp.name),stop,None,log=lambda e,**kw:logs.append((e,kw)))
        self.assertEqual(broker.call_count,2)
        self.assertEqual(broker.call_args.args[0]['SMART_MODE'],'preview')
        self.assertEqual(stop.waits,[3,60,60])
        self.assertEqual(len(logs),2)
        self.assertTrue(all(fields['reason'] == 'invalid_paper_runtime_data' for _,fields in logs))


if __name__ == '__main__':
    unittest.main()
