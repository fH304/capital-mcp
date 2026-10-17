import unittest
from smarttrader.sizing import size_position, SizingError

class SizingTests(unittest.TestCase):
    def size(self, **changes):
        args = dict(equity=1000, free_margin=1000, stop_distance=1, spread=0,
                    point_value=1, quote_to_account=1, margin_per_unit=1,
                    min_size=0.01, size_step=0.01, max_size=100000,
                    open_risk=0, daily_remaining_risk=250)
        args.update(changes)
        return size_position(**args)
    def test_growth_and_drawdown(self):
        self.assertEqual(self.size()['size'],2.5)
        self.assertEqual(self.size(equity=2000)['size'],5)
        self.assertEqual(self.size(equity=500)['size'],1.25)
    def test_wider_stop_reduces_size(self):
        self.assertEqual(self.size(stop_distance=2)['size'],1.25)
    def test_currency_conversion(self):
        self.assertEqual(self.size(quote_to_account=2)['size'],1.25)
    def test_margin_constraint(self):
        self.assertEqual(self.size(margin_per_unit=100)['size'],1)
    def test_never_rounds_up_risk(self):
        r = self.size(stop_distance=3, size_step=0.1)
        self.assertLessEqual(r['planned_risk'],r['risk_budget'])
        self.assertEqual(r['size'],0.8)
    def test_existing_risk_and_day_budget(self):
        self.assertEqual(self.size(open_risk=1,daily_remaining_risk=2)['risk_budget'],1)
        with self.assertRaises(SizingError): self.size(open_risk=5)
        with self.assertRaises(SizingError): self.size(daily_remaining_risk=0)
    def test_broker_minimum_rejects(self):
        with self.assertRaises(SizingError): self.size(min_size=100)
    def test_invalid_inputs_reject(self):
        for changes in [dict(equity=float('nan')),dict(quote_to_account=0),
                        dict(risk_fraction=0.9),dict(spread=-1)]:
            with self.assertRaises(SizingError): self.size(**changes)

if __name__ == '__main__': unittest.main()
