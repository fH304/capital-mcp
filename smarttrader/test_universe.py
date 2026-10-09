import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from .capital import CapitalDemo, CapitalError
from .daily import AdaptiveDailyRisk
from .news import NewsClient
from .runner import Config, Worker
from .universe import MARKETS, relevant_articles, rank
from .test_analysis import NOW, context
from .test_capital import Response
from .test_marketdata import MarketCollector
from .test_runner import LiveStateTransport


def article(title, symbols=()):
    return dict(context()['articles'][0], title=title, content='Evidence', symbols=list(symbols))


def trending(epic='EURUSD', strength=.001):
    from .test_marketdata import rows
    from .candles import RESOLUTIONS, normalize_candles
    frames={}
    for resolution in RESOLUTIONS:
        values=rows(resolution)
        for i,b in enumerate(values):
            c=1+i*strength
            for name,value in (('openPrice',c),('highPrice',c+.002),('lowPrice',c-.002),('closePrice',c)):
                b[name]=dict(bid=value,ask=value+.0001)
        frames[resolution]=normalize_candles(values,resolution,NOW)
    return dict(epic=epic,bid=1.04,ask=1.0401,quote_time=NOW,collected_at=NOW,
                execution_supported=True,quote_currency='USD',timeframes=frames)


class UniverseTests(unittest.TestCase):
    def test_routing_is_specific_and_labels_topic_evidence(self):
        news=[article('Bitcoin price rises'),article('Goldman Sachs earnings'),
              article('Spot gold falls'),article('Gold prices rise'),article('Unrelated', ['EURUSD.FOREX'])]
        self.assertEqual(len(relevant_articles('BTCUSD',news,NOW)),1)
        gold=relevant_articles('GOLD',news,NOW)
        self.assertEqual(len(gold),2)
        self.assertTrue(all(a['relevance']=='headline_topic' for a in gold))
        self.assertEqual(relevant_articles('EURUSD',news,NOW)[0]['relevance'],'symbol_tag')
        self.assertFalse(relevant_articles('GOLD',news,NOW+21601))
    def test_rank_penalizes_spread_and_disagreeing_timeframes(self):
        ctx=trending()
        self.assertGreater(rank(ctx)['score'],0)
        wide=copy.deepcopy(ctx)
        wide['ask']=wide['bid']+.02
        self.assertEqual(rank(wide)['score'],0)
        divergent=copy.deepcopy(ctx)
        divergent['timeframes']['HOUR']['candles'][-1]['c']=.9
        self.assertEqual(rank(divergent)['score'],0)
    def test_general_news_shares_one_budget_reservation(self):
        requests=[]
        def opener(request,timeout):
            requests.append(request)
            a=article('Spot gold rises')
            return Response([dict(date=a['published_utc'],title=a['title'],link='https://example.com/gold')])
        with tempfile.TemporaryDirectory() as folder:
            client=NewsClient('private',folder+'/news.sqlite',opener=opener)
            try:
                self.assertEqual(client.fetch('__GENERAL__',NOW)['status'],'fetched')
                self.assertEqual(client.fetch('__GENERAL__',NOW+60)['status'],'cached')
                self.assertEqual(len(requests),1)
                query=parse_qs(urlsplit(requests[0].full_url).query)
                self.assertNotIn('s',query)
                self.assertEqual(query['limit'],['100'])
                self.assertEqual(client.db.execute('SELECT used FROM budget').fetchone()[0],5)
            finally:
                client.db.close()
    def test_non_usd_without_verified_fx_is_observable_but_execution_is_refused(self):
        transport=LiveStateTransport()
        transport.market['instrument']['currency']='JPY'
        broker=CapitalDemo(key='private',user='private',password='private',account_id='demo-1',
                           daily_controller=lambda e,t:dict(stopped=False,remaining=10),
                           opener=transport,clock=lambda:NOW,sleep=lambda _:None)
        broker.login()
        data=MarketCollector(broker,lambda:NOW).collect_market('EURUSD')
        self.assertFalse(data['execution_supported'])
        with self.assertRaises(CapitalError):
            broker.snapshot('EURUSD')
        self.assertFalse(any(method=='POST' and url.endswith('/positions') for method,url,_ in transport.calls))
    def test_market_expansion_preserves_daily_baseline_ceiling_and_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/risk.sqlite'
            old=AdaptiveDailyRisk(path,'demo-1',['EURUSD'])
            old(1000,NOW)
            self.assertTrue(old(980,NOW+1)['stopped'])
            old.close()
            broad=AdaptiveDailyRisk(path,'demo-1',MARKETS,selected_market_only=True)
            try:
                broad.activate('GOLD')
                result=broad(1100,NOW+2)
                self.assertEqual(result['baseline'],1000)
                self.assertLessEqual(result['limit'],10)
                self.assertTrue(result['stopped'])
                self.assertFalse(result['ready'])
                broad.update_market('GOLD',trending()['timeframes']['MINUTE_15'],NOW+2)
                self.assertTrue(broad(1100,NOW+2)['ready'])
            finally:
                broad.close()


