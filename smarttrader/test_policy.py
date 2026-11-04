import unittest
from smarttrader.policy import analysis_risk, assessed_size, leverage_proposal
from smarttrader.sizing import SizingError

class PolicyTests(unittest.TestCase):
    def proposal(self, **changes):
        values = dict(notional=500,free_margin=1000,available=[1,2,5,10],
                      current=2,maximum=5,class_has_positions=False,
                      class_has_working_orders=False,data_fresh=True)
        values.update(changes)
        return leverage_proposal(**values)
    def test_positive_needs_independent_validation(self):
        self.assertEqual(analysis_risk('positive'), '0.00125')
        self.assertEqual(analysis_risk('positive',True), '0.0025')
        self.assertEqual(analysis_risk('acceptable',True), '0.00125')
    def test_reject_and_bad_gate(self):
        for label,gate in [('reject',False),('unknown',False),('positive','true')]:
            with self.assertRaises(SizingError): analysis_risk(label,gate)
    def test_integrated_size_is_bounded(self):
        data = dict(equity=1000,free_margin=1000,stop_distance=1,spread=0,
                    point_value=1,quote_to_account=1,margin_per_unit=1,min_size=0.01,
                    size_step=0.01,max_size=100,open_risk=0,daily_remaining_risk=250)
        a = assessed_size(assessment='acceptable',**data)
        b = assessed_size(assessment='positive',validation_passed=True,**data)
        self.assertEqual(a['planned_risk'],1.25)
        self.assertEqual(b['planned_risk'],2.5)
    def test_raise_only_as_needed_for_margin(self):
        self.assertEqual(self.proposal()['leverage'],5)
    def test_reduce_for_smaller_notional(self):
        self.assertEqual(self.proposal(notional=50)['leverage'],1)
    def test_occupied_class_never_changes(self):
        for flag in ['class_has_positions','class_has_working_orders']:
            with self.assertRaises(SizingError): self.proposal(**{flag:True})
            r=self.proposal(notional=100,**{flag:True})
            self.assertEqual(r['leverage'],2)
            self.assertFalse(r['change_required'])
    def test_cap_cannot_be_exceeded(self):
        with self.assertRaises(SizingError): self.proposal(maximum=2)
    def test_stale_or_invalid_data_blocks(self):
        for values in [dict(data_fresh=False),dict(current=3),dict(available=[]),dict(maximum=0)]:
            with self.assertRaises(SizingError): self.proposal(**values)

if __name__ == '__main__': unittest.main()
