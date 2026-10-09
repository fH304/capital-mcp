import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError

from .capital import BASE, CapitalDemo, CapitalError
from .coordinator import DemoCoordinator, CoordinationError
from .test_analysis import NOW, context, recommendation


class Response:
    headers = {'CST':'session-token', 'X-SECURITY-TOKEN':'account-token'}
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, size):
        return json.dumps(self.data).encode()


def market():
    rules = {name:dict(unit='POINTS',value=value) for name,value in (
        ('minDealSize',1),('minSizeIncrement',1),('maxDealSize',100000),
        ('minStopOrProfitDistance',.001),('maxStopOrProfitDistance',1))}
    return dict(instrument=dict(epic='EURUSD',type='CURRENCIES',currency='USD',lotSize=1,
                               marginFactorUnit='PERCENTAGE',marginFactor=.5), dealingRules=rules,
                snapshot=dict(bid=1.10,offer=1.101,scalingFactor=1,marketStatus='TRADEABLE',
                              marketModes=['REGULAR'],delayTime=0,
                              updateTimeUTC=datetime.fromtimestamp(NOW,timezone.utc).isoformat()))


class Transport:
    def __init__(self):
        self.calls = []
        self.account = 'demo-1'
        self.market = market()
        self.position, self.fail_post, self.stop_missing, self.fill = None, False, False, 1.101
        self.multiple = False
    def __call__(self, request, timeout):
        path = request.full_url.removeprefix(BASE)
        method = request.get_method()
        self.calls.append((method,request.full_url,request.data))
        if path=='/session':
            data = dict(currentAccountId=self.account,accountId=self.account,timezoneOffset=4)
        elif path=='/accounts':
            data = dict(accounts=[dict(accountId=self.account,currency='USD',status='ENABLED',accountType='CFD',
                                       balance=dict(balance=1000,available=1000,profitLoss=10))])
        elif path=='/workingorders':
            data = {'workingOrders':[]}
        elif path=='/positions' and method=='GET':
            data = {'positions':[]}
        elif path.startswith('/markets/'):
            data = self.market
        elif path=='/positions' and method=='POST':
            if self.fail_post:
                raise HTTPError(request.full_url,401,'secret',{},None)
            self.position = json.loads(request.data)
            data = {'dealReference':'o_1'}
        elif path=='/confirms/o_1':
            deals = [dict(status='OPENED',dealId='deal-1')]
            data = dict(dealStatus='ACCEPTED',affectedDeals=deals* (2 if self.multiple else 1))
        elif path=='/positions/deal-1':
            p = self.position
            data = dict(position=dict(dealId='deal-1',currency='USD',contractSize=1,
                                      direction=p['direction'],size=p['size'],level=self.fill,
                                      stopLevel=None if self.stop_missing else p['stopLevel'],profitLevel=p['profitLevel']),
                        market=dict(epic=p['epic']))
        else:
            raise AssertionError('Unexpected request '+path)
        return Response(copy.deepcopy(data))


class CapitalTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.adapter = self.make()
        self.adapter.login()
        self.tmp = tempfile.TemporaryDirectory()
        self.coordinator = DemoCoordinator(self.tmp.name+'/state.sqlite','demo-1')
    def tearDown(self):
        self.coordinator.close()
        self.tmp.cleanup()
    def make(self, **changes):
        args = dict(key='private-key',user='private-user',password='private-password',account_id='demo-1',
                    daily_controller=lambda equity, now:dict(stopped=False,remaining=20),
                    armed=True,opener=self.transport,clock=lambda:NOW,sleep=lambda delay:None)
        args.update(changes)
        return CapitalDemo(**args)
    def process(self, **changes):
        return self.coordinator.process('candle-1',recommendation(),context(),self.adapter,now=NOW,armed=True,**changes)
    def test_authenticated_demo_order_and_actual_confirmation(self):
        result = self.process()
        self.assertEqual(result['status'],'confirmed')
        posts = [x for x in self.transport.calls if x[0]=='POST' and x[1].endswith('/positions')]
        self.assertEqual(len(posts),1)
        payload = json.loads(posts[0][2])
        self.assertEqual(set(payload),{'epic','direction','size','stopLevel','profitLevel','guaranteedStop'})
        self.assertEqual(payload['stopLevel'],1.09)
        self.assertTrue(all(x[1].startswith(BASE) for x in self.transport.calls))
    def test_equity_does_not_double_count_floating_profit(self):
        self.assertEqual(self.adapter.snapshot('EURUSD')['equity'],1000)
    def test_live_constructor_and_wrong_login_account_refused(self):
        with self.assertRaises(CapitalError):
            self.make(environment='live')
        self.transport.account='other'
        with self.assertRaises(CapitalError):
            self.adapter.login()
        self.assertIsNone(self.adapter.account_id)
    def test_session_switch_blocks_before_orders(self):
        self.transport.account='other'
        with self.assertRaises(CapitalError):
            self.process()
        self.assertFalse(any(x[1].endswith('/positions') and x[0]=='POST' for x in self.transport.calls))
    def test_post_401_is_not_retried_or_reauthenticated(self):
        self.transport.fail_post=True
        with self.assertRaises(CoordinationError) as error:
            self.process()
        self.assertNotIn('private',str(error.exception))
        self.assertNotIn('secret',str(error.exception))
        self.assertEqual(sum(m=='POST' and u.endswith('/positions') for m,u,_ in self.transport.calls),1)
        self.assertEqual(sum(m=='POST' and u.endswith('/session') for m,u,_ in self.transport.calls),1)
    def test_missing_stop_multiple_deals_or_slippage_halt(self):
        for attr,value in (('stop_missing',True),('multiple',True),('fill',1.12)):
            with self.subTest(attr=attr):
                t = Transport()
                setattr(t,attr,value)
                a = self.make(opener=t)
                a.login()
                with tempfile.TemporaryDirectory() as folder:
                    c = DemoCoordinator(folder+'/state.sqlite','demo-1')
                    try:
                        with self.assertRaises(CoordinationError):
                            c.process('candle-1',recommendation(),context(),a,now=NOW,armed=True)
                        self.assertEqual(c.db.execute('SELECT status FROM smart_entries').fetchone()[0],'uncertain')
                    finally:
                        c.close()
    def test_unknown_currency_and_nonunit_scaling_rejected(self):
        for section,key,value in (('instrument','currency','XYZ'),('instrument','lotSize',100),
                                   ('snapshot','scalingFactor',100),('instrument','type','SHARES')):
            self.transport.market=market()
            self.transport.market[section][key]=value
            with self.assertRaises(CapitalError):
                self.adapter.snapshot('EURUSD')
    def test_local_timestamp_uses_authenticated_offset(self):
        s=self.transport.market['snapshot']
        del s['updateTimeUTC']
        s['updateTime']=datetime.fromtimestamp(NOW+4*3600,timezone.utc).replace(tzinfo=None).isoformat()
        self.assertEqual(self.adapter.snapshot('EURUSD')['quote_time'],NOW)
    def test_unarmed_adapter_cannot_post(self):
        self.adapter.armed=False
        with self.assertRaises(CoordinationError):
            self.process()
        self.assertIsNone(self.transport.position)
    def test_no_account_switch_leverage_or_topup_endpoint(self):
        for method,path in (('PUT','/session'),('PUT','/accounts/preferences'),('POST','/accounts/topUp'),
                            ('DELETE','/positions/deal-1')):
            with self.assertRaises(CapitalError):
                self.adapter._request(method,path,{})
