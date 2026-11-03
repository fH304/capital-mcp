import copy
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from .ema50_daily import (Broker, Halt, Journal, Transport, Wait, Worker,
                          daily_signal, minimum_order)


class FakeBroker:
    account = '123'
    offset = 0

    def __init__(self):
        self.now = 410 * 86400 + 3600
        self.direction = 'BUY'
        self.positions = []
        self.orders = []
        self.writes = []
        self.confirms = {}
        self.fail_open = self.fail_close = self.delay_close = False

    def prices(self):
        day = int(self.now // 86400) * 86400
        return [dict(snapshotTimeUTC=datetime.fromtimestamp(day - (350-i)*86400,
                            timezone.utc).isoformat(), closePrice=dict(
                            bid=3651+i if self.direction == 'BUY' else 4349-i,
                            ask=3651.5+i if self.direction == 'BUY' else 4349.5-i))
                for i in range(350)]

    def market(self):
        return dict(instrument=dict(epic='GOLD', type='COMMODITIES', currency='USD',
                    lotSize=1, marginFactor=5, marginFactorUnit='PERCENTAGE'),
                    snapshot=dict(scalingFactor=1, marketStatus='TRADEABLE',
                    marketModes=['REGULAR'], delayTime=0, bid=4000, offer=4000.5,
                    updateTimeUTC=datetime.fromtimestamp(self.now, timezone.utc).isoformat()),
                    dealingRules={k:dict(unit='POINTS', value=v) for k,v in
                    (('minDealSize',.01),('minSizeIncrement',.01),('maxDealSize',100))})

    def read(self, path):
        if path == '/session':
            return dict(accountId=self.account)
        if path == '/accounts':
            return dict(accounts=[dict(accountId=self.account, accountType='CFD',
                        currency='USD', status='ENABLED', balance=dict(balance=1000, available=1000))])
        if path == '/positions':
            return dict(positions=copy.deepcopy(self.positions))
        if path == '/workingorders':
            return dict(workingOrders=self.orders)
        if path == '/markets/GOLD':
            return self.market()
        if path == '/prices/GOLD?resolution=DAY&max=500':
            return dict(prices=self.prices())
        if path.startswith('/confirms/'):
            return self.confirms[path.split('/')[-1]]
        if path.startswith('/positions/'):
            return copy.deepcopy(next(x for x in self.positions if x['position']['dealId'] == path.split('/')[-1]))
        raise AssertionError(path)

    def write(self, method, path, payload=None):
        self.writes.append((method,path,copy.deepcopy(payload)))
        ref = 'reference' + str(len(self.writes))
        if method == 'POST':
            deal = 'deal' + str(len(self.writes))
            self.positions.append(dict(position=dict(dealId=deal, currency='USD', contractSize=1,
                                      direction=payload['direction'], size=payload['size']),
                                      market=dict(epic='GOLD')))
            self.confirms[ref] = dict(dealStatus='ACCEPTED', affectedDeals=[dict(dealId=deal,status='OPENED')])
            if self.fail_open:
                raise Transport()
        else:
            deal = path.split('/')[-1]
            if self.fail_close:
                raise Transport()
            self.positions = [x for x in self.positions if x['position']['dealId'] != deal]
            self.confirms[ref] = (dict(dealStatus='PENDING', affectedDeals=[]) if self.delay_close else
                                 dict(dealStatus='ACCEPTED', affectedDeals=[dict(dealId=deal,status='CLOSED')]))
        return dict(dealReference=ref)


class DailyEMA50Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'state.sqlite')
        self.api = FakeBroker()
        self.journal = Journal(self.path, self.api.account, 'demo')
        self.logs = []
        self.worker = self.make_worker()

    def tearDown(self):
        self.journal.db.close()
        self.tmp.cleanup()

    def make_worker(self, mode='demo'):
        return Worker(self.api,self.journal,mode,clock=lambda:self.api.now,
                      sleep=lambda _:None,log=lambda event,**kw:self.logs.append((event,kw)))

    def test_incomplete_daily_bar_cannot_change_signal(self):
        rows = self.api.prices()
        original = daily_signal(rows,self.api.now)
        day = int(self.api.now//86400)*86400
        rows.append(dict(snapshotTimeUTC=datetime.fromtimestamp(day,timezone.utc).isoformat(),
                         closePrice=dict(bid=1,ask=1)))
        self.assertEqual(daily_signal(rows,self.api.now),original)
        self.assertEqual(original['direction'],'BUY')

    def test_minimum_order_has_no_stop_or_target(self):
        self.worker.cycle()
        order = self.api.writes[0][2]
        self.assertEqual(order,dict(epic='GOLD',direction='BUY',size=.01,guaranteedStop=False))
        self.assertIsNotNone(self.journal.state['position'])

    def test_same_direction_is_held_across_days(self):
        self.worker.cycle()
        self.api.now += 5*86400
        self.worker.cycle()
        self.assertEqual(len(self.api.writes),1)

    def test_confirmed_reversal_closes_before_opening_opposite(self):
        self.worker.cycle()
        self.api.now += 86400
        self.api.direction = 'SELL'
        self.worker.cycle()
        self.assertEqual([x[0] for x in self.api.writes],['POST','DELETE','POST'])
        self.assertEqual(self.journal.state['position']['direction'],'SELL')
        self.assertEqual(len(self.api.positions),1)

    def test_failed_close_cannot_open_opposite(self):
        self.worker.cycle()
        self.api.now += 86400
        self.api.direction,self.api.fail_close = 'SELL',True
        with self.assertRaises(Transport):
            self.worker.cycle()
        with self.assertRaises(Halt):
            self.make_worker().cycle()
        self.assertEqual([x[0] for x in self.api.writes],['POST','DELETE'])
        self.assertEqual(self.api.positions[0]['position']['direction'],'BUY')

    def test_unconfirmed_close_cannot_open_opposite(self):
        self.worker.cycle()
        self.api.now += 86400
        self.api.direction,self.api.delay_close = 'SELL',True
        with self.assertRaises(Wait):
            self.worker.cycle()
        self.assertEqual(len(self.api.writes),2)
        ref = self.journal.state['pending']['reference']
        self.api.confirms[ref] = dict(dealStatus='ACCEPTED',affectedDeals=[dict(dealId='deal1',status='CLOSED')])
        self.worker.cycle()
        self.assertEqual([x[0] for x in self.api.writes],['POST','DELETE','POST'])

    def test_lost_submission_response_is_never_resent(self):
        self.api.fail_open = True
        with self.assertRaises(Transport):
            self.worker.cycle()
        self.assertEqual(len(self.api.positions),1)
        with self.assertRaises(Halt):
            self.make_worker().cycle()
        self.assertEqual(len(self.api.writes),1)

    def test_persistent_ownership_prevents_restart_duplicate(self):
        self.worker.cycle()
        self.journal.db.close()
        self.journal = Journal(self.path,self.api.account,'demo')
        self.make_worker().cycle()
        self.assertEqual(len(self.api.writes),1)

    def test_foreign_position_halts_without_closing_it(self):
        self.api.positions = [dict(position=dict(dealId='manual',direction='SELL',size=1),market=dict(epic='GOLD'))]
        with self.assertRaises(Halt):
            self.worker.cycle()
        self.assertEqual(self.api.writes,[])

    def test_external_position_close_does_not_reopen_automatically(self):
        self.worker.cycle()
        self.api.positions = []
        with self.assertRaises(Halt):
            self.make_worker().cycle()
        self.assertEqual(len(self.api.writes),1)

    def test_working_orders_block_entry(self):
        self.api.orders = [dict(dealId='manual-limit')]
        with self.assertRaises(Halt):
            self.worker.cycle()
        self.assertFalse(self.api.writes)

    def test_preview_never_sends_orders(self):
        self.make_worker('preview').cycle()
        self.assertFalse(self.api.writes)
        self.assertIn('ema50_preview',[e for e,_ in self.logs])

    def test_stale_quote_blocks_entry(self):
        market = self.api.market()
        market['snapshot']['updateTimeUTC'] = datetime.fromtimestamp(self.api.now-121,timezone.utc).isoformat()
        with self.assertRaises(Wait):
            minimum_order(market,'BUY',1000,1000,self.api.now,0)

    def test_minimum_too_large_for_test_account_is_not_rounded_up_into_trade(self):
        with self.assertRaises(Wait):
            minimum_order(self.api.market(),'BUY',10,10,self.api.now,0)

    def test_live_environment_is_rejected(self):
        with self.assertRaises(Halt):
            Broker(dict(CAP_ENV='live'))

    def test_preview_transport_cannot_send_order(self):
        env = dict(CAP_ENV='demo',BOT_ACCOUNT_ID='123',SMART_MODE='preview',
                   CAP_API_KEY='fixture',CAP_IDENTIFIER='fixture',CAP_API_PASSWORD='fixture')
        api = Broker(env,sleep=lambda _:None)
        with self.assertRaises(Halt):
            api.write('POST','/positions',{})

    def test_scope_cannot_be_reused_for_another_account(self):
        with self.assertRaises(Halt):
            Journal(self.path,'456','demo')

    def test_opt_in_dispatch_does_not_construct_old_ai_worker(self):
        from . import runner
        with patch.dict(os.environ,{'SMART_GOLD_DAILY_EMA50':'1'}), \
                patch('smarttrader.ema50_daily.main') as daily_main, \
                patch.object(runner,'Config',side_effect=AssertionError('legacy configuration')):
            runner.main()
            daily_main.assert_called_once_with()

    def test_transport_uses_only_demo_host_and_minimum_order_payload(self):
        requests = []
        class Response(io.BytesIO):
            headers = {}
        def opener(request,timeout):
            requests.append(request)
            return Response(json.dumps({'dealReference':'confirmed-reference'}).encode())
        env = dict(CAP_ENV='demo',BOT_ACCOUNT_ID='123',SMART_MODE='demo',
                   SMART_ARMED='DEMO_ONLY',SMART_EXCLUSIVE_ACCOUNT='123',
                   CAP_API_KEY='fixture',CAP_IDENTIFIER='fixture',CAP_API_PASSWORD='fixture')
        api = Broker(env,opener=opener,sleep=lambda _:None)
        payload = dict(epic='GOLD',direction='SELL',size=.01,guaranteedStop=False)
        api.write('POST','/positions',payload)
        self.assertEqual(requests[0].full_url,'https://demo-api-capital.backend-capital.com/api/v1/positions')
        self.assertEqual(json.loads(requests[0].data),payload)
        self.assertEqual(len(requests),1)

    def test_transport_never_retries_rejected_mutation_or_changes_account_settings(self):
        requests = []
        def opener(request,timeout):
            requests.append(request)
            raise HTTPError(request.full_url,401,'Unauthorized',{},None)
        env = dict(CAP_ENV='demo',BOT_ACCOUNT_ID='123',SMART_MODE='demo',
                   SMART_ARMED='DEMO_ONLY',SMART_EXCLUSIVE_ACCOUNT='123',
                   CAP_API_KEY='fixture',CAP_IDENTIFIER='fixture',CAP_API_PASSWORD='fixture')
        api = Broker(env,opener=opener,sleep=lambda _:None)
        with self.assertRaises(Transport):
            api.write('POST','/positions',dict(epic='GOLD',direction='SELL',size=.01))
        self.assertEqual(len(requests),1)
        with self.assertRaises(Halt):
            api.write('PUT','/accounts/preferences',{})
        self.assertEqual(len(requests),1)


if __name__ == '__main__':
    unittest.main()
