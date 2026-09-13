"""Forecast blend, robust bounds and safety margin (RFP sections 17, 18, 21, 69).

Pure functions: every case here is arithmetic on metrics, with no database.
"""
import unittest

from panel.services.usage_intelligence import forecast as forecast_module
from panel.services.usage_intelligence import trend as trend_module
from panel.services.usage_intelligence.schemas import (
    CycleMetrics, Signals, WindowMetrics,
)


def _cycle(rate, *, maturity='insufficient', elapsed=8.0, available=True):
    return CycleMetrics(available=available, average_daily_gb=rate, maturity=maturity,
                        elapsed_days=elapsed, effective_elapsed_days=elapsed)


def _rolling(rate, *, basis=31.0, available=True):
    return WindowMetrics(available=available, average_daily_gb=rate, basis_days=basis)


class ForecastBlendTests(unittest.TestCase):
    def test_the_acceptance_scenario_projects_about_105gb(self):
        """RFP section 69: 0.8 x 3.75 + 0.2 x 1.94 ≈ 3.39 GB/day → ≈105GB, buffered ≈126."""
        cycle, rolling = _cycle(3.75), _rolling(1.94)
        trend = trend_module.detect_trend(cycle, rolling)
        result = forecast_module.forecast_usage(cycle, rolling, trend=trend)

        self.assertEqual(result.basis, 'current_cycle_dominant')
        self.assertEqual(result.blend, (0.80, 0.20))
        self.assertAlmostEqual(result.average_daily_gb, 3.388, places=3)
        self.assertAlmostEqual(result.projected_31d_gb, 105.03, places=1)
        self.assertEqual(result.safety_margin_percent, 20)
        self.assertAlmostEqual(result.buffered_requirement_gb, 126.04, places=1)

    def test_a_mature_stable_cycle_uses_the_65_35_blend(self):
        # Within the stable band (0.90-1.15) so the trend really is stable.
        cycle, rolling = _cycle(1.05, maturity='mature', elapsed=20.0), _rolling(1.0)
        trend = trend_module.detect_trend(cycle, rolling)
        self.assertEqual(trend.state, 'stable')
        result = forecast_module.forecast_usage(cycle, rolling, trend=trend)
        self.assertEqual(result.blend, (0.65, 0.35))
        self.assertAlmostEqual(result.average_daily_gb, 1.0325, places=3)
        self.assertEqual(result.basis, 'blended')
        self.assertEqual(result.safety_margin_percent, 15)

    def test_a_young_cycle_leans_on_the_history(self):
        # 2.2 against 2.0 sits inside the widened early band (stable), so the history keeps
        # most of the weight; a strong increase in an early cycle still wins (see below).
        cycle, rolling = _cycle(2.2, maturity='early', elapsed=2.0), _rolling(2.0)
        trend = trend_module.detect_trend(cycle, rolling)
        self.assertEqual(trend.state, 'stable')
        result = forecast_module.forecast_usage(cycle, rolling, trend=trend)
        self.assertEqual(result.blend, (0.45, 0.55))
        self.assertAlmostEqual(result.average_daily_gb, 2.09, places=3)
        # Two days of evidence is early: the widest safety margin applies.
        self.assertEqual(result.safety_margin_percent, 25)

    def test_an_early_exhaustion_overrides_even_a_stable_cycle(self):
        cycle = _cycle(1.0, maturity='mature', elapsed=20.0)
        rolling = _rolling(1.0)
        trend = trend_module.detect_trend(cycle, rolling)
        self.assertEqual(trend.state, 'stable')
        signals = Signals(early_exhaustion=True, exhaustion_severity='critical',
                          exhaustion_ratio=0.26, expected_duration_days=31.0)
        result = forecast_module.forecast_usage(cycle, rolling, signals=signals, trend=trend)
        self.assertEqual(result.blend, (0.80, 0.20))
        self.assertEqual(result.basis, 'current_cycle_dominant')

    def test_without_a_cycle_the_history_answers_alone(self):
        result = forecast_module.forecast_usage(_cycle(0.0, available=False), _rolling(1.6))
        self.assertEqual(result.basis, 'rolling_history')
        self.assertAlmostEqual(result.average_daily_gb, 1.6, places=3)
        self.assertEqual(result.safety_margin_percent, 25)

    def test_without_any_window_the_live_fallback_is_admitted(self):
        result = forecast_module.forecast_usage(
            _cycle(0.0, available=False), _rolling(0.0, available=False))
        self.assertEqual(result.basis, 'live_fallback')
        self.assertEqual(result.average_daily_gb, 0.0)
        self.assertEqual(result.buffered_requirement_gb, 0.0)

    def test_the_horizon_follows_the_package_duration(self):
        cycle, rolling = _cycle(3.0, maturity='mature', elapsed=20.0), _rolling(3.0)
        trend = trend_module.detect_trend(cycle, rolling)
        week = forecast_module.forecast_usage(cycle, rolling, trend=trend, horizon_days=7)
        month = forecast_module.forecast_usage(cycle, rolling, trend=trend, horizon_days=30)
        self.assertAlmostEqual(week.projected_31d_gb, 21.0, places=1)
        self.assertAlmostEqual(month.projected_31d_gb, 90.0, places=1)
        self.assertEqual(week.horizon_days, 7)
        self.assertLess(week.buffered_requirement_gb, month.buffered_requirement_gb)


