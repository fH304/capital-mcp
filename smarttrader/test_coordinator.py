import tempfile
import unittest
from unittest.mock import patch

from .analysis import AnalysisError
from .coordinator import CoordinationError, DemoCoordinator, plan_entry
from .test_analysis import NOW, context, recommendation


def snapshot():
    return dict(environment='demo', account_id='demo-1', epic='EURUSD',
                account_time=NOW, positions_time=NOW, orders_time=NOW,
                contract_time=NOW, quote_time=NOW, bid=1.10, ask=1.101,
                tradeable=True, daily_stopped=False, positions=[], working_orders=[],
                equity=1000, free_margin=1000, point_value=1, quote_to_account=1,
                margin_per_unit=.02, min_size=1, size_step=1, max_size=100000,
                open_risk=0, daily_remaining_risk=20)


class Broker:
    environment, account_id = 'demo', 'demo-1'
    def __init__(self):
        self.data, self.calls, self.failure, self.mismatch = snapshot(), 0, False, False
    def snapshot(self, epic):
        return self.data
    def submit_entry(self, plan):
        self.calls += 1
        self.plan = plan
        if self.failure:
            raise TimeoutError('secret credential')
        return 'reference-1'
    def confirm_entry(self, reference):
        result = dict(self.plan, status='confirmed', deal_id='deal-1')
        if self.mismatch:
            result['stop_level'] = 0
        return result


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name+'/entries.sqlite'
        self.coordinator = DemoCoordinator(self.path, 'demo-1')
        self.broker = Broker()
    def tearDown(self):
        self.coordinator.close()
        self.tmp.cleanup()
    def process(self, signal='candle-1', **kwargs):
        return self.coordinator.process(signal, recommendation(), context(), self.broker,
                                        now=NOW, **kwargs)
    def test_preview_never_submits(self):
        result = self.process()
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(self.broker.calls, 0)
        self.assertLessEqual(result['plan']['planned_risk'], 1.25)
        # Spread counted once: BUY executable entry minus stop = .011.
        self.assertAlmostEqual(result['plan']['planned_risk'], result['plan']['size']*.011)
    def test_confirmation_and_persistent_duplicate(self):
        self.assertEqual(self.process(armed=True)['status'], 'confirmed')
        self.coordinator.close()
        self.coordinator = DemoCoordinator(self.path, 'demo-1')
        self.assertEqual(self.process(armed=True)['status'], 'duplicate')
        self.assertEqual(self.broker.calls, 1)
    def test_timeout_blocks_new_signal_after_restart_and_redacts(self):
        self.broker.failure = True
        with self.assertRaises(CoordinationError) as error:
            self.process(armed=True)
        self.assertNotIn('secret', str(error.exception))
        self.coordinator.close()
        self.coordinator = DemoCoordinator(self.path, 'demo-1')
        with self.assertRaises(CoordinationError):
            self.process('candle-2', armed=True)
        self.assertEqual(self.broker.calls, 1)
    def test_missing_position_protection_blocks(self):
        self.broker.mismatch = True
        with self.assertRaises(CoordinationError):
            self.process(armed=True)
        with self.assertRaises(CoordinationError):
            self.process('candle-2', armed=True)
    def test_real_and_wrong_account_block(self):
        for field, value in (('environment', 'live'), ('account_id', 'another')):
            setattr(self.broker, field, value)
            with self.assertRaises(CoordinationError):
                self.process(armed=True)
            setattr(self.broker, field, getattr(Broker, field))
        self.assertEqual(self.broker.calls, 0)
    def test_account_market_and_daily_gate_block(self):
        for key, value in (('account_id', 'another'), ('epic', 'GOLD'),
                           ('positions', [{'deal_id':'old'}]), ('working_orders', [{}]),
                           ('daily_stopped', True), ('tradeable', False)):
            self.broker.data = snapshot()
            self.broker.data[key] = value
            with self.assertRaises(CoordinationError):
                self.process(armed=True)
        self.assertEqual(self.broker.calls, 0)
    def test_all_snapshot_timestamps_required(self):
        for key in ('account_time', 'positions_time', 'orders_time', 'contract_time'):
            self.broker.data = snapshot()
            self.broker.data[key] = NOW-31
            with self.assertRaises(CoordinationError):
                self.process(armed=True)
    def test_changed_price_invalidates_old_reward_risk(self):
        self.broker.data.update(bid=1.12, ask=1.121)
        with self.assertRaises(AnalysisError):
            self.process(armed=True)
        self.assertEqual(self.broker.calls, 0)
    def test_clock_is_checked_after_snapshot_collection(self):
        with patch('smarttrader.coordinator.time.time', return_value=NOW+31):
            with self.assertRaises(CoordinationError):
                self.coordinator.process('candle-1', recommendation(), context(), self.broker, armed=True)
        self.assertEqual(self.broker.calls, 0)
    def test_wait_does_not_reserve_or_submit(self):
        r = recommendation()
        r.update(action='WAIT', assessment='reject')
        result = self.coordinator.process('candle-1', r, context(), self.broker, now=NOW, armed=True)
        self.assertEqual(result['status'], 'wait')
        self.assertEqual(self.coordinator.db.execute('SELECT count(*) FROM smart_entries').fetchone()[0], 0)
