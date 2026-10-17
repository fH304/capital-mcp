import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .analysis import AnalysisClient, AnalysisError, INSTRUCTIONS, validate_recommendation
from .candles import MarketDataError
from .coordinator import DemoCoordinator, plan_entry
from .institutional import InstitutionalStudy, market_brief, shadow_setup
from .marketdata import MarketCollector
from .test_analysis import NOW, context, recommendation
from .test_coordinator import Broker, snapshot
from . import test_continuous
from .test_capital import Response as HTTPResponse
from .test_universe import trending
from .universe import rank


def breakout(action='BUY', spread=.0001):
    ctx = trending()
    ctx['timeframes']['MINUTE_15']['candles'][-1].update(o=1.0388, h=1.0401, l=1.0385, c=1.0398)
    ctx.update(bid=1.0398-spread/2, ask=1.0398+spread/2)
    if action == 'SELL':
        for frame in ctx['timeframes'].values():
            for b in frame['candles']:
                b.update(o=2-b['o'], c=2-b['c'], h=2-b['l'], l=2-b['h'])
        ctx.update(bid=2-ctx['ask'], ask=2-ctx['bid'])
    return ctx


class BriefTests(unittest.TestCase):
    def test_observed_trend_levels_and_missing_data_not_global_bank_flow(self):
        ctx = breakout()
        brief = market_brief(ctx, NOW)
        self.assertEqual(brief['regime'], 'trend_up')
        self.assertAlmostEqual(brief['frames']['MINUTE_15']['prior_resistance'], 1.03905)
        self.assertIn('scheduled_macro_calendar', brief['missing_data'])
        self.assertIn('bank_client_flow', brief['missing_data'])
        self.assertNotIn('probability', brief)
        self.assertNotIn('action', brief)
        self.assertNotIn('vwap', json.dumps(brief).lower())
        self.assertIsNone(brief['spread_atr_limit'])

    def test_range_and_mixed_are_distinguished(self):
        ctx = breakout()
        for frame in ctx['timeframes'].values():
            for b in frame['candles']:
                b.update(o=1.02, c=1.02, h=1.022, l=1.018)
        self.assertEqual(market_brief(ctx, NOW)['regime'], 'range')
        ctx['timeframes']['HOUR_4'] = breakout()['timeframes']['HOUR_4']
        brief = market_brief(ctx, NOW)
        self.assertEqual(brief['regime'], 'mixed')
        self.assertEqual(shadow_setup(ctx, brief)['action'], 'WAIT')

    def test_stale_crossed_and_unclosed_observations_rejected(self):
        for kind in ('stale', 'crossed', 'unclosed'):
            ctx = breakout()
            if kind == 'stale':
                ctx['quote_time'] = NOW-31
            elif kind == 'crossed':
                ctx['ask'] = ctx['bid']-.001
            else:
                ctx['timeframes']['MINUTE_15']['candles'][-1]['t'] = NOW
            with self.subTest(kind=kind), self.assertRaises(MarketDataError):
                market_brief(ctx, NOW)

    def test_old_gap_remains_visible_and_blocks_paper_setup(self):
        ctx = breakout()
        frame = ctx['timeframes']['HOUR_4']
        for b in frame['candles'][:-10]:
            b['t'] -= frame['seconds']
        brief = market_brief(ctx, NOW)
        self.assertFalse(brief['frames']['HOUR_4']['history_contiguous'])
        self.assertEqual(shadow_setup(ctx, brief)['reason'], 'lookback_history_gap')

    def test_analyst_payload_recomputes_brief_and_excludes_paper_order(self):
        ctx = dict(breakout(), articles=context()['articles'], desk_brief={'regime': 'fake_bank_signal'})
        payloads = []
        def opener(request, timeout):
            payloads.append(json.loads(request.data))
            result = dict(recommendation(), stop_level=1.03, target_level=1.07)
            return HTTPResponse(dict(status='completed', output=[dict(type='message', content=[
                dict(type='output_text', text=json.dumps(result))])]))
        with tempfile.TemporaryDirectory() as folder:
            ai = AnalysisClient('fixture', 'fixture', folder+'/ai.sqlite', opener=opener)
            try:
                ai.analyze(ctx, NOW)
            finally:
                ai.db.close()
        supplied = json.loads(payloads[0]['input'])
        self.assertEqual(supplied['desk_brief']['regime'], 'trend_up')
        self.assertNotIn('shadow_action', supplied['desk_brief'])
        self.assertNotIn('tools', payloads[0])
        self.assertLess(len(payloads[0]['input'].encode()), 30000)
        self.assertIn('no fixed spread/ATR rejection threshold', INSTRUCTIONS)


