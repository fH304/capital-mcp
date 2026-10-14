import copy
import tempfile
import unittest
from .test_universe import trending
from .test_analysis import NOW, context, recommendation
from .strategy import trend_setup, range_setup, accepts_levels
from .strategy_study import StrategyStudy, metrics
from .analysis import validate_recommendation, AnalysisError


def setup_context(epic='EURUSD', entry=1.04):
    ctx=trending(epic)
    shift=entry-.001-ctx['timeframes']['HOUR']['candles'][-1]['c']
    for f in ctx['timeframes'].values():
        for b in f['candles']:
            for k in ('o','h','l','c'): b[k]+=shift
    for b in ctx['timeframes']['HOUR']['candles']:
        b['h']=b['c']+.0002; b['l']=b['c']-.0002
    ctx['timeframes']['HOUR_4']['candles'][-1]['h']=entry+.04
    bars=ctx['timeframes']['MINUTE_15']['candles']
    bars[-3].update(o=entry-.0015,c=entry-.001,h=entry-.0005,l=entry-.002)
    bars[-2].update(o=entry-.001,c=entry-.002,h=entry-.0005,l=entry-.003)
    bars[-1].update(o=entry-.002,c=entry,h=entry+.0005,l=entry-.003)
    return dict(ctx,bid=entry,ask=entry+.0001,articles=[],news_feed_healthy=True,news_checked_at=NOW)


