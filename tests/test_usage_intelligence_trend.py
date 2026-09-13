"""Trend classification (RFP section 15 and test 45.2, 45.4).

Pure functions: no database, no Flask. The scenarios are the ones the RFP names, including
the reported bug's ratio (a heavy 8-day cycle against a light month must read as a strong
increase, not as noise).
"""
import unittest

from panel.services.usage_intelligence import trend as trend_module
from panel.services.usage_intelligence.schemas import (
    CycleMetrics, WindowMetrics, classify_trend, trend_thresholds_for,
)


def _cycle(rate, *, maturity='mature', available=True):
    return CycleMetrics(available=available, average_daily_gb=rate, maturity=maturity,
                        elapsed_days=8.0, effective_elapsed_days=8.0)


def _rolling(rate, *, available=True):
    return WindowMetrics(available=available, average_daily_gb=rate, basis_days=31.0)


class TrendDetectionTests(unittest.TestCase):
    def test_the_reported_bug_reads_as_a_strong_increase(self):
        """3.75 GB/day against 1.94 GB/day is +93%, not 'average 1.94'."""
        trend = trend_module.detect_trend(_cycle(3.75), _rolling(1.94))
        self.assertEqual(trend.state, 'strong_increase')
        self.assertAlmostEqual(trend.ratio, 1.933, places=3)
        self.assertEqual(trend.to_dict()['change_percent'], 93)

    def test_a_stable_cycle_is_stable(self):
        trend = trend_module.detect_trend(_cycle(1.5), _rolling(1.55))
        self.assertEqual(trend.state, 'stable')
        self.assertEqual(trend.to_dict()['change_percent'], -3)

    def test_an_increase_and_a_decrease_are_named(self):
        self.assertEqual(trend_module.detect_trend(_cycle(1.2), _rolling(1.0)).state,
                         'increasing')
        self.assertEqual(trend_module.detect_trend(_cycle(0.85), _rolling(1.0)).state,
                         'decreasing')
        self.assertEqual(trend_module.detect_trend(_cycle(0.5), _rolling(1.0)).state,
                         'strong_decrease')

    def test_a_short_cycle_needs_a_bigger_change_before_it_claims_a_trend(self):
        # 1.20 would be "increasing" on a mature cycle ...
        self.assertEqual(classify_trend(1.20, maturity='mature'), 'increasing')
        # ... but a six-hour-old cycle has not earned that claim yet.
        self.assertEqual(classify_trend(1.20, maturity='very_early'), 'stable')
        self.assertEqual(classify_trend(1.20, maturity='insufficient'), 'stable')
        # The same widening applies at the strong threshold.
        self.assertEqual(classify_trend(1.50, maturity='mature'), 'strong_increase')
        self.assertEqual(classify_trend(1.50, maturity='very_early'), 'increasing')

    def test_a_missing_or_zero_denominator_is_handled_safely(self):
        no_history = trend_module.detect_trend(_cycle(4.0), _rolling(0.0, available=False))
        self.assertEqual(no_history.state, 'unknown')
        self.assertIsNone(no_history.ratio)
        zero_rate = trend_module.detect_trend(_cycle(4.0), WindowMetrics(
            available=True, average_daily_gb=0.0, basis_days=31.0))
        self.assertEqual(zero_rate.state, 'unknown')
        self.assertIsNone(zero_rate.ratio)

    def test_without_a_cycle_there_is_no_trend(self):
        trend = trend_module.detect_trend(_cycle(0.0, available=False), _rolling(1.0))
        self.assertEqual(trend.state, 'unknown')
        self.assertIsNone(trend.change_percent)

    def test_an_early_cycle_is_flagged_as_confidence_aware(self):
        early = trend_module.detect_trend(_cycle(3.0, maturity='very_early'), _rolling(1.0))
        self.assertTrue(early.confidence_aware)
        mature = trend_module.detect_trend(_cycle(3.0, maturity='mature'), _rolling(1.0))
        self.assertFalse(mature.confidence_aware)

    def test_the_threshold_tables_are_the_documented_ones(self):
        self.assertEqual([limit for _name, limit in trend_thresholds_for('mature')],
                         [0.70, 0.90, 1.15, 1.40, None])
        self.assertEqual([limit for _name, limit in trend_thresholds_for('early')],
                         [0.60, 0.80, 1.25, 1.60, None])


class TrendWeightTests(unittest.TestCase):
    def test_a_strong_increase_gives_the_cycle_most_of_the_weight(self):
        cycle = _cycle(3.75, maturity='medium')
        rolling = _rolling(1.94)
        trend = trend_module.detect_trend(cycle, rolling)
        weight = trend_module.trend_weight(cycle, rolling, trend)
        self.assertGreaterEqual(weight, 0.80)
        self.assertLessEqual(weight, 1.0)

    def test_a_minutes_old_cycle_does_not_get_to_dominate(self):
        cycle = _cycle(20.0, maturity='insufficient')
        rolling = _rolling(1.0)
        trend = trend_module.detect_trend(cycle, rolling)
        self.assertLessEqual(trend_module.trend_weight(cycle, rolling, trend), 0.80)

    def test_with_no_history_to_blend_with_the_cycle_is_everything(self):
        cycle = _cycle(3.0, maturity='early')
        rolling = _rolling(0.0, available=False)
        trend = trend_module.detect_trend(cycle, rolling)
        self.assertEqual(trend_module.trend_weight(cycle, rolling, trend), 1.0)

    def test_a_decrease_still_counts_but_cannot_erase_the_month(self):
        cycle = _cycle(0.4, maturity='mature')
        rolling = _rolling(1.0)
        trend = trend_module.detect_trend(cycle, rolling)
        weight = trend_module.trend_weight(cycle, rolling, trend)
        self.assertGreaterEqual(weight, 0.40)
        self.assertLessEqual(weight, 0.55)


class TrendCopyTests(unittest.TestCase):
    def test_persian_and_english_copy_exist_for_every_state(self):
        for state in ('strong_increase', 'increasing', 'stable', 'decreasing',
                      'strong_decrease', 'unknown'):
            self.assertTrue(trend_module.describe_state(state, 'fa'))
            self.assertTrue(trend_module.describe_state(state, 'en'))
            self.assertNotEqual(trend_module.describe_state(state, 'fa'),
                                trend_module.describe_state(state, 'en'))
        # An unknown state falls back to the neutral sentence rather than crashing.
        self.assertEqual(trend_module.describe_state('nonsense', 'fa'),
                         trend_module.describe_state('unknown', 'fa'))


if __name__ == '__main__':
    unittest.main()