class RobustBoundsTests(unittest.TestCase):
    def test_one_freak_day_does_not_rewrite_the_recommendation(self):
        """RFP section 18: a 25GB day in a 0.5GB/day account must not wreck the forecast."""
        series = [0.5] * 9 + [25.0]
        stats = forecast_module.daily_series_stats(series)
        self.assertEqual(stats['count'], 10)
        self.assertAlmostEqual(stats['median'], 0.5, places=2)
        # max(3 x median, P90); P90 interpolates between the normal days and the spike, so
        # the cap still lands far below the outlier it exists to trim.
        self.assertAlmostEqual(stats['cap'], 2.95, places=2)
        self.assertLess(stats['cap'], 25.0 / 4.0)
        rate = forecast_module.robust_rate(series, basis_days=31.0)
        # Without the cap the rate would be ~1.29 GB/day; with it, ~0.31.
        self.assertLess(rate, 0.4)
        self.assertGreater(rate, 0.2)

    def test_a_genuine_sustained_increase_survives_the_cap(self):
        series = [1.0] * 20 + [5.0] * 8
        stats = forecast_module.daily_series_stats(series)
        self.assertAlmostEqual(stats['median'], 1.0, places=2)
        # The cap (max(3 x median, P90)) trims nothing here: no day exceeds 5.0.
        rate = forecast_module.robust_rate(series, basis_days=28.0)
        self.assertAlmostEqual(rate, (20 * 1.0 + 8 * 5.0) / 28.0, places=3)

    def test_the_cap_scales_with_the_accounts_own_baseline(self):
        small = forecast_module.robust_cap([0.5, 0.5, 0.5, 0.6])
        large = forecast_module.robust_cap([50.0, 50.0, 50.0, 60.0])
        self.assertLess(small, large)
        self.assertGreater(large, 100.0)

    def test_too_few_days_means_no_robust_rate(self):
        self.assertIsNone(forecast_module.robust_rate([5.0], basis_days=1.0))
        self.assertIsNone(forecast_module.robust_rate([], basis_days=1.0))
        self.assertIsNone(forecast_module.robust_cap([5.0]))
        self.assertEqual(forecast_module.daily_series_stats([])['cap'], None)

    def test_the_forecast_uses_the_winsorized_series_when_it_has_one(self):
        cycle = _cycle(4.0, maturity='medium', elapsed=10.0)
        rolling = _rolling(1.0, basis=10.0)
        trend = trend_module.detect_trend(cycle, rolling)
        series = [1.0] * 9 + [20.0]
        result = forecast_module.forecast_usage(cycle, rolling, trend=trend,
                                                cycle_daily_gb=series)
        capped_rate = forecast_module.robust_rate(series, basis_days=10.0)
        expected = capped_rate * 0.8 + 1.0 * 0.2
        self.assertAlmostEqual(result.average_daily_gb, expected, places=3)
        self.assertLess(result.average_daily_gb, 4.0 * 0.8 + 1.0 * 0.2)
        self.assertIsNotNone(result.capped_daily_gb)


class SafetyMarginTests(unittest.TestCase):
    def test_the_margin_table_matches_the_rfp(self):
        self.assertEqual(
            forecast_module.safety_margin_for('stable', data_confidence='high'), 0.10)
        self.assertEqual(
            forecast_module.safety_margin_for('stable', data_confidence='medium'), 0.15)
        self.assertEqual(
            forecast_module.safety_margin_for('strong_increase', data_confidence='high'),
            0.20)
        self.assertEqual(
            forecast_module.safety_margin_for('stable', data_confidence='early'), 0.25)
        self.assertEqual(
            forecast_module.safety_margin_for('stable', maturity='very_early'), 0.25)

    def test_the_margin_can_be_overridden_by_the_caller(self):
        cycle, rolling = _cycle(1.0, maturity='mature', elapsed=20.0), _rolling(1.0)
        trend = trend_module.detect_trend(cycle, rolling)
        result = forecast_module.forecast_usage(cycle, rolling, trend=trend,
                                                safety_margin=0.05)
        self.assertEqual(result.safety_margin_percent, 5)

    def test_zero_usage_produces_a_zero_requirement_without_raising(self):
        result = forecast_module.forecast_usage(_cycle(0.0, available=False),
                                               _rolling(0.0))
        self.assertEqual(result.average_daily_gb, 0.0)
        self.assertEqual(result.projected_31d_gb, 0.0)
        self.assertEqual(result.buffered_requirement_gb, 0.0)
        self.assertEqual(result.to_dict()['basis'], 'live_fallback')


if __name__ == '__main__':
    unittest.main()
