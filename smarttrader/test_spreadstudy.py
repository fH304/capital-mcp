import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .spreadstudy import (SpreadStudy,candidate_costs,paper_outcome,historical_cost_study,
                          BAR_SECONDS,HORIZON_BARS,RETENTION_SECONDS)
from .universe import rank,spread_metrics
from .test_analysis import NOW,recommendation
from . import test_continuous
from .test_marketdata import rows
from .test_universe import article,trending


def bar(stamp,*,o=100,h=102,l=99,c=100,spread=1):
    return dict(t=stamp,bid=dict(o=o,h=h,l=l,c=c),
                ask=dict(o=o+spread,h=h+spread,l=l+spread,c=c+spread))


def plan(action='BUY'):
    return dict(action=action,stop_level=97 if action=='BUY' else 104,
                target_level=113 if action=='BUY' else 88,paper_start=NOW+BAR_SECONDS)


class CostTests(unittest.TestCase):
    def test_both_entry_sides_count_spread_once(self):
        context=dict(bid=100,ask=101,quote_currency='JPY')
        for action,stop,target in (('BUY',97,113),('SELL',104,88)):
            costs=candidate_costs(context,dict(action=action,stop_level=stop,target_level=target))
            self.assertEqual(costs['risk_distance'],4)
            self.assertEqual(costs['reward_distance'],12)
            self.assertEqual(costs['reward_risk'],3)
            self.assertEqual(costs['spread_to_risk'],.25)
            self.assertEqual(costs['quote_currency'],'JPY')
        with self.assertRaises(ValueError):
            candidate_costs(context,dict(action='BUY',stop_level=102,target_level=113))

    def test_zero_atr_is_not_serialized_as_infinity(self):
        ctx=trending()
        for candle in ctx['timeframes']['MINUTE_15']['candles']:
            candle.update(o=1,h=1,l=1,c=1)
        metrics=rank(ctx)
        self.assertEqual(metrics['score'],0)
        self.assertEqual(metrics['spread_gate_reason'],'zero_atr')
        self.assertIsNone(metrics['spread_to_atr'])
        json.dumps(metrics,allow_nan=False)

    def test_existing_spread_boundary_and_trend_gate_remain(self):
        ctx=trending()
        for frame in ctx['timeframes'].values():
            for index,candle in enumerate(frame['candles']):
                close=2+index/32
                candle.update(o=close,c=close,h=close+1.25,l=close-1.25)
        ctx.update(bid=3,ask=3.5)
        self.assertEqual(rank(ctx)['spread_to_atr'],.2)
        self.assertGreater(rank(ctx)['score'],0)
        ctx['ask']=3.5001
        self.assertEqual(rank(ctx)['score'],0)
        self.assertEqual(rank(ctx)['spread_gate_reason'],'spread_above_limit')
        ctx['ask']=3.5
        ctx['timeframes']['HOUR']['candles'][-1]['c']=1
        self.assertEqual(rank(ctx)['reason'],'timeframes_disagree')


class PaperTests(unittest.TestCase):
    def test_full_future_bar_only_and_both_levels_are_ambiguous(self):
        p=plan()
        old=bar(NOW,h=120,l=90)
        self.assertEqual(paper_outcome(p,[old],NOW+BAR_SECONDS)['status'],'pending')
        both=bar(p['paper_start'],h=115,l=95)
        result=paper_outcome(p,[old,both],p['paper_start']+BAR_SECONDS)
        self.assertEqual(result['status'],'ambiguous')
        self.assertNotIn('price_R',result)

    def test_buy_target_uses_bid_and_sell_target_uses_ask(self):
        p=plan()
        # Ask touches the BUY target but bid does not; it must remain pending.
        self.assertEqual(paper_outcome(p,[bar(p['paper_start'],h=112.5)],
                         p['paper_start']+BAR_SECONDS)['status'],'pending')
        result=paper_outcome(p,[bar(p['paper_start'],h=113)],p['paper_start']+BAR_SECONDS)
        self.assertEqual(result['price_R'],3)
        p=plan('SELL')
        result=paper_outcome(p,[bar(p['paper_start'],l=87)],p['paper_start']+BAR_SECONDS)
        self.assertEqual(result['price_R'],3)

    def test_missing_bar_is_censored_and_gap_loss_exceeds_one_R(self):
        p=plan()
        first=bar(p['paper_start'])
        gap=bar(p['paper_start']+2*BAR_SECONDS,o=95,h=96,l=94,c=95)
        self.assertEqual(paper_outcome(p,[first,gap],gap['t']+BAR_SECONDS)['status'],'missing_bars')
        gap['t']=p['paper_start']+BAR_SECONDS
        result=paper_outcome(p,[first,gap],gap['t']+BAR_SECONDS)
        self.assertEqual(result['status'],'stop')
        self.assertEqual(result['price_R'],-1.5)

    def test_next_open_price_must_still_allow_original_protection(self):
        p=plan()
        result=paper_outcome(p,[bar(p['paper_start'],o=114,h=115,l=113,c=114)],
                             p['paper_start']+BAR_SECONDS)
        self.assertEqual(result['status'],'invalid_next_open')
        self.assertNotIn('price_R',result)

    def test_horizon_uses_executable_close_without_subtracting_spread_again(self):
        p=plan()
        bars=[bar(p['paper_start']+i*BAR_SECONDS,c=102) for i in range(HORIZON_BARS)]
        result=paper_outcome(p,bars,bars[-1]['t']+BAR_SECONDS)
        self.assertEqual(result['status'],'horizon')
        self.assertEqual(result['price_R'],.25)


