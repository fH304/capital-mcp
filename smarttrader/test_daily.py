import tempfile
import unittest
from .daily import DailyRisk
from .test_analysis import NOW


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=self.tmp.name+'/risk.sqlite'
        self.gate=DailyRisk(self.path,'demo-1',20)
    def tearDown(self):
        self.gate.close()
        self.tmp.cleanup()
    def test_loss_and_profits_do_not_increase_allowance(self):
        self.assertEqual(self.gate(1000,NOW)['remaining'],20)
        self.assertEqual(self.gate(990,NOW+1)['remaining'],10)
        self.assertEqual(self.gate(1050,NOW+2)['remaining'],20)
    def test_stop_latched_across_recovery_and_restart(self):
        self.gate(1000,NOW)
        self.assertTrue(self.gate(979,NOW+1)['stopped'])
        self.gate.close()
        self.gate=DailyRisk(self.path,'demo-1',20)
        self.assertTrue(self.gate(1010,NOW+2)['stopped'])
        self.assertEqual(self.gate(1010,NOW+3)['remaining'],0)
    def test_new_utc_day_records_new_baseline(self):
        self.gate(1000,NOW)
        self.gate(979,NOW+1)
        self.assertFalse(self.gate(979,NOW+86400)['stopped'])
        self.assertEqual(self.gate(979,NOW+86401)['baseline'],979)
    def test_changing_limit_cannot_reset_stopped_day(self):
        self.gate(1000,NOW)
        other=DailyRisk(self.path,'demo-1',250)
        try:
            with self.assertRaises(ValueError):
                other(1000,NOW+1)
        finally:
            other.close()
    def test_account_scope_is_separate(self):
        self.gate(1000,NOW)
        self.gate(979,NOW+1)
        other=DailyRisk(self.path,'demo-2',20)
        try:
            self.assertFalse(other(1000,NOW+2)['stopped'])
        finally:
            other.close()
    def test_clock_cannot_reset_to_prior_day(self):
        self.gate(1000,NOW)
        with self.assertRaises(ValueError):
            self.gate(1000,NOW-86400)
