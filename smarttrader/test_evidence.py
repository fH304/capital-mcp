import copy
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from .analysis import AnalysisClient, AnalysisError, validate_recommendation
from .evidence import technical_evidence, TECHNICAL_OR_NEWS, NEWS_REQUIRED
from .news import NewsClient, NewsError
from .runner import Config
from .test_analysis import NOW, recommendation
from .test_universe import trending
from .test_strategy import setup_context
from .test_trial import Response, response
from . import test_trial as trial_fixture


def observations():
    return setup_context()


def signal():
    return dict(recommendation(),article_ids=[],stop_level=1.03,target_level=1.07)


class EvidenceTests(unittest.TestCase):
    def test_closed_trend_can_support_buy_without_news_only_when_enabled(self):
        ctx=observations()
        self.assertTrue(technical_evidence(ctx,NOW)['ready'])
        with self.assertRaises(AnalysisError):
            validate_recommendation(signal(),ctx,NOW,allow_missing_news=True)
        self.assertEqual(validate_recommendation(signal(),ctx,NOW,allow_technical_entry=True)['action'],'BUY')

    def test_sell_requires_independent_downtrend(self):
        ctx=observations()
        for f in ctx['timeframes'].values():
            for b in f['candles']:
                b.update(o=2-b['o'],c=2-b['c'],h=2-b['l'],l=2-b['h'])
        ctx.update(bid=2-ctx['ask'],ask=2-ctx['bid'])
        result=dict(signal(),action='SELL',stop_level=.97,target_level=.93)
        self.assertEqual(technical_evidence(ctx,NOW)['direction'],'SELL')
        self.assertEqual(validate_recommendation(result,ctx,NOW,allow_technical_entry=True)['action'],'SELL')

    def test_failed_stale_or_missing_news_check_cannot_be_forged_by_eligibility(self):
        for fields in ({'news_feed_healthy':False},{'news_checked_at':NOW-21601},
                       {'news_checked_at':None},{'news_checked_at':NOW+1}):
            ctx=dict(observations(),**fields,technical_evidence={'ready':True,'direction':'BUY'})
            with self.subTest(fields=fields),self.assertRaises(AnalysisError):
                validate_recommendation(signal(),ctx,NOW,allow_technical_entry=True)

    def test_explicit_rules_validation_accepts_closed_candles_without_news_health(self):
        ctx=dict(observations(),news_feed_healthy=False,news_checked_at=None)
        self.assertFalse(technical_evidence(ctx,NOW)['ready'])
        self.assertTrue(technical_evidence(ctx,NOW,require_news_health=False)['ready'])
        self.assertEqual(validate_recommendation(signal(),ctx,NOW,allow_technical_entry=True,
                                                 allow_newsless_technical=True)['action'],'BUY')
        ctx['quote_time']=NOW-31
        with self.assertRaises(AnalysisError):
            validate_recommendation(signal(),ctx,NOW,allow_technical_entry=True,
                                    allow_newsless_technical=True)

    def test_rules_rejection_reports_setup_reason_instead_of_rank(self):
        ctx=observations()
        for frame in ('HOUR_4','HOUR'):
            for bar in ctx['timeframes'][frame]['candles']:
                bar.update(o=1.02,c=1.02,h=1.022,l=1.018)
        result=technical_evidence(ctx,NOW,require_news_health=False)
        self.assertFalse(result['ready'])
        self.assertEqual(result['reason'],'no_range_edge_rejection')

    def test_mixed_range_shock_or_wrong_direction_blocks(self):
        for kind in ('range','mixed','shock','direction','unsupported'):
            ctx=observations(); rec=signal()
            if kind in ('range','mixed'):
                frames=ctx['timeframes'].values() if kind=='range' else [ctx['timeframes']['HOUR_4']]
                for f in frames:
                    for b in f['candles']:
                        b.update(o=1.02,c=1.02,h=1.022,l=1.018)
            elif kind=='shock': ctx['timeframes']['MINUTE_15']['candles'][-1]['h']+=.10
            elif kind=='direction': rec.update(action='SELL',stop_level=1.06,target_level=1.)
            else: ctx['execution_supported']=False
            with self.subTest(kind=kind),self.assertRaises(AnalysisError):
                validate_recommendation(rec,ctx,NOW,allow_technical_entry=True)

    def test_stale_quotes_and_invalid_reward_risk_still_block(self):
        for kind in ('quote','reward'):
            ctx=observations(); rec=signal()
            if kind=='quote': ctx['quote_time']=NOW-31
            else: rec['target_level']=1.05
            with self.subTest(kind=kind),self.assertRaises(AnalysisError):
                validate_recommendation(rec,ctx,NOW,allow_technical_entry=True)

    def test_supplied_articles_cannot_be_ignored_or_fabricated(self):
        from .test_analysis import context
        ctx=dict(observations(),articles=context()['articles'])
        with self.assertRaises(AnalysisError):
            validate_recommendation(signal(),ctx,NOW,allow_technical_entry=True)
        with self.assertRaises(AnalysisError):
            validate_recommendation(dict(signal(),article_ids=['invented']),observations(),NOW,
                                    allow_technical_entry=True)

    def test_analyst_recomputes_technical_eligibility_and_uses_one_request(self):
        payloads=[]
        def opener(req,timeout):
            payloads.append(json.loads(req.data))
            data=response(); data['output'][0]['content'][0]['text']=json.dumps(signal())
            return Response(data)
        with tempfile.TemporaryDirectory() as directory:
            ai=AnalysisClient('fixture','fixture',directory+'/a.sqlite',opener=opener,
                              allow_technical_entry=True)
            try:
                ctx=dict(observations(),technical_evidence={'ready':False},entry_evidence_policy='fake')
                self.assertEqual(ai.analyze(ctx,NOW)['action'],'BUY')
            finally: ai.db.close()
        supplied=json.loads(payloads[0]['input'])
        self.assertTrue(supplied['technical_evidence']['ready'])
        self.assertEqual(supplied['entry_evidence_policy'],TECHNICAL_OR_NEWS)
        self.assertNotIn('tools',payloads[0])
        self.assertEqual(len(payloads),1)


