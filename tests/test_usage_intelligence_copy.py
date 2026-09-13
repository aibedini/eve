"""Customer-facing explanation copy (RFP sections 26-27).

The lines the customer reads must quote the real numbers, exist in both languages, and never
carry an identifier.
"""
import re
import unittest

from panel.services.usage_intelligence.copy import build_explanation, explanation_state


class ExplanationCopyTests(unittest.TestCase):
    def test_a_strong_increase_quotes_both_rates_and_the_reason(self):
        result = build_explanation(trend_state='strong_increase', cycle_daily_gb=3.75,
                                   rolling_daily_gb=1.94, change_percent=93.0,
                                   forecast_gb=105.0, data_confidence='medium',
                                   maturity='medium')
        self.assertEqual(result['state'], 'strong_increase')
        for text, marker in ((result['fa'], 'افزایش قابل‌توجهی'),
                             (result['en'], 'increased significantly')):
            self.assertIn('3.75', text)
            self.assertIn('1.94', text)
            self.assertIn(marker, text)
        # The RFP's sentence ends by explaining the weighting decision.
        self.assertIn('وزن', result['fa'])
        self.assertIn('more weight', result['en'])

    def test_a_stable_cycle_uses_the_stable_sentence(self):
        result = build_explanation(trend_state='stable', cycle_daily_gb=1.05,
                                   rolling_daily_gb=1.0, maturity='mature')
        self.assertEqual(result['state'], 'stable')
        self.assertIn('تقریباً ثابت است', result['fa'])
        self.assertIn('in line with the past month', result['en'])

    def test_insufficient_evidence_is_stated_plainly(self):
        result = build_explanation(trend_state='stable', cycle_daily_gb=5.0,
                                   rolling_daily_gb=1.0, data_confidence='early',
                                   maturity='very_early')
        self.assertEqual(result['state'], 'insufficient_evidence')
        self.assertIn('داده کافی جمع نشده', result['fa'])
        self.assertIn('not enough evidence', result['en'])

    def test_no_cycle_at_all_is_insufficient_evidence(self):
        result = build_explanation(trend_state='unknown', cycle_available=False)
        self.assertEqual(result['state'], 'insufficient_evidence')

    def test_an_unknown_trend_is_not_dressed_up_as_a_verdict(self):
        self.assertEqual(
            explanation_state(trend_state='unknown', data_confidence='high',
                              cycle_available=True, maturity='mature'),
            'insufficient_evidence')

    def test_increasing_and_decreasing_states_exist_in_both_languages(self):
        for state in ('increasing', 'decreasing', 'strong_decrease',
                      'strong_increase', 'stable'):
            result = build_explanation(trend_state=state, cycle_daily_gb=2.0,
                                       rolling_daily_gb=1.0, maturity='mature')
            self.assertEqual(result['state'], state)
            self.assertTrue(result['fa'].strip())
            self.assertTrue(result['en'].strip())
            self.assertNotEqual(result['fa'], result['en'])

    def test_early_exhaustion_adds_its_own_clause(self):
        result = build_explanation(trend_state='strong_increase', cycle_daily_gb=6.2,
                                   rolling_daily_gb=1.9, maturity='medium',
                                   early_exhaustion=True, expected_duration_days=31.0)
        self.assertIn('31', result['fa'])
        self.assertIn('31', result['en'])
        self.assertIn('تمام شده است', result['fa'])
        self.assertIn('ran out', result['en'])

    def test_the_copy_never_carries_an_identifier(self):
        result = build_explanation(trend_state='strong_increase', cycle_daily_gb=3.75,
                                   rolling_daily_gb=1.94, maturity='medium',
                                   early_exhaustion=True, expected_duration_days=31)
        for text in (result['fa'], result['en']):
            self.assertNotIn('@', text)
            self.assertNotIn('http', text)
            self.assertIsNone(re.search(r'\b09\d{9}\b', text))

    def test_a_broken_number_does_not_break_the_sentence(self):
        result = build_explanation(trend_state='strong_increase', cycle_daily_gb=None,
                                   rolling_daily_gb='oops', maturity='medium')
        self.assertIn('0.00', result['fa'])
        self.assertTrue(result['en'])


if __name__ == '__main__':
    unittest.main()
