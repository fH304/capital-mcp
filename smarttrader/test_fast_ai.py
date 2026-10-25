import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .analysis import AnalysisClient, AnalysisError, validate_recommendation
from .coordinator import plan_entry
from .evidence import technical_evidence
from .fast_ai import setup
from .runner import Config, Worker
from .test_analysis import NOW
from .test_coordinator import snapshot
from .test_strategy import setup_context
from .test_trial import authorize_test_account, original_checkpoint
from .trial import (APPROVED_ACCOUNT, AUTHORIZED_STARTED_AT, FAST_AI_AUTHORIZED_AT,
                    MODEL, TrialBudget, TrialStopped)
from .universe import rank


def market():
    context = setup_context()
    for index, bar in enumerate(context['timeframes']['MINUTE_15']['candles']):
        close = 1.09+index*.0001
        bar.update(o=close-.00015, c=close, h=close+.0002, l=close-.0002)
    context.update(bid=close-.00001, ask=close+.00001,
                   strategy_policy='fast_ai_m15', execution_supported=True,
                   news_feed_healthy=False, news_checked_at=None)
    return context


class FastAITests(unittest.TestCase):
    def test_ai_can_accept_only_the_independent_closed_candle_candidate(self):
        ctx = market()
        candidate = setup(ctx, NOW)
        self.assertTrue(candidate['ready'], candidate)
        self.assertTrue(technical_evidence(ctx, NOW, require_news_health=False)['ready'])
        recommendation = dict(action='BUY', assessment='acceptable',
                              stop_level=candidate['stop_level'],
                              target_level=candidate['target_level'],
                              reason='M15 candidate confirmed by H1', article_ids=[])
        self.assertEqual(validate_recommendation(
            recommendation, ctx, NOW, allow_technical_entry=True,
            allow_newsless_technical=True)['action'], 'BUY')
        for changed in (dict(action='SELL'),
                        dict(stop_level=candidate['stop_level']+.00001),
                        dict(target_level=candidate['target_level']+.00001)):
            with self.subTest(changed=changed), self.assertRaises(AnalysisError):
                validate_recommendation(dict(recommendation, **changed), ctx, NOW,
                                        allow_technical_entry=True,
                                        allow_newsless_technical=True)

    def test_spread_and_weak_momentum_prevent_a_paid_candidate(self):
        ctx = market()
        ctx['ask'] += .0001
        self.assertEqual(setup(ctx, NOW)['reason'], 'stop_too_wide_or_spread_too_costly')
        ctx = market()
        ctx['timeframes']['MINUTE_15']['candles'][-1]['o'] = \
            ctx['timeframes']['MINUTE_15']['candles'][-1]['c']
        self.assertEqual(setup(ctx, NOW)['reason'], 'momentum_not_confirmed')

    def test_confirmed_candidate_keeps_broker_stop_and_portfolio_risk_cap(self):
        authorize_test_account(self)
        ctx = market()
        candidate = setup(ctx,NOW)
        rec = dict(action='BUY', assessment='acceptable',
                   stop_level=candidate['stop_level'], target_level=candidate['target_level'],
                   reason='Directional evidence', article_ids=[])
        data = snapshot()
        data.update(account_id=APPROVED_ACCOUNT, bid=ctx['bid'], ask=ctx['ask'],
                    hedging_mode=True, leverage=200, margin_factor=.005, fx_rate=1,
                    quote_currency='USD', conversion_time=NOW, position_quotes={})
        plan = plan_entry(rec,ctx,data,NOW,owned={},max_positions=0,
                          allow_technical_entry=True,allow_newsless_technical=True)
        self.assertEqual((plan['strategy'],plan['stop_level'],plan['target_level']),
                         ('ai_fast_m15_v1',candidate['stop_level'],candidate['target_level']))
        self.assertLessEqual(plan['planned_risk'],.5)
        self.assertLessEqual(plan['combined_planned_risk'],2.5)

    def test_new_ai_grant_uses_existing_verified_trial_and_no_extra_budget(self):
        authorize_test_account(self)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'trial.sqlite'
            original_checkpoint(path, AUTHORIZED_STARTED_AT)
            trial = TrialBudget(path, APPROVED_ACCOUNT, FAST_AI_AUTHORIZED_AT+1)
            try:
                status = trial.status(FAST_AI_AUTHORIZED_AT+1)
                self.assertTrue(status['trial_active'])
                self.assertEqual(status['authorized_budget_usd'], 30)
                with self.assertRaisesRegex(TrialStopped, 'rules_only_renewal'):
                    trial.reserve('EURUSD', MODEL, FAST_AI_AUTHORIZED_AT+1)
                charge = trial.reserve('EURUSD', MODEL, FAST_AI_AUTHORIZED_AT+1,
                                       fast_ai=True)
                self.assertTrue(charge)
                self.assertEqual(trial.status(FAST_AI_AUTHORIZED_AT+1)['requests'], 1)
            finally:
                trial.close()

    def test_daily_cap_does_not_reserve_trial_money(self):
        class Ledger:
            def __init__(self):
                self.reservations = 0
            def reserve(self, *args, **kwargs):
                self.reservations += 1
        ledger = Ledger()
        with tempfile.TemporaryDirectory() as folder:
            client = AnalysisClient('test', MODEL, folder+'/analysis.sqlite',
                                    daily_calls=24, allow_technical_entry=True,
                                    allow_newsless_technical=True, fast_ai=True,
                                    trial=ledger)
            try:
                day = datetime.fromtimestamp(NOW, timezone.utc).date().isoformat()
                with client.db:
                    client.db.execute('INSERT INTO ai_budget VALUES (?,?)',(day,24))
                with self.assertRaisesRegex(AnalysisError,'cap exhausted'):
                    client.analyze(market(),NOW)
                self.assertEqual(ledger.reservations,0)
            finally:
                client.db.close()

    def test_fast_worker_ignores_old_rules_settings_and_calls_ai_only_for_candidate(self):
        authorize_test_account(self)
        now = FAST_AI_AUTHORIZED_AT+60
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)/'state'
            directory.mkdir()
            original_checkpoint(directory/'trial.sqlite', AUTHORIZED_STARTED_AT)
            env = dict(BOT_ACCOUNT_ID=APPROVED_ACCOUNT, CAP_API_KEY='test',
                       CAP_IDENTIFIER='test', CAP_API_PASSWORD='test',
                       OPENAI_API_KEY='test', OPENAI_MODEL='configured',
                       SMART_MARKETS='broad', SMART_AI_FAST='1',
                       SMART_PAIR_ENTRIES='1',
                       SMART_DECISION_ENGINE='rules', SMART_RULES_STRATEGY='hourly_hedge',
                       SMART_STATE_DIR=str(directory), BOT_STATE_PATH=folder+'/old.sqlite')
            config = Config(env)
            self.assertEqual((config.strategy, config.decision_engine, config.model),
                             ('ai_fast', 'ai', MODEL))
            self.assertEqual(config.max_positions,0)
            calls = []
            class Analyst:
                def analyze(self, context):
                    calls.append(context['epic'])
                    return dict(action='WAIT', assessment='reject', stop_level=0,
                                target_level=0, reason='Wait for confirmation', article_ids=[])
            worker = Worker(config, ai_factory=Analyst, clock=lambda: now,
                            emit=lambda *args, **kwargs: None)
            try:
                ctx = market()
                delta = now-NOW
                for frame in ctx['timeframes'].values():
                    for candle in frame['candles']:
                        candle['t'] += delta
                ctx['quote_time'] += delta
                ctx['collected_at'] += delta
                ctx['timeframes']['MINUTE_15']['candles'][-1]['o'] = \
                    ctx['timeframes']['MINUTE_15']['candles'][-1]['c']
                worker.monitor_market('EURUSD',ctx,rank(ctx),[])
                self.assertEqual(calls, [])
                ctx['timeframes']['MINUTE_15']['candles'][-1]['o'] -= .00015
                ctx['epic'] = 'GBPUSD'  # different market, same completed candle
                worker.monitor_market('GBPUSD',ctx,rank(ctx),[])
                self.assertEqual(calls, ['GBPUSD'])
                class Broker:
                    def configure_entries(self, *args):
                        pass
                    def login(self):
                        pass
                worker.broker_factory = lambda daily: Broker()
                _, journal = worker.resources()
                self.assertTrue(journal.allow_newsless_technical)
            finally:
                for name in ('trial','ai','schedule','journal','daily'):
                    obj = getattr(worker.local,name,None)
                    if obj is not None and hasattr(obj,'db'):
                        obj.db.close()


if __name__ == '__main__':
    unittest.main()