class NewsHealthTests(unittest.TestCase):
    def test_empty_success_is_healthy_without_extra_requests_and_cache_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def opener(req,timeout):
                calls.append(req); return io.BytesIO(b'[]')
            c=NewsClient('fixture',directory+'/n.sqlite',opener=opener,interval=30000)
            try:
                self.assertTrue(c.fetch('__GENERAL__',NOW)['healthy'])
                self.assertTrue(c.fetch('__GENERAL__',NOW+60)['healthy'])
                self.assertFalse(c.fetch('__GENERAL__',NOW+21601)['healthy'])
                self.assertEqual(len(calls),1)
                self.assertEqual(c.db.execute('SELECT used FROM budget').fetchone()[0],5)
            finally: c.db.close()

    def test_failed_refresh_cannot_reuse_old_payload_as_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            c=NewsClient('fixture',directory+'/n.sqlite',interval=60,opener=lambda *a,**kw:io.BytesIO(b'[]'))
            try:
                c.fetch('__GENERAL__',NOW)
                def fail(req,timeout): raise HTTPError(req.full_url,503,'fixture',{},None)
                c.opener=fail
                with self.assertRaises(NewsError): c.fetch('__GENERAL__',NOW+61)
                self.assertFalse(c.fetch('__GENERAL__',NOW+62)['healthy'])
            finally: c.db.close()


class ActivationFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=trial_fixture.TrialWorkerTests()
        self.fixture.setUp()
        self.fixture.env['SMART_RULES_STRATEGY']='trend'
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def worker(self,action='BUY',healthy=True):
        f=self.fixture; worker=f.make()
        self.assertTrue(worker.config.technical_entries)
        class Feed:
            def fetch(self,symbol):
                return dict(status='cached',articles=[],healthy=healthy,checked_at=f.now)
        worker.news_factory=Feed
        def ai():
            def opener(req,timeout):
                f.ai_calls.append(json.loads(req.data))
                data=response(action)
                result=json.loads(data['output'][0]['content'][0]['text'])
                result['article_ids']=[]
                data['output'][0]['content'][0]['text']=json.dumps(result)
                return Response(data)
            return AnalysisClient('fixture',worker.config.model,str(worker.config.directory/'analysis.sqlite'),
                                  daily_calls=None,allow_technical_entry=True,opener=opener,trial=worker.trial_budget())
        worker.ai_factory=ai
        f.prime(worker)
        worker.local.broker.entry_guard=worker.require_entry
        return worker

    def run_market(self,worker):
        from .marketdata import MarketCollector
        f=self.fixture
        def collect(collector,epic):
            context=f.collect(collector,epic)
            if worker.config.decision_engine=='rules':
                context['timeframes'].pop('HOUR_4')
                for bar in context['timeframes']['HOUR']['candles']:
                    bar.update(o=1.02,c=1.02,h=1.022,l=1.018)
            return context
        with patch.object(MarketCollector,'collect_market',collect):
            worker.opportunities()

    def orders(self):
        return [c for c in self.fixture.transport.calls if c[0]=='POST' and c[1].endswith('/positions')]

    def test_healthy_empty_news_reaches_actual_mock_post_and_confirmation(self):
        worker=self.worker()
        self.run_market(worker)
        self.assertEqual(len(self.orders()),1)
        self.assertIn('entry_confirmed',[e for e,_ in self.fixture.events])
        plan=next(iter(worker.local.journal.owned().values()))
        self.assertEqual(plan['evidence_basis'],'validated_closed_candles')
        self.assertLessEqual(plan['planned_risk'],1.25)
        self.assertEqual(len(self.fixture.ai_calls),1)

    def test_rules_wait_without_hourly_or_daily_trend(self):
        f=self.fixture
        f.env.update(SMART_DECISION_ENGINE='rules',OPENAI_API_KEY='',OPENAI_MODEL='',EODHD_API_KEY='')
        worker=f.make()
        f.prime(worker)
        worker.local.broker.entry_guard=worker.require_entry
        self.assertEqual(worker.config.analysis_mode,'rules')
        self.assertNotIn('news',worker.gate.limits)
        self.run_market(worker)
        self.assertEqual(len(self.orders()),0, f.events[-12:])
        self.assertEqual(f.ai_calls,[])
        self.assertTrue(any(e=='rules_evaluated' and fields['reason']=='no_new_trend_signal'
                            for e,fields in f.events))
        self.run_market(worker)
        self.assertEqual(len(self.orders()),0)

    def test_rules_do_not_trade_without_closed_candle_setup(self):
        f=self.fixture
        f.env['SMART_DECISION_ENGINE']='rules'
        worker=f.make(); f.prime(worker)
        from .marketdata import MarketCollector
        def no_setup(collector,epic):
            context=f.collect(collector,epic)
            context['timeframes'].pop('HOUR_4')
            for bar in context['timeframes']['HOUR']['candles']:
                bar.update(o=1.02,c=1.02,h=1.022,l=1.018)
            return context
        with patch.object(MarketCollector,'collect_market',no_setup):
            worker.opportunities()
        self.assertEqual(self.orders(),[])
        self.assertTrue(any(e=='entry_wait' for e,_ in f.events))

    def _assert_independent_trend_submits(self,route,initial_ask=1.101):
        f=self.fixture
        f.env.update(SMART_DECISION_ENGINE='rules',OPENAI_API_KEY='',OPENAI_MODEL='',EODHD_API_KEY='')
        worker=f.make(); f.prime(worker)
        from .marketdata import MarketCollector
        def bars(seconds,count):
            result=[]
            for i in range(count):
                p=1.06+(i if count==40 else i+10)*.001
                result.append(dict(t=f.now-(count-i)*seconds,o=p,h=p+.0008,
                                   l=p-.0008,c=p+.0004))
            return result
        def collect(collector,epic):
            ctx=f.collect(collector,epic)
            ctx['timeframes'].pop('HOUR_4')
            ctx.update(bid=1.10,ask=initial_ask)
            if route=='HOUR':
                ctx['timeframes']['HOUR']['candles']=bars(3600,40)
            else:
                ctx['timeframes'].pop('HOUR')
            return ctx
        def daily(collector,ctx):
            if route=='DAY':
                ctx['trend_daily']=bars(86400,30)
            return ctx
        with patch.object(MarketCollector,'collect_market',collect),\
             patch.object(MarketCollector,'attach_trend_daily',daily):
            worker.opportunities()
        self.assertEqual(len(self.orders()),1)
        self.assertEqual(f.ai_calls,[])
        order=json.loads(self.orders()[0][2])
        self.assertIn('stopLevel',order)
        self.assertNotIn('profitLevel',order)
        plan=next(iter(worker.local.journal.owned().values()))
        self.assertEqual(plan['trend_route'],route)
        self.assertIsNone(plan['target_level'])
        self.assertEqual(plan['trend_signal_at'],f.now-(3600 if route=='HOUR' else 86400))
        self.assertTrue(any(e=='rules_evaluated' and fields.get('timeframe')==route and fields['ready']
                            for e,fields in f.events))
        self.assertTrue(any(e=='entry_confirmed' for e,_ in f.events))
        return worker

    def test_hour_trend_can_submit_with_no_daily_history(self):
        self._assert_independent_trend_submits('HOUR',initial_ask=1.1001)

    def test_day_trend_can_submit_with_no_hour_history(self):
        self._assert_independent_trend_submits('DAY')

    def test_owned_hour_position_exits_once_on_confirmed_hour_reversal(self):
        from datetime import datetime,timezone
        worker=self._assert_independent_trend_submits('HOUR',initial_ask=1.1001)
        f=self.fixture
        f.now+=3600
        def raw_rows():
            rows=[]
            for i in range(40):
                price=1.2-i*.002
                def side(value): return dict(bid=value,ask=value+.0001)
                rows.append(dict(snapshotTimeUTC=datetime.fromtimestamp(
                    f.now-(40-i)*3600,timezone.utc).isoformat(),
                    openPrice=side(price),highPrice=side(price+.0008),
                    lowPrice=side(price-.0008),closePrice=side(price-.0004)))
            return rows
        broker=worker.local.broker
        quote=dict(bid=1.10,ask=1.1001,quote_time=f.now,quote_currency='USD')
        with patch.object(broker,'market_quote',return_value=quote),\
             patch.object(broker,'prices',return_value=raw_rows()) as prices:
            self.assertTrue(worker.positions())
            self.assertTrue(f.transport.closed)
            self.assertEqual(prices.call_args.args[1],'HOUR')
            worker.positions()
        self.assertEqual(sum(method=='DELETE' for method,_,_ in f.transport.calls),1)
        self.assertTrue(any(e=='closed' and fields['reason']=='confirmed_trend_reversal'
                            for e,fields in f.events))

    def test_trend_feed_failure_keeps_broker_stop_active(self):
        from .capital import CapitalError
        worker=self._assert_independent_trend_submits('HOUR',initial_ask=1.1001)
        f=self.fixture; f.now+=3600
        broker=worker.local.broker
        quote=dict(bid=1.10,ask=1.1001,quote_time=f.now,quote_currency='USD')
        with patch.object(broker,'market_quote',return_value=quote),\
             patch.object(broker,'prices',side_effect=CapitalError('Price history unavailable')):
            self.assertTrue(worker.positions())
            self.assertFalse(f.transport.closed)
            self.assertTrue(any(e=='trend_exit_unavailable' for e,_ in f.events))
            f.transport.stop_missing=True
            self.assertTrue(worker.positions())
        self.assertTrue(f.transport.closed)
        self.assertTrue(any(e=='closed' and fields['reason']=='missing_broker_stop'
                            for e,fields in f.events))

    def test_wait_is_recorded_with_reason_and_does_not_send(self):
        worker=self.worker(action='WAIT')
        self.run_market(worker)
        self.assertEqual(self.orders(),[])
        metrics=worker.trial_budget().summary(self.fixture.now)['metrics']
        self.assertTrue(any(m['event']=='entry_wait' and m['detail']=='analyst_wait' for m in metrics))

    def test_failed_news_feed_does_not_send(self):
        worker=self.worker(healthy=False)
        self.run_market(worker)
        self.assertEqual(self.orders(),[])

    def test_news_failure_at_submission_is_proven_unsent(self):
        worker=self.worker(); broker=worker.local.broker
        original=broker.submit_entry
        def fail(plan):
            worker.gate.failure('news'); return original(plan)
        with patch.object(broker,'submit_entry',fail): self.run_market(worker)
        self.assertEqual(self.orders(),[])
        self.assertFalse(worker.local.journal.unresolved())
        self.assertEqual(worker.local.journal.db.execute('SELECT status FROM smart_entries').fetchone()[0],'blocked')

    def test_preview_and_expired_trial_do_not_send(self):
        worker=self.worker(); worker.config.armed=False
        self.run_market(worker)
        self.assertEqual(self.orders(),[])
        self.assertIn('entry_preview',[e for e,_ in self.fixture.events])
        self.fixture.now+=72*3600
        worker.config.armed=True
        self.run_market(worker)
        self.assertEqual(self.orders(),[])
        self.assertEqual(len(self.fixture.ai_calls),1)

    def test_news_http_diagnostic_is_safe_and_does_not_send(self):
        worker=self.worker()
        def fail(symbol): raise NewsError('EODHD HTTP status 401')
        worker.local.news.fetch=fail
        with self.assertRaises(NewsError): worker.job('news')()
        event=next(fields for name,fields in self.fixture.events if name=='news_failed')
        self.assertEqual(event['reason'],'news_http_error')
        self.assertEqual(event['http_status'],401)
        self.assertEqual(self.orders(),[])

    def test_other_accounts_cannot_select_new_policy_and_arming_still_required(self):
        env=dict(self.fixture.env,BOT_ACCOUNT_ID='other',SMART_EXCLUSIVE_ACCOUNT='other')
        self.assertEqual(Config(env).entry_evidence,NEWS_REQUIRED)
        with self.assertRaises(ValueError): Config(dict(env,SMART_ENTRY_EVIDENCE=TECHNICAL_OR_NEWS))
        with self.assertRaises(ValueError): Config(dict(self.fixture.env,SMART_ARMED=''))
        with self.assertRaises(ValueError): Config(dict(env,SMART_DECISION_ENGINE='rules'))


if __name__=='__main__': unittest.main()