class PersistenceTests(unittest.TestCase):
    def test_restart_dedup_and_read_only_report_do_not_rewrite_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'spread.sqlite'
            ctx=trending()
            ctx.update(bid=1.10,ask=1.1001)
            scoring=rank(ctx)
            study=SpreadStudy(path,'demo-1')
            study.observe(ctx,scoring,NOW)
            study.observe(ctx,scoring,NOW+1)
            self.assertTrue(study.candidate(ctx,scoring,recommendation(),NOW)['recorded'])
            study.close()
            study=SpreadStudy(path,'demo-1')
            self.assertFalse(study.candidate(ctx,scoring,recommendation(),NOW+2)['recorded'])
            study.close()
            before=path.read_bytes()
            report=SpreadStudy.report(path,'demo-1',NOW+3)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(report['markets']['EURUSD']['observations'],1)
            self.assertEqual(report['markets']['EURUSD']['paper_candidates'],1)
            self.assertFalse(report['threshold_changed'])
            self.assertEqual(len(report['markets']),21)
            with self.assertRaises(sqlite3.OperationalError):
                SpreadStudy.report(Path(folder)/'absent.sqlite','demo-1',NOW)
            self.assertFalse((Path(folder)/'absent.sqlite').exists())

    def test_future_results_update_paper_only_and_expired_observations_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'spread.sqlite'
            ctx=trending()
            ctx.update(bid=100,ask=101)
            rec=dict(action='BUY',stop_level=97,target_level=113)
            scoring=rank(ctx)
            study=SpreadStudy(path,'demo-1')
            costs=study.candidate(ctx,scoring,rec,NOW)
            future=bar(costs['paper_start'],h=113)
            study.observe(ctx,scoring,future['t']+BAR_SECONDS,[future])
            stored=json.loads(study.db.execute('SELECT outcome FROM spread_candidates').fetchone()[0])
            self.assertEqual(stored['status'],'target')
            study.observe(ctx,scoring,NOW+RETENTION_SECONDS+1)
            self.assertEqual(study.db.execute('SELECT count(*) FROM spread_candidates').fetchone()[0],0)
            study.close()

    def test_historical_comparison_uses_closed_bars_and_does_not_claim_profitability(self):
        feed=rows('MINUTE_15')
        report=historical_cost_study({'EURUSD':dict(prices=feed)},NOW)
        market=report['markets']['EURUSD']
        self.assertEqual(market['closed_bars'],39)
        self.assertEqual(market['observations'],25)
        self.assertTrue(all(v['paper_completed']==0 and v['mean_price_R'] is None
                            for v in market['comparison']))
        self.assertFalse(report['threshold_changed'])
        duplicate=copy.deepcopy(feed)
        duplicate.append(duplicate[-2])
        result=historical_cost_study({'EURUSD':dict(prices=duplicate)},NOW)
        self.assertIn('error_type',result['markets']['EURUSD'])


class WorkerStudyTests(unittest.TestCase):
    def fixture(self):
        obj=test_continuous.ContinuousTests(methodName='test_preview_entry_runs_once_while_analysis_continues_all_21')
        obj.setUp()
        self.addCleanup(obj.tearDown)
        return obj

    def test_rejected_valid_candidate_has_costs_and_no_order(self):
        fixture=self.fixture()
        fixture.buy={'EURUSD'}
        worker=fixture.make()
        ctx=fixture.collect(None,'EURUSD')
        ctx['ask']=ctx['bid']+.001
        # Still a valid recommendation, but current spread/ATR gate rejects it.
        scoring=rank(ctx)
        self.assertEqual(scoring['score'],0)
        worker.monitor_market('EURUSD',ctx,scoring,[article('Euro',['EURUSD.FOREX'])])
        costs=[f for event,f in fixture.events if event=='entry_candidate_costs']
        self.assertEqual(len(costs),1)
        self.assertFalse(costs[0]['technical_passed'])
        block=[f for event,f in fixture.events if event=='entry_blocked'][0]
        self.assertEqual(block['spread_gate_reason'],'spread_above_limit')
        self.assertFalse(any(m=='POST' and u.endswith('/positions') for m,u,_ in fixture.transport.calls))

    def test_study_disk_failure_does_not_change_existing_entry_behavior(self):
        fixture=self.fixture()
        fixture.buy={'EURUSD'}
        worker=fixture.make()
        fixture.prime(worker)
        with patch.object(worker,'spread_study',side_effect=sqlite3.OperationalError('private')):
            fixture.sweep(worker)
        self.assertIn('entry_preview',[e for e,_ in fixture.events])
        self.assertIn('spread_study_failed',[e for e,_ in fixture.events])
        self.assertNotIn('private',json.dumps(fixture.events))
        self.assertEqual(len(fixture.contexts),21)


if __name__=='__main__':
    unittest.main()
