import copy
import json
import unittest
from datetime import datetime,timezone
from unittest.mock import patch

from .analysis import AnalysisError,validate_context
from .capital import CapitalDemo,CapitalError
from .candles import RESOLUTIONS,MarketDataError,normalize_candles
from .marketdata import MarketCollector
from .test_analysis import NOW,context,recommendation
from .test_capital import Transport,Response


def rows(resolution):
    duration=RESOLUTIONS[resolution]
    current=NOW//duration*duration
    result=[]
    for i in range(39,-1,-1):
        result.append(dict(snapshotTimeUTC=datetime.fromtimestamp(current-i*duration,timezone.utc).isoformat(),
                           openPrice=dict(bid=1.10,ask=1.101),highPrice=dict(bid=1.12,ask=1.121),
                           lowPrice=dict(bid=1.09,ask=1.091),closePrice=dict(bid=1.11,ask=1.111)))
    return result


class PriceTransport(Transport):
    def __call__(self,request,timeout):
        if '/prices/' in request.full_url:
            self.calls.append((request.get_method(),request.full_url,request.data))
            resolution=request.full_url.split('resolution=')[1].split('&')[0]
            return Response(dict(prices=rows(resolution)))
        return super().__call__(request,timeout)


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.transport=PriceTransport()
        self.broker=CapitalDemo(key='private-key',user='private-user',password='private-password',
                                account_id='demo-1',daily_controller=lambda equity,now:dict(stopped=False,remaining=20),
                                opener=self.transport,clock=lambda:NOW,sleep=lambda delay:None)
        self.broker.login()
        self.collector=MarketCollector(self.broker,clock=lambda:NOW)
    def collect(self):
        return self.collector.collect('EURUSD',context()['articles'])
    def test_all_timeframes_and_fresh_quote_reach_analysis(self):
        data=self.collect()
        self.assertEqual(set(data['timeframes']),set(RESOLUTIONS))
        for resolution,frame in data['timeframes'].items():
            self.assertEqual(len(frame['candles']),32)
            self.assertTrue(all(c['t']+RESOLUTIONS[resolution]<=NOW for c in frame['candles']))
            self.assertAlmostEqual(frame['candles'][-1]['o'],1.1005)
            self.assertNotIn('broker_volume',frame['candles'][-1])
        self.assertLess(len(json.dumps(data).encode()),30000)
        market_calls=[i for i,c in enumerate(self.transport.calls) if '/markets/' in c[1]]
        price_calls=[i for i,c in enumerate(self.transport.calls) if '/prices/' in c[1]]
        self.assertLess(market_calls[0],min(price_calls))
        self.assertGreater(market_calls[-1],max(price_calls))
        self.assertFalse(any(method!='GET' for method,_,_ in self.transport.calls[1:]))
    def test_link_to_analysis_client(self):
        class Client:
            def analyze(client,data):
                self.assertIn('HOUR_4',data['timeframes'])
                return recommendation()
        data,result=self.collector.analyze('EURUSD',context()['articles'],Client())
        self.assertEqual(result['action'],'BUY')
    def test_recent_gaps_duplicates_and_future_bars_rejected(self):
        for kind in ('duplicate','gap','future'):
            data=rows('MINUTE_15')
            if kind=='duplicate':
                data[-2]=copy.deepcopy(data[-3])
            elif kind=='gap':
                del data[-3]
            else:
                data[-1]['snapshotTimeUTC']=datetime.fromtimestamp(NOW+900,timezone.utc).isoformat()
            with self.assertRaises(MarketDataError):
                normalize_candles(data,'MINUTE_15',NOW)
    def test_bad_ohlc_spread_nan_and_missing_utc_rejected(self):
        for kind in ('range','spread','nan','local'):
            data=rows('MINUTE_15')
            if kind=='range':
                data[-2]['highPrice']=dict(bid=1.01,ask=1.02)
            elif kind=='spread':
                data[-2]['openPrice']=dict(bid=1.10,ask=1.09)
            elif kind=='nan':
                data[-2]['closePrice']['bid']=float('nan')
            else:
                data[-2]['snapshotTime']=data[-2].pop('snapshotTimeUTC')
            with self.assertRaises(MarketDataError):
                normalize_candles(data,'MINUTE_15',NOW)
    def test_stale_or_short_histories_rejected(self):
        data=rows('MINUTE_15')
        with self.assertRaises(MarketDataError):
            normalize_candles(data[:20],'MINUTE_15',NOW)
        with self.assertRaises(MarketDataError):
            normalize_candles(data[-10:],'MINUTE_15',NOW)
    def test_optional_volume_is_preserved_without_fabrication(self):
        data=rows('MINUTE_15')
        data[-2]['lastTradedVolume']=0
        frame=normalize_candles(data,'MINUTE_15',NOW)
        self.assertEqual(frame['candles'][-1]['broker_volume'],0)
        data[-2]['lastTradedVolume']=-1
        with self.assertRaises(MarketDataError):
            normalize_candles(data,'MINUTE_15',NOW)
    def test_later_analysis_revalidates_frames(self):
        data=self.collect()
        data['timeframes']['HOUR']['candles'][-1]['t']=NOW
        with self.assertRaises(AnalysisError):
            validate_context(data,NOW)
        data=self.collect()
        del data['timeframes']['HOUR_4']
        with self.assertRaises(AnalysisError):
            validate_context(data,NOW)
    def test_closed_market_prevents_historical_calls(self):
        self.transport.market['snapshot']['marketStatus']='CLOSED'
        with self.assertRaises(CapitalError):
            self.collect()
        self.assertFalse(any('/prices/' in c[1] for c in self.transport.calls))
    def test_query_scope_cannot_be_injected(self):
        for resolution,count in (('HOUR&max=1000',40),('HOUR',True),('HOUR',1000)):
            with self.assertRaises(CapitalError):
                self.broker.prices('EURUSD',resolution,count)
    def test_slow_analysis_blocks_context(self):
        collector=MarketCollector(self.broker,clock=lambda:NOW)
        class Client:
            def analyze(client,data):
                collector.clock=lambda:NOW+31
                return recommendation()
        with self.assertRaises(AnalysisError):
            collector.analyze('EURUSD',context()['articles'],Client())
