"""Two-axis confidence (RFP section 19 and tests 45.9, 45.15).

Plenty of data about behaviour that just changed is a supported answer - the model must be
able to say "I have the evidence, and the customer's pattern moved" instead of hiding it
behind one averaged confidence label.
"""
import unittest

from panel.services.usage_intelligence import confidence as confidence_module
from panel.services.usage_intelligence import trend as trend_module
from panel.services.usage_intelligence.schemas import (
    CycleMetrics, Signals, WindowMetrics,
)


def _cycle(rate, *, maturity='medium', elapsed=8.0, available=True):
    return CycleMetrics(available=available, average_daily_gb=rate, maturity=maturity,
                        elapsed_days=elapsed, effective_elapsed_days=elapsed)


def _rolling(rate, *, basis=31.0, dates=20, samples=40, available=True):
    return WindowMetrics(available=available, average_daily_gb=rate, basis_days=basis,
                         observed_dates=dates, samples=samples)


class DataConfidenceTests(unittest.TestCase):
    def test_a_long_observed_cycle_is_high_confidence(self):
        label, reasons = confidence_module.assess_data_confidence(
            _cycle(3.0, maturity='mature', elapsed=20.0), _rolling(2.0))
        self.assertEqual(label, 'high')
        self.assertEqual(reasons, ())

    def test_a_young_cycle_is_early_confidence_with_a_reason(self):
        label, reasons = confidence_module.assess_data_confidence(
            _cycle(3.0, maturity='very_early', elapsed=0.5), _rolling(2.0, dates=2))
        self.assertEqual(label, 'early')
        self.assertIn('cycle_too_young', reasons)

    def test_stale_telemetry_downgrades_the_data_confidence(self):
        fresh, _ = confidence_module.assess_data_confidence(
            _cycle(3.0, maturity='mature', elapsed=20.0), _rolling(2.0))
        stale, reasons = confidence_module.assess_data_confidence(
            _cycle(3.0, maturity='mature', elapsed=20.0), _rolling(2.0),
            freshness='stale')
        self.assertEqual(fresh, 'high')
        self.assertEqual(stale, 'medium')
        self.assertIn('stale_telemetry', reasons)

    def test_an_unknown_telemetry_timestamp_also_downgrades(self):
        label, reasons = confidence_module.assess_data_confidence(
            _cycle(3.0, maturity='mature', elapsed=20.0), _rolling(2.0),
            freshness='unknown')
        self.assertEqual(label, 'medium')
        self.assertIn('telemetry_timestamp_unknown', reasons)

    def test_without_a_cycle_the_data_can_never_be_high(self):
        label, reasons = confidence_module.assess_data_confidence(
            _cycle(0.0, available=False), _rolling(1.6))
        self.assertIn('no_verified_cycle', reasons)
        self.assertIn(label, ('medium', 'early'))


class BehaviorStabilityTests(unittest.TestCase):
    def test_the_reported_bug_is_high_data_and_low_stability(self):
        """RFP section 19's example: enough evidence, but the customer changed."""
        cycle = _cycle(3.75, maturity='medium', elapsed=8.0)
        rolling = _rolling(1.94)
        baseline = WindowMetrics(available=True, average_daily_gb=1.55, basis_days=31.0)
        trend = trend_module.detect_trend(cycle, rolling)
        self.assertEqual(trend.state, 'strong_increase')

        result = confidence_module.assess_confidence(
            cycle, rolling, baseline=baseline, trend=trend,
            daily_series=[3.0, 4.0, 3.5, 4.2, 3.1, 4.4, 3.6, 3.9])
        self.assertEqual(result.data, 'medium')
        self.assertEqual(result.behavior_stability, 'low')
        self.assertIn('strong_increase', result.reasons)

    def test_a_steady_customer_is_high_stability(self):
        cycle = _cycle(1.05, maturity='mature', elapsed=20.0)
        rolling = _rolling(1.0)
        baseline = WindowMetrics(available=True, average_daily_gb=1.0, basis_days=31.0)
        trend = trend_module.detect_trend(cycle, rolling)
        result = confidence_module.assess_confidence(
            cycle, rolling, baseline=baseline, trend=trend,
            daily_series=[1.0, 1.1, 0.9, 1.0, 1.0, 0.95, 1.05, 1.0])
        self.assertEqual(result.behavior_stability, 'high')
        self.assertEqual(result.data, 'high')

    def test_early_exhaustion_makes_the_behaviour_unstable(self):
        cycle = _cycle(3.0, maturity='medium')
        rolling = _rolling(2.0)
        trend = trend_module.detect_trend(cycle, rolling)
        signals = Signals(early_exhaustion=True, exhaustion_severity='critical',
                          exhaustion_ratio=0.26)
        label, reasons = confidence_module.assess_behavior_stability(
            cycle, rolling, trend=trend, signals=signals)
        self.assertEqual(label, 'low')
        self.assertIn('early_exhaustion', reasons)

    def test_high_daily_variance_is_low_stability(self):
        cycle = _cycle(1.0, maturity='mature', elapsed=20.0)
        rolling = _rolling(1.0)
        trend = trend_module.detect_trend(cycle, rolling)
        label, reasons = confidence_module.assess_behavior_stability(
            cycle, rolling, trend=trend,
            daily_series=[0.1, 5.0, 0.2, 6.0, 0.1, 4.5])
        self.assertEqual(label, 'low')
        self.assertIn('high_daily_variance', reasons)

    def test_a_stable_cycle_that_does_not_match_the_baseline_is_only_medium(self):
        cycle = _cycle(1.10, maturity='mature', elapsed=20.0)
        rolling = _rolling(1.05)
        baseline = WindowMetrics(available=True, average_daily_gb=0.6, basis_days=31.0)
        trend = trend_module.detect_trend(cycle, rolling)
        label, reasons = confidence_module.assess_behavior_stability(
            cycle, rolling, baseline=baseline, trend=trend,
            daily_series=[1.1, 1.05, 1.15, 1.0, 1.1])
        self.assertEqual(label, 'medium')
        self.assertIn('baseline_drift', reasons)

    def test_a_missing_baseline_is_reported_rather_than_assumed(self):
        cycle = _cycle(1.0, maturity='mature', elapsed=20.0)
        rolling = _rolling(1.0)
        trend = trend_module.detect_trend(cycle, rolling)
        label, reasons = confidence_module.assess_behavior_stability(
            cycle, rolling, trend=trend, daily_series=[1.0, 1.0, 1.0])
        self.assertEqual(label, 'medium')
        self.assertIn('no_baseline_window', reasons)


if __name__ == '__main__':
    unittest.main()