class SetupTests(unittest.TestCase):
    def test_buy_and_sell_are_unvalidated_structural_paper_hypotheses(self):
        for action in ('BUY', 'SELL'):
            ctx = breakout(action)
            original = copy.deepcopy(ctx)
            setup = shadow_setup(ctx, market_brief(ctx, NOW))
            self.assertEqual(ctx, original)
            self.assertEqual(setup['action'], action)
            self.assertFalse(setup['execution_authorized'])
            self.assertFalse(setup['validated'])
            sign = 1 if action == 'BUY' else -1
            self.assertGreater(sign*(setup['entry_price']-setup['stop_level']), 0)
            self.assertAlmostEqual(sign*(setup['target_level']-setup['entry_price'])/setup['risk_distance'], 2)

    def test_spread_above_old_reference_is_not_a_paper_veto(self):
        ctx = breakout(spread=.001)
        brief = market_brief(ctx, NOW)
        self.assertGreater(brief['spread_to_atr'], .2)
        self.assertEqual(shadow_setup(ctx, brief)['action'], 'BUY')
        self.assertGreater(rank(ctx)['score'], 0)

    def test_breakout_retest_uses_reference_before_both_signal_bars(self):
        ctx = breakout()
        bars = ctx['timeframes']['MINUTE_15']['candles']
        bars[-2].update(o=1.037, c=1.0388, h=1.0390, l=1.0369)
        bars[-1].update(o=1.0382, c=1.0387, h=1.0390, l=1.0379)
        ctx.update(bid=1.03865, ask=1.03875)
        setup = shadow_setup(ctx, market_brief(ctx, NOW))
        self.assertEqual(setup['reason'], 'breakout_retest')
        self.assertLess(setup['reference_level'], bars[-2]['c'])

    def test_shock_chasing_reversal_and_unsupported_contract_do_not_signal(self):
        expected = dict(shock='volatility_shock', chase='price_extended_no_chasing',
                        reversal='breakout_reversed_before_observation', unsupported='observation_only_contract')
        for kind, reason in expected.items():
            ctx = breakout()
            if kind == 'shock':
                ctx['timeframes']['MINUTE_15']['candles'][-1]['h'] += .03
            elif kind == 'chase':
                ctx.update(bid=ctx['bid']+.003, ask=ctx['ask']+.003)
            elif kind == 'reversal':
                ctx.update(bid=1.038, ask=1.0381)
            else:
                ctx['execution_supported'] = False
            with self.subTest(kind=kind):
                setup = shadow_setup(ctx, market_brief(ctx, NOW))
                self.assertEqual(setup['action'], 'WAIT')
                self.assertEqual(setup['reason'], reason)


