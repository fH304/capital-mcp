import threading
import unittest
from dataclasses import replace
from smarttrader.monitor import PositionView, exit_decision, EntryGate, Monitor

class MonitorTests(unittest.TestCase):
    def position(self, **changes):
        p=PositionView('owned-deal',True,'BUY',90,100,101,1000,True,True)
        return replace(p,**changes)
    def test_owned_only(self):
        self.assertEqual(exit_decision(self.position(owned=False),1000,True)['action'],'alert')
    def test_stop_uses_executable_side(self):
        self.assertEqual(exit_decision(self.position(bid=90,ask=91),1000)['reason'],'stop_breached')
        self.assertEqual(exit_decision(self.position(direction='SELL',stop_level=101),1000)['reason'],'stop_breached')
    def test_daily_loss_exit_and_missing_protection(self):
        self.assertEqual(exit_decision(self.position(),1000,True)['reason'],'daily_loss_limit')
        self.assertEqual(exit_decision(self.position(protected=False),1000)['reason'],'missing_broker_stop')
    def test_bad_quotes_do_not_create_exit(self):
        for change in [dict(quote_time=900),dict(bid=float('nan')),dict(tradeable=False),dict(quote_time=1001)]:
            self.assertEqual(exit_decision(self.position(**change),1000,True)['action'],'alert')
    def test_model_claim_is_insufficient(self):
        p=self.position(adverse_news_confirmed=True,evidence_time=1000)
        self.assertEqual(exit_decision(p,1000)['action'],'hold')
        self.assertEqual(exit_decision(replace(p,evidence_validated=True),1000)['reason'],'validated_adverse_news')
        self.assertEqual(exit_decision(replace(p,evidence_validated=True,evidence_time=600),1000)['action'],'hold')
    def test_entry_gate_fails_closed(self):
        gate=EntryGate()
        self.assertFalse(gate.may_enter(1000))
        for name in gate.limits: gate.success(name,1000)
        self.assertTrue(gate.may_enter(1000))
        self.assertFalse(gate.may_enter(1031))
        gate.failure('news');self.assertFalse(gate.may_enter(1000))
        gate.success('news',1000);gate.stopped(True)
        self.assertFalse(gate.may_enter(1000))
    def test_slow_news_cannot_block_position_updates(self):
        entered=threading.Event();release=threading.Event();updated=threading.Event()
        def news():
            entered.set();release.wait(2);return False
        monitor=Monitor(lambda: updated.set() or True, lambda:True,news,lambda *a,**k:None,
                        intervals={'positions':0.01,'opportunities':0.01,'news':1})
        monitor.start()
        try:
            self.assertTrue(entered.wait(1))
            updated.clear()
            self.assertTrue(updated.wait(0.5))
            self.assertFalse(monitor.gate.may_enter(1000))
        finally:
            release.set();monitor.stop()
    def test_feed_errors_redact_secrets_and_do_not_stop_exit_monitor(self):
        rows=[];updated=threading.Event()
        def failing(): raise RuntimeError('secret-token')
        monitor=Monitor(lambda:updated.set() or True,lambda:True,failing,
                        lambda event,**kw:rows.append((event,kw)),
                        intervals={'positions':0.01,'opportunities':0.01,'news':0.01})
        monitor.start()
        try: self.assertTrue(updated.wait(1))
        finally: monitor.stop()
        self.assertNotIn('secret-token',str(rows))
        self.assertFalse(monitor.gate.may_enter(1000))

if __name__ == '__main__': unittest.main()
