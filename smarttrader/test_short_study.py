import tempfile
import unittest

from .short_study import short_setup
from .strategy_study import StrategyStudy
from .spreadstudy import paper_outcome, BAR_SECONDS
from .test_analysis import NOW
from .test_universe import trending


def reversal():
    context=trending()
    bars=context['timeframes']['MINUTE_15']['candles']
    bars[-2].update(o=1.031,c=1.030,h=1.032,l=1.029)
    bars[-1].update(o=1.030,c=1.033,h=1.034,l=1.030)
    context.update(bid=1.033,ask=1.0331)
    return context


class ShortStudyTests(unittest.TestCase):
    def test_observed_reversal_is_isolated_from_execution(self):
        context=reversal()
        setup=short_setup(context,NOW)
        self.assertTrue(setup['ready'],setup)
        self.assertFalse(setup['execution_authorized'])
        self.assertEqual(setup['min_reward_risk'],1.5)
        self.assertGreater(setup['spread_to_reward'],0)
        self.assertLessEqual(setup['target_level']-setup['entry_price'],3*setup['atr'])
        with tempfile.TemporaryDirectory() as folder:
            study=StrategyStudy(folder+'/forward.sqlite','fixture')
            results=study.observe(context,NOW)
            self.assertEqual(len(results),3)
            self.assertTrue(results[-1]['ready'])
            self.assertFalse(results[-1]['execution_authorized'])
            self.assertFalse(study.observe(context,NOW)[-1]['recorded'])
            report=study.report(folder+'/forward.sqlite','fixture',NOW)
            self.assertEqual(report['markets']['EURUSD']['short_reversal_paper_v1']['candidates'],1)
            study.close()

    def test_no_reversal_and_conflicting_higher_frame_wait(self):
        context=reversal()
        context['timeframes']['MINUTE_15']['candles'][-1]['c']=1.031
        self.assertFalse(short_setup(context,NOW)['ready'])
        context=reversal()
        higher=context['timeframes']['HOUR_4']['candles']
        for bar in higher:
            bar.update(o=2-bar['o'],c=2-bar['c'],h=2-bar['l'],l=2-bar['h'])
        self.assertEqual(short_setup(context,NOW)['reason'],'short_higher_frame_conflict')

    def test_short_paper_threshold_does_not_change_existing_two_r_studies(self):
        start=NOW+BAR_SECONDS
        future={'t':start,'bid':dict(o=100,h=101.9,l=99.9,c=101),
                'ask':dict(o=100.1,h=102,l=100,c=101.1)}
        short=dict(action='BUY',paper_start=start,stop_level=99,target_level=101.8,
                   min_reward_risk=1.5)
        regular={k:v for k,v in short.items() if k!='min_reward_risk'}
        self.assertEqual(paper_outcome(short,[future],start+BAR_SECONDS)['status'],'target')
        self.assertEqual(paper_outcome(regular,[future],start+BAR_SECONDS)['status'],'invalid_next_open')
