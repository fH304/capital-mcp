"""Fast AI uses only its required closed candles and blocks legacy exposure."""
import unittest

from .analysis import AnalysisError, validate_context
from .candles import MarketDataError
from .exposure import ExposureError, portfolio_entry_limits
from .institutional import market_brief
from .marketdata import MarketCollector
from .test_analysis import NOW
from .test_exposure import data, leg
from .test_marketdata import rows


class FastAIMarketTests(unittest.TestCase):
    def market(self, *, gap=False):
        class Broker:
            def __init__(self):
                self.requests=[]

            def observation_quote(self, epic):
                return dict(bid=1.10,ask=1.101,quote_time=NOW,
                            execution_supported=True)

            market_quote=observation_quote

            def prices(self, epic, resolution, count):
                self.requests.append(resolution)
                self.assertions(resolution)
                result=rows(resolution)
                if gap and resolution=='HOUR':
                    del result[-3]
                return result

            @staticmethod
            def assertions(resolution):
                if resolution not in ('MINUTE_15','HOUR'):
                    raise AssertionError('Fast AI must not fetch H4')

        broker=Broker()
        collector=MarketCollector(broker,clock=lambda:NOW)
        collector.fast_ai=True
        return broker,collector

    def test_fast_ai_accepts_fresh_m15_h1_without_h4(self):
        broker,collector=self.market()
        ctx=collector.collect_market('EURUSD')
        self.assertEqual(broker.requests,['MINUTE_15','HOUR'])
        self.assertEqual(set(ctx['timeframes']),{'MINUTE_15','HOUR'})
        ctx.update(strategy_policy='fast_ai_m15',articles=[])
        validate_context(ctx,NOW,allow_missing_news=True)
        self.assertEqual(set(market_brief(ctx,NOW)['frames']),{'MINUTE_15','HOUR'})
        self.assertNotIn('HOUR_4',market_brief(ctx,NOW)['frames'])

    def test_missing_or_gapped_required_frame_still_blocks(self):
        _,collector=self.market(gap=True)
        with self.assertRaises(MarketDataError):
            collector.collect_market('EURUSD')
        _,collector=self.market()
        ctx=collector.collect_market('EURUSD')
        ctx.update(strategy_policy='fast_ai_m15',articles=[])
        del ctx['timeframes']['HOUR']
        with self.assertRaises(AnalysisError):
            validate_context(ctx,NOW,allow_missing_news=True)

    def test_fast_ai_waits_for_legacy_position_to_close(self):
        snapshot=data()
        plan,position=leg()
        plan['strategy']='hourly_hedge_v1'
        snapshot.update(epic='GBPUSD',positions=[position],position_quotes={
            'old':dict(epic='EURUSD',quote_currency='USD',bid=1.101,ask=1.102,
                       quote_time=NOW,conversion_time=NOW,quote_to_account=1,
                       fx_rate=1,margin_factor=.005)},daily_remaining_risk=1000)
        with self.assertRaisesRegex(ExposureError,'Close legacy positions'):
            portfolio_entry_limits(snapshot,{'old':plan},candle=NOW-900,now=NOW,fast_ai=True)


if __name__=='__main__':
    unittest.main()