class StructureTests(unittest.TestCase):
    def test_pullback_stop_and_observed_target(self):
        c=setup_context(); s=trend_setup(c,NOW)
        self.assertTrue(s['ready'],s); self.assertEqual(s['reason'],'trend_pullback')
        self.assertLess(s['stop_level'],min(b['l'] for b in c['timeframes']['MINUTE_15']['candles'][-5:]))
        self.assertEqual(s['target_level'],1.08)
        self.assertTrue(accepts_levels(s,'BUY',1.03,1.07,c['bid'],c['ask']))
        self.assertFalse(accepts_levels(s,'BUY',1.039,1.07,c['bid'],c['ask']))
        self.assertFalse(accepts_levels(s,'BUY',1.03,1.09,c['bid'],c['ask']))

    def test_no_pattern_no_chasing_and_unclosed_bars(self):
        self.assertFalse(trend_setup(trending(),NOW)['ready'])
        c=setup_context(); c.update(bid=1.06,ask=1.0601)
        self.assertEqual(trend_setup(c,NOW)['reason'],'price_extended_no_chasing')
        c=setup_context(); c['timeframes']['MINUTE_15']['candles'][-1]['t']=NOW
        with self.assertRaises(ValueError): trend_setup(c,NOW)

    def test_news_does_not_bypass_setup_or_structure_levels(self):
        for c,stop,target in [(trending(),1.03,1.07),(setup_context(),1.039,1.07),(setup_context(),1.03,1.09)]:
            c['articles']=context()['articles']
            rec=dict(recommendation(),stop_level=stop,target_level=target)
            with self.subTest(stop=stop,target=target),self.assertRaises(AnalysisError):
                validate_recommendation(rec,c,NOW,allow_technical_entry=True)

    def test_mirrored_sell_is_symmetric(self):
        c=setup_context()
        for f in c['timeframes'].values():
            for b in f['candles']: b.update(o=2-b['o'],c=2-b['c'],h=2-b['l'],l=2-b['h'])
        c.update(bid=2-c['ask'],ask=2-c['bid'])
        s=trend_setup(c,NOW)
        self.assertTrue(s['ready'],s); self.assertEqual(s['direction'],'SELL')
        self.assertTrue(accepts_levels(s,'SELL',.97,.93,c['bid'],c['ask']))

    def test_retest_uses_prior_level_and_rejects_bare_breakout(self):
        c=setup_context(); bars=c['timeframes']['MINUTE_15']['candles']
        for b in bars[-22:-2]:
            b['h']=min(b['h'],1.038); b['o']=min(b['o'],b['h']); b['c']=min(b['c'],b['h'])
        bars[-2].update(o=1.037,c=1.039,h=1.0395,l=1.037)
        bars[-1].update(o=1.039,c=1.04,h=1.0405,l=1.038)
        s=trend_setup(c,NOW)
        self.assertTrue(s['ready'],s);self.assertEqual(s['reason'],'breakout_retest')
        self.assertEqual(s['reference_level'],1.038)

    def test_range_strategy_remains_separate(self):
        c=setup_context()
        for f in c['timeframes'].values():
            for b in f['candles']: b.update(o=1.04,c=1.04,h=1.05,l=1.03)
        c['timeframes']['MINUTE_15']['candles'][-1].update(o=1.031,c=1.032,h=1.033,l=1.029)
        c.update(bid=1.032,ask=1.0321)
        s=range_setup(c,NOW)
        self.assertTrue(s['ready'],s);self.assertFalse(s['execution_authorized'])
        self.assertFalse(trend_setup(c,NOW)['ready'])

    def test_supported_target_cannot_be_stretched_to_create_two_r(self):
        c=setup_context()
        c['timeframes']['HOUR_4']['candles'][-1]['h']=1.041
        self.assertFalse(trend_setup(c,NOW)['ready'])
        c=setup_context(); s=trend_setup(c,NOW)
        self.assertFalse(accepts_levels(s,'BUY',1.03,1.061,1.04,1.0401))
        self.assertFalse(accepts_levels(s,'BUY',1.03,1.07,1.049,1.0491))

    def test_future_observations_do_not_score_on_signal_bars_or_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            study=StrategyStudy(d+'/s.sqlite','fixture'); c=setup_context()
            study.observe(c,NOW)
            start=study.db.execute('SELECT json_extract(payload,"$.paper_start") FROM setup_forward '
                                  'WHERE version="structure_trend_v1"').fetchone()[0]
            self.assertGreater(start,NOW)
            # A new qualifying closed candle within the fixed horizon is recorded as WAIT.
            c=copy.deepcopy(c)
            for f in c['timeframes'].values():
                for b in f['candles']: b['t']+=900
            c.update(quote_time=NOW+900,news_checked_at=NOW+900)
            result=study.observe(c,NOW+900)
            self.assertEqual(result[0]['reason'],'paper_horizon_overlap')
            self.assertEqual(study.report(d+'/s.sqlite','fixture',NOW+900)['markets']['EURUSD']
                             ['structure_trend_v1']['all_forward']['completed'],0)
            study.close()

    def test_metrics_include_losers_and_slippage_without_double_spread(self):
        p=dict(stop_level=1.,slippage_stress_distance=.01)
        rows=[(p,dict(status='target',paper_entry=1.1,price_R=2.)),
              (p,dict(status='stop',paper_entry=1.1,price_R=-1.)),
              (p,dict(status='ambiguous'))]
        m=metrics(rows)
        self.assertAlmostEqual(m['mean_stressed_R'],.4)
        self.assertAlmostEqual(m['max_closed_trade_drawdown_R'],1.1)
        self.assertEqual(m['outcome_counts']['ambiguous'],1)
        self.assertFalse(m['profitability_validated'])

    def test_forward_is_idempotent_read_only_and_never_promotes(self):
        with tempfile.TemporaryDirectory() as d:
            study=StrategyStudy(d+'/s.sqlite','fixture')
            c=setup_context()
            self.assertTrue(study.observe(c,NOW)[0]['recorded'])
            self.assertFalse(study.observe(c,NOW)[0]['recorded'])
            r=study.report(d+'/s.sqlite','fixture',NOW)
            self.assertFalse(r['profitability_validated'])
            self.assertFalse(r['markets']['EURUSD']['structure_trend_v1']['comparison_ready'])
            self.assertEqual(study.db.execute('SELECT count(*) FROM setup_forward').fetchone()[0],2)
            study.close()


if __name__=='__main__':unittest.main()
