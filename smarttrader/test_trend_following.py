import unittest

from .trend_following import POLICY, active_setup, frame_setup, daily_candles, reversal_exit
from .test_analysis import NOW
from .test_strategy import setup_context
from .analysis import validate_context


def bars(seconds, count, end, *, falling=False):
    result=[]
    for i in range(count):
        price=104.2-i*.1 if falling else 97+i*.1
        result.append(dict(t=end-(count-i)*seconds,o=price,
                           h=price+.08,l=price-.08,
                           c=price-.04 if falling else price+.04))
    return result


class IndependentTrendTests(unittest.TestCase):
    def context(self):
        context=setup_context()
        context.update(strategy_policy=POLICY,bid=100.9,ask=101.0,quote_time=NOW,
                       articles=[],trend_daily=bars(86400,30,NOW))
        context['timeframes']={key:value for key,value in context['timeframes'].items()
                               if key in ('HOUR','MINUTE_15')}
        context['timeframes']['HOUR']['candles']=bars(3600,40,NOW)
        return context

    def test_hourly_signal_does_not_require_daily_data(self):
        ctx=self.context(); ctx['trend_daily']=[]
        setup=active_setup(ctx,NOW)
        self.assertTrue(setup['ready'])
        self.assertEqual((setup['route'],setup['direction']),('HOUR','BUY'))
        self.assertLess(setup['stop_level'],ctx['bid'])
        validate_context(ctx,NOW,allow_missing_news=True)

    def test_daily_signal_does_not_require_hourly_data(self):
        ctx=self.context(); ctx['timeframes'].pop('HOUR')
        setup=active_setup(ctx,NOW)
        self.assertTrue(setup['ready'])
        self.assertEqual((setup['route'],setup['direction']),('DAY','BUY'))
        validate_context(ctx,NOW,allow_missing_news=True)

    def test_routes_can_disagree_without_vetoing_each_other(self):
        ctx=self.context(); ctx['trend_daily']=bars(86400,30,NOW,falling=True)
        hour=frame_setup(ctx,'HOUR',NOW)
        day=frame_setup(ctx,'DAY',NOW)
        self.assertTrue(hour['ready'])
        self.assertTrue(day['ready'])
        self.assertEqual((hour['direction'],day['direction']),('BUY','SELL'))
        self.assertNotEqual(hour['signal_at'],day['signal_at'])

    def test_reversal_requires_new_complete_opposite_candle_on_original_route(self):
        prior = bars(3600,40,NOW,falling=True)
        self.assertTrue(reversal_exit(prior,'HOUR','BUY',prior[-2]['t'],NOW))
        self.assertFalse(reversal_exit(prior,'HOUR','BUY',prior[-1]['t'],NOW))
        self.assertFalse(reversal_exit(prior,'HOUR','SELL',prior[-2]['t'],NOW))
        self.assertFalse(reversal_exit(prior,'DAY','BUY',prior[-2]['t'],NOW))
        bullish = bars(86400,30,NOW)
        self.assertTrue(reversal_exit(bullish,'DAY','SELL',bullish[-2]['t'],NOW))
        # Weekend/session gaps are real; they must not hold a losing trend
        # position for five extra trading days while its broker stop remains.
        for candle in bullish[:-4]:
            candle['t'] -= 2*86400
        self.assertTrue(reversal_exit(bullish,'DAY','SELL',bullish[-2]['t'],NOW))

    def test_open_day_is_discarded(self):
        from datetime import datetime,timezone
        completed=NOW-86400
        def row(t):
            return dict(snapshotTimeUTC=datetime.fromtimestamp(t,timezone.utc).isoformat(),
                        **{key:dict(bid=100.0,ask=100.1) for key in
                           ('openPrice','highPrice','lowPrice','closePrice')})
        # The completed daily row is retained and the current day is not.
        result=daily_candles([row(completed),row(NOW-10)],NOW)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['t'],completed)


if __name__=='__main__':
    unittest.main()
