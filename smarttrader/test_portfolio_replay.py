import unittest
from unittest.mock import patch

from .portfolio_replay import confirmation, exit_on_bar, permitted_entry, replay


def row(price=100):
    return dict(openPrice=dict(bid=price-.1,ask=price+.1),
                closePrice=dict(bid=price-.1,ask=price+.1),
                highPrice=dict(bid=price+.4,ask=price+.6),
                lowPrice=dict(bid=price-.4,ask=price-.2))


def signal():
    return dict(ready=True,direction='BUY',stop_level=95,target_level=120,
                atr=1,strategy='range_rejection_v1',reason='range_edge_rejection',
                signal_close=100)


class PortfolioReplayTests(unittest.TestCase):
    def test_confirmation_uses_closed_mid_and_rejects_touched_protection(self):
        confirm=row(100.15)
        self.assertEqual(confirmation(signal(),confirm),(True,None))
        confirm['lowPrice']['bid']=94
        self.assertEqual(confirmation(signal(),confirm)[1],'confirmation_levels_touched')
        self.assertEqual(confirmation(signal(),row(99.9))[1],'confirmation_not_continuing')

    def test_margin_and_risk_are_both_bounded_before_entry(self):
        spec=dict(margin=.1,min_size=.1,size_step=.1)
        plan,reason=permitted_entry(signal(),row(100)['openPrice'],1000,spec,0)
        self.assertIsNone(reason)
        self.assertLessEqual(plan['size']*plan['entry']*spec['margin'],100+1e-9)
        self.assertLessEqual(plan['expected_risk'],1.25)
        plan,reason=permitted_entry(signal(),row(100)['openPrice'],1000,
                                    dict(spec,min_size=1),0)
        self.assertIsNone(plan)
        self.assertEqual(reason,'size_below_broker_minimum')

    def test_same_bar_target_and_stop_resolve_at_stop(self):
        position=dict(direction='BUY',entry=100,size=1,stop=95,target=105)
        candle=row(100)
        candle['lowPrice']['bid']=94
        candle['highPrice']['bid']=106
        result=exit_on_bar(position,candle,0)
        self.assertEqual(result['exit_reason'],'stop')
        self.assertEqual(result['gross_pnl'],-5)

    def test_shared_account_one_pending_and_extra_closed_confirmation(self):
        times=(0,900,1800,2700)
        feed={t:row(100 if t<900 else 101) for t in times}
        data={e:{'MINUTE_15':dict(rows=feed,times=list(times))} for e in ('GOLD','SILVER')}
        context=lambda data,epic,stamp:dict(epic=epic,bid=100,ask=100.2,
            timeframes={'MINUTE_15':{'candles':[{'c':100}]}})
        with patch('smarttrader.portfolio_replay.history_context',side_effect=context),\
             patch('smarttrader.portfolio_replay.technical_evidence',return_value=signal()):
            baseline=replay(data,start=0,end=3600)
            confirmed=replay(data,start=0,end=3600,variant='range_confirm')
        self.assertEqual(baseline['open_position']['epic'],'GOLD')
        self.assertEqual(baseline['open_position']['opened_at'],900)
        self.assertEqual(confirmed['open_position']['opened_at'],1800)
        self.assertEqual(confirmed['reasons']['hypothetical_entry'],1)


if __name__=='__main__':unittest.main()
