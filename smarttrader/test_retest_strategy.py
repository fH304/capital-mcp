import unittest

from .retest_strategy import daily_breakouts, entry, m15_confirmation


class RetestStrategyTest(unittest.TestCase):
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

    def test_cost_and_chase_checks_use_next_executable_open(self):
        setup = {'direction': 1, 'stop': 99, 'level': 100, 'atr_hour': 2}
        accepted = entry(setup, {'bid': 100.3, 'ask': 100.4})
        self.assertIsNotNone(accepted)
        self.assertAlmostEqual(accepted['target'], 103.2)
        self.assertIsNone(entry(setup, {'bid': 100.1, 'ask': 100.5}))
        self.assertIsNone(entry(setup, {'bid': 101.1, 'ask': 101.2}))


if __name__ == '__main__':
    unittest.main()
