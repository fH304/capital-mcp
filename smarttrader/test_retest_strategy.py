import unittest
from datetime import datetime, timedelta, timezone

from .retest_strategy import daily_breakouts, entry, m15_confirmation, live_setup
from .strategy import active_setup, accepts_levels
from .evidence import technical_evidence


class RetestStrategyTest(unittest.TestCase):
    def test_only_recent_closed_retest_can_authorize_rules_entry(self):
        def row(moment, opening, high, low, close):
            sides=lambda x:{'bid':x,'ask':x}
            return {'snapshotTimeUTC':moment.strftime('%Y-%m-%dT%H:%M:%S'),
                    'openPrice':sides(opening),'highPrice':sides(high),
                    'lowPrice':sides(low),'closePrice':sides(close)}
        now=datetime(2026,10,18,18,5,tzinfo=timezone.utc)
        daily=[row(datetime(2026,9,23,tzinfo=timezone.utc)+timedelta(days=i),
                   99,100,98,99) for i in range(25)]
        daily[-1]=row(datetime(2026,10,17,tzinfo=timezone.utc),99,102,98,101)
        hourly=[row(datetime(2026,10,17,2,tzinfo=timezone.utc)+timedelta(hours=i),
                    100,100.5,99.5,100) for i in range(40)]
        hourly[-1]=row(datetime(2026,10,18,17,tzinfo=timezone.utc),100,100.6,99.95,100.4)
        m15=[row(datetime(2026,10,18,17,45,tzinfo=timezone.utc),100,100.45,99.95,100.4)]
        context={'strategy_policy':'three_month_retest_only','execution_supported':True,
                 'bid':100.30,'ask':100.32,
                 'retest_history':{'daily':daily,'hourly':hourly,'m15':m15}}
        setup=active_setup(context,now.timestamp())
        self.assertTrue(setup['ready'],setup)
        self.assertEqual(setup['direction'],'BUY')
        self.assertTrue(technical_evidence(context,now.timestamp(),require_news_health=False)['ready'])
        self.assertTrue(accepts_levels(setup,'BUY',setup['stop_level'],setup['target_level'],
                                       context['bid'],context['ask']))
        self.assertFalse(active_setup(context,(now+timedelta(minutes=11)).timestamp())['ready'])
        self.assertFalse(active_setup(dict(context,retest_history={}),now.timestamp())['ready'])

    def test_daily_signal_uses_prior_twenty_bars_and_is_known_after_close(self):
        def bar(day, high, low, close):
            sides = lambda value: {'bid': value, 'ask': value}
            return {'snapshotTimeUTC': f'2026-09-{day:02d}T00:00:00',
                    'highPrice': sides(high), 'lowPrice': sides(low),
                    'closePrice': sides(close)}
        history = [bar(day, 12, 8, 10) for day in range(1, 21)]
        history.append(bar(21, 14, 9, 13))
        signals = daily_breakouts(history)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['direction'], 1)
        self.assertEqual(signals[0]['level'], 12)
        self.assertEqual(signals[0]['known_at'], 1790035200)
        history[-1]['closePrice'] = {'bid': 11, 'ask': 11}
        self.assertEqual(daily_breakouts(history), [])

    def test_spread_and_distance_do_not_veto_positive_protected_entry(self):
        setup = {'direction': 1, 'stop': 99, 'level': 100, 'atr_hour': 2}
        accepted = entry(setup, {'bid': 100.3, 'ask': 100.4})
        self.assertIsNotNone(accepted)
        self.assertAlmostEqual(accepted['target'], 103.2)
        self.assertIsNotNone(entry(setup, {'bid': 100.1, 'ask': 100.5}))
        self.assertIsNotNone(entry(setup, {'bid': 101.1, 'ask': 101.2}))
        self.assertIsNone(entry(setup, {'bid': 98.8, 'ask': 98.9}))
        self.assertIsNone(entry(setup, {'bid': 100.4, 'ask': 100.3}))


if __name__ == '__main__':
    unittest.main()