class BroadWorkerTests(unittest.TestCase):
    @patch('time.time',return_value=NOW)
    def test_complete_sweep_ranks_fresh_eligible_markets_before_one_ai_call(self,_):
        with tempfile.TemporaryDirectory() as folder:
            env=dict(BOT_ACCOUNT_ID='demo-1',CAP_API_KEY='private',CAP_IDENTIFIER='private',CAP_API_PASSWORD='private',
                     OPENAI_API_KEY='private',OPENAI_MODEL='fixture',EODHD_API_KEY='private',SMART_MARKETS='broad',
                     SMART_ANALYSIS_MODE='budgeted',
                     SMART_STATE_DIR=folder+'/state',BOT_STATE_PATH=folder+'/old.sqlite')
            cfg=Config(env)
            class MultiTransport(LiveStateTransport):
                def __call__(self,request,timeout):
                    if '/markets/' in request.full_url:
                        self.market['instrument']['epic']=request.full_url.rsplit('/',1)[1]
                    return super().__call__(request,timeout)
            transport=MultiTransport()
            def broker(daily):
                return CapitalDemo(key='private',user='private',password='private',account_id='demo-1',
                                   daily_controller=daily,opener=transport,clock=lambda:NOW,sleep=lambda _:None)
            requests=[]
            class News:
                def fetch(self,symbol):
                    requests.append(symbol)
                    return dict(status='cached',articles=[article('Euro', ['EURUSD.FOREX']),
                                                         article('Spot gold rises'),article('Bitcoin rally')])
            ai_calls=[]
            class AI:
                def analyze(self,ctx):
                    ai_calls.append(ctx)
                    return dict(action='WAIT',assessment='reject',stop_level=0,target_level=0,
                                reason='Insufficient evidence for entry',article_ids=[])
            events=[]
            worker=Worker(cfg,broker,AI,News,clock=lambda:NOW,emit=lambda event,**fields:events.append((event,fields)))
            def collect(collector,epic):
                if epic=='US30':
                    raise CapitalError('Closed market')
                ctx=trending(epic,.002 if epic=='GOLD' else .0005)
                if epic=='BTCUSD':
                    ctx['execution_supported']=False
                return ctx
            try:
                worker.positions()
                worker.gate.success('positions',NOW)
                self.assertTrue(worker.news())
                worker.gate.success('news',NOW)
                with patch.object(MarketCollector,'collect_market',collect):
                    for _ in range(len(MARKETS)-1):
                        self.assertTrue(worker.opportunities())
                    self.assertFalse(ai_calls)
                    worker.opportunities()
                self.assertEqual(requests,['__GENERAL__'])
                self.assertEqual(len(ai_calls),1)
                self.assertEqual(ai_calls[0]['epic'],'GOLD')
                ranking=[data for event,data in events if event=='universe_ranked'][0]
                self.assertEqual(ranking['eligible'],2)
                self.assertNotIn('BTCUSD',[c['epic'] for c in ranking['top']])
                self.assertIn('market_unavailable',[event for event,_ in events])
                self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in transport.calls))
            finally:
                for name in ('daily','journal','scans'):
                    obj=getattr(worker.local,name,None)
                    if obj:
                        obj.close()