class StudyTests(unittest.TestCase):
    def test_restart_dedup_report_is_read_only_and_waits_are_included(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'institutional.sqlite'
            ctx = breakout()
            study = InstitutionalStudy(path, 'demo-1')
            self.assertTrue(study.observe(ctx, NOW)['recorded'])
            study.close()
            study = InstitutionalStudy(path, 'demo-1')
            self.assertFalse(study.observe(ctx, NOW+1)['recorded'])
            ctx['epic'] = 'GOLD'
            ctx['execution_supported'] = False
            self.assertTrue(study.observe(ctx, NOW)['recorded'])
            study.close()
            before = path.read_bytes()
            report = InstitutionalStudy.report(path, 'demo-1', NOW+2)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(report['markets']['EURUSD']['candidates'], 1)
            self.assertEqual(report['markets']['GOLD']['candidates'], 0)
            self.assertFalse(report['execution_authorized'])
            self.assertIsNone(report['markets']['EURUSD']['mean_price_R'])
            missing = Path(folder)/'missing.sqlite'
            with self.assertRaises(sqlite3.OperationalError):
                InstitutionalStudy.report(missing, 'demo-1', NOW)
            self.assertFalse(missing.exists())

    def test_mature_pending_is_missing_data_not_profitable_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'institutional.sqlite'
            study = InstitutionalStudy(path, 'demo-1')
            study.observe(breakout(), NOW)
            study.close()
            report = InstitutionalStudy.report(path, 'demo-1', NOW+20*900)
            self.assertEqual(report['markets']['EURUSD']['outcome_counts'], {'missing_bars': 1})
            self.assertEqual(report['markets']['EURUSD']['completed'], 0)

    def test_paper_signal_does_not_submit_when_analyst_waits(self):
        fixture = test_continuous.ContinuousTests(methodName='test_all_21_analyzed_without_news_entry_rank_or_account_readiness')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        worker = fixture.make()
        self.addCleanup(lambda: worker.local.institutional_study.close())
        with patch.object(MarketCollector, 'collect_market', lambda _, epic: dict(breakout(), epic=epic)):
            worker.opportunities()
        signals = [fields for event, fields in fixture.events if event == 'institutional_observation']
        self.assertEqual(signals[0]['shadow_action'], 'BUY')
        self.assertIn('entry_wait', [event for event, _ in fixture.events])
        self.assertFalse(any(method == 'POST' and url.endswith('/positions') for method, url, _ in fixture.transport.calls))

    def test_future_stop_and_target_are_censored_as_ambiguous(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'institutional.sqlite'
            study = InstitutionalStudy(path, 'demo-1')
            ctx = breakout()
            study.observe(ctx, NOW)
            candidate = json.loads(study.db.execute('SELECT payload FROM desk_observations').fetchone()[0])
            spread = ctx['ask']-ctx['bid']
            bid = dict(o=ctx['bid'], c=ctx['bid'], l=candidate['stop_level']-.001,
                       h=candidate['target_level']+.001)
            future = dict(t=candidate['paper_start'], bid=bid, ask={k:v+spread for k,v in bid.items()})
            fresh = copy.deepcopy(ctx)
            fresh['quote_time'] += 1800
            for frame in fresh['timeframes'].values():
                for candle in frame['candles']:
                    candle['t'] += 1800
            study.observe(fresh, NOW+1800, [future])
            result = json.loads(study.db.execute('SELECT outcome FROM desk_observations ORDER BY candle LIMIT 1').fetchone()[0])
            self.assertEqual(result['status'], 'ambiguous')
            self.assertNotIn('price_R', result)
            study.close()

    def test_study_disk_failure_leaves_analysis_and_orders_independent(self):
        fixture = test_continuous.ContinuousTests(methodName='test_all_21_analyzed_without_news_entry_rank_or_account_readiness')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.buy = {'EURUSD'}
        worker = fixture.make()
        fixture.prime(worker)
        with patch.object(worker, 'institutional_study', side_effect=sqlite3.OperationalError('private-error')):
            fixture.sweep(worker)
        self.assertEqual(len(fixture.contexts), 21)
        self.assertIn('institutional_study_failed', [event for event, _ in fixture.events])
        self.assertIn('entry_preview', [event for event, _ in fixture.events])
        self.assertNotIn('private-error', json.dumps(fixture.events))
        self.assertFalse(any(method == 'POST' and url.endswith('/positions') for method, url, _ in fixture.transport.calls))


class CostAwareEntryTests(unittest.TestCase):
    def inputs(self, ask):
        ctx = dict(context(), bid=100, ask=ask)
        rec = dict(recommendation(), stop_level=99, target_level=115)
        snap = dict(snapshot(), bid=100, ask=ask, min_size=.01, size_step=.01)
        return ctx, rec, snap

    def test_wider_spread_reduces_size_without_a_fixed_ratio_veto(self):
        plans = []
        for ask in (101, 104):
            ctx, rec, snap = self.inputs(ask)
            plans.append(plan_entry(rec, ctx, snap, NOW))
        self.assertLess(plans[1]['size'], plans[0]['size'])
        self.assertLessEqual(plans[1]['planned_risk'], 1.25)
        self.assertAlmostEqual(plans[1]['planned_risk'], plans[1]['size']*5)

    def test_spread_that_destroys_reward_risk_blocks_before_order(self):
        ctx, rec, snap = self.inputs(106)
        with tempfile.TemporaryDirectory() as folder:
            journal = DemoCoordinator(folder+'/orders.sqlite', 'demo-1')
            broker = Broker()
            broker.data = snap
            try:
                with self.assertRaisesRegex(AnalysisError, 'Reward/risk'):
                    journal.process('wide', rec, ctx, broker, now=NOW, armed=True)
                self.assertEqual(broker.calls, 0)
            finally:
                journal.close()

    def test_quotes_refreshed_before_submission_can_invalidate_costs(self):
        ctx, rec, _ = self.inputs(104)
        validate_recommendation(rec, ctx, NOW)
        changed = dict(snapshot(), bid=100, ask=106, min_size=.01, size_step=.01)
        with self.assertRaisesRegex(AnalysisError, 'Reward/risk'):
            plan_entry(rec, ctx, changed, NOW)


if __name__ == '__main__':
    unittest.main()
