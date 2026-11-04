import copy
import tempfile
import unittest
from .daily import AdaptiveDailyRisk, DailyRisk
from .candles import normalize_candles
from .test_marketdata import rows
from .test_analysis import NOW


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=self.tmp.name+'/risk.sqlite'
        self.gate=AdaptiveDailyRisk(self.path,'demo-1',['EURUSD'])
        self.frame=normalize_candles(rows('MINUTE_15'),'MINUTE_15',NOW)
    def tearDown(self):
        self.gate.close()
        self.tmp.cleanup()
    def normal(self):
        self.gate.update_market('EURUSD',self.frame,NOW)
    def stressed(self):
        frame=copy.deepcopy(self.frame)
        for bar in frame['candles'][-5:]:
            bar['h']=1.2
            bar['l']=1.0
        self.gate.update_market('EURUSD',frame,NOW)
    def test_missing_and_stale_data_prevent_entry_readiness(self):
        self.assertFalse(self.gate(1000,NOW)['ready'])
        self.normal()
        self.assertTrue(self.gate(1000,NOW)['ready'])
        self.assertFalse(self.gate(1000,NOW+301)['ready'])
    def test_limit_scales_with_account_and_profit_cannot_increase_it(self):
        self.normal()
        self.assertEqual(self.gate(1000,NOW)['limit'],10)
        self.assertEqual(self.gate(2000,NOW+1)['limit'],10)
        next_day=self.gate(2000,NOW+86400)
        self.assertEqual(next_day['limit'],20)
    def test_volatility_halves_then_never_restores_limit_during_day(self):
        self.normal()
        self.gate(1000,NOW)
        self.stressed()
        high=self.gate(1000,NOW)
        self.assertEqual(high['limit'],5)
        self.assertTrue(high['stressed'])
        self.normal()
        self.assertEqual(self.gate(1000,NOW)['limit'],5)
    def test_loss_reduces_remaining_and_stop_survives_restart(self):
        self.normal()
        self.gate(1000,NOW)
        self.assertAlmostEqual(self.gate(997,NOW)['remaining'],6.97)
        self.assertTrue(self.gate(989,NOW)['stopped'])
        self.gate.close()
        self.gate=AdaptiveDailyRisk(self.path,'demo-1',['EURUSD'])
        self.assertTrue(self.gate(1100,NOW+1)['stopped'])
    def test_fixed_to_auto_preserves_baseline_and_stopped_day(self):
        fixed=DailyRisk(self.path,'demo-1',20)
        try:
            fixed(1000,NOW)
            fixed(979,NOW+1)
        finally:
            fixed.close()
        adapted=self.gate(1100,NOW+2)
        self.assertTrue(adapted['stopped'])
        self.assertEqual(adapted['baseline'],1000)
        self.assertEqual(adapted['limit'],10)
    def test_invalid_frames_and_other_markets_rejected(self):
        for epic,frame in [('OTHER',self.frame),('EURUSD',dict(self.frame,resolution='HOUR'))]:
            with self.assertRaises(ValueError):
                self.gate.update_market(epic,frame,NOW)
    def test_any_required_market_stale_blocks_entries(self):
        other=AdaptiveDailyRisk(self.tmp.name+'/other.sqlite','demo-1',['EURUSD','GOLD'])
        try:
            other.update_market('EURUSD',self.frame,NOW)
            self.assertFalse(other(1000,NOW)['ready'])
            other.update_market('GOLD',self.frame,NOW)
            self.assertTrue(other(1000,NOW)['ready'])
        finally:
            other.close()

    def test_auto_to_fixed_cannot_reset_same_day_gate(self):
        self.gate(1000,NOW)
        fixed=DailyRisk(self.path,'demo-1',250)
        try:
            with self.assertRaises(ValueError):
                fixed(1000,NOW+1)
        finally:
            fixed.close()
