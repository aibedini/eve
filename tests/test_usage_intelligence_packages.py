"""Package selection (RFP sections 21-23 and tests 45.10-45.12, 45.17, 45.18)."""
import unittest

from panel.services.usage_intelligence import packages as package_module
from panel.services.usage_intelligence.schemas import ForecastMetrics


def _forecast(rate, *, margin=0.20):
    return ForecastMetrics(average_daily_gb=rate, projected_31d_gb=rate * 31,
                           safety_margin_percent=int(margin * 100),
                           buffered_requirement_gb=rate * 31 * (1 + margin))


def _catalog(*specs):
    return [{'id': index + 1, 'name': 'pkg-%d' % (index + 1), 'days': days,
             'volume': volume, 'price': price}
            for index, (days, volume, price) in enumerate(specs)]


class RecommendedPackageTests(unittest.TestCase):
    def test_the_smallest_covering_package_wins(self):
        catalog = _catalog((30, 10, 100), (30, 30, 200), (30, 50, 300), (30, 120, 500),
                           (30, 200, 700))
        result = package_module.select_packages(catalog, _forecast(3.39))
        recommended = result['recommended']
        # 3.39 GB/day x 30 days x 1.20 = 122GB required → the 200GB offer is the first fit.
        self.assertEqual(recommended.package_volume_gb, 200)
        self.assertFalse(recommended.capacity_limited)
        self.assertFalse(recommended.unlimited)
        self.assertAlmostEqual(recommended.required_gb, 122.0, places=0)
        self.assertEqual(recommended.reason, 'covers_forecast')

    def test_the_safety_margin_applies_to_the_primary_recommendation(self):
        catalog = _catalog((30, 100, 100), (30, 120, 200), (30, 200, 300))
        # 3.0 GB/day x 30 days = 90GB point forecast; with 20% headroom, 108GB.
        without = package_module.select_packages(catalog, _forecast(3.0, margin=0.0))
        with_margin = package_module.select_packages(catalog, _forecast(3.0, margin=0.20))
        self.assertEqual(without['recommended'].package_volume_gb, 100)
        self.assertEqual(with_margin['recommended'].package_volume_gb, 120)

    def test_each_package_is_judged_against_its_own_duration(self):
        """RFP 45.18: mixed durations must not be measured against 31 days."""
        catalog = _catalog((7, 50, 100), (30, 100, 200))
        # 5 GB/day: the 7-day offer must cover 5 x 7 x 1.2 = 42GB → 50GB fits ...
        weekly_first = package_module.select_packages(catalog, _forecast(5.0))
        self.assertEqual(weekly_first['recommended'].package_days, 7)
        self.assertEqual(weekly_first['recommended'].package_volume_gb, 50)
        # ... and the monthly one is measured against 180GB, so it would not.
        self.assertTrue(package_module.required_for(
            {'days': 30, 'volume': 100}, 5.0, safety_margin=0.2) > 100)

    def test_capacity_limited_when_even_the_largest_package_is_not_enough(self):
        catalog = _catalog((30, 30, 100), (30, 60, 200), (30, 100, 300))
        result = package_module.select_packages(catalog, _forecast(6.0))
        recommended = result['recommended']
        self.assertEqual(recommended.package_volume_gb, 100)
        self.assertTrue(recommended.capacity_limited)
        self.assertEqual(recommended.reason, 'largest_package_insufficient')
        # 6 x 30 x 1.2 = 216GB required, 100GB offered.
        self.assertAlmostEqual(recommended.capacity_shortfall_gb, 116.0, places=0)

    def test_zero_usage_recommends_the_smallest_offer_not_an_upsell(self):
        catalog = _catalog((30, 200, 100), (30, 10, 50), (30, 50, 80))
        result = package_module.select_packages(catalog, _forecast(0.0))
        self.assertEqual(result['recommended'].package_volume_gb, 10)
        self.assertEqual(result['recommended'].reason, 'no_usage_lowest')
        self.assertFalse(result['recommended'].capacity_limited)
        self.assertIsNone(result['comfort'])

    def test_an_empty_catalog_yields_no_choice(self):
        self.assertEqual(package_module.select_packages([], _forecast(3.0)),
                         {'recommended': None, 'comfort': None})


class ComfortPackageTests(unittest.TestCase):
    def test_comfort_is_one_level_up(self):
        catalog = _catalog((30, 50, 100), (30, 100, 200), (30, 200, 300))
        # 3.0 GB/day x 30 days = 90GB fits the 100GB offer exactly, so the next level up
        # (200GB) is the comfort answer.
        result = package_module.select_packages(catalog, _forecast(3.0, margin=0.0))
        self.assertEqual(result['recommended'].package_volume_gb, 100)
        self.assertEqual(result['comfort'].package_volume_gb, 200)
        self.assertEqual(result['comfort'].reason, 'comfort_step_up')

    def test_comfort_falls_back_to_unlimited_when_nothing_is_bigger(self):
        catalog = [{'id': 1, 'name': 'top', 'days': 30, 'volume': 100, 'price': 300},
                   {'id': 2, 'name': 'unmetered', 'days': 30, 'volume': 0, 'price': 900}]
        result = package_module.select_packages(catalog, _forecast(3.0, margin=0.0))
        self.assertEqual(result['recommended'].package_volume_gb, 100)
        self.assertTrue(result['comfort'].unlimited)
        self.assertEqual(result['comfort'].reason, 'comfort_unlimited')

    def test_no_comfort_when_the_recommendation_is_already_unlimited(self):
        catalog = [{'id': 1, 'name': 'unmetered', 'days': 30, 'volume': 0, 'price': 900}]
        result = package_module.select_packages(catalog, _forecast(3.0))
        self.assertTrue(result['recommended'].unlimited)
        self.assertIsNone(result['comfort'])


class UnlimitedPackageTests(unittest.TestCase):
    def test_a_finite_package_is_never_passed_over_for_unlimited(self):
        catalog = [{'id': 1, 'name': 'finite', 'days': 30, 'volume': 500, 'price': 500},
                   {'id': 2, 'name': 'unmetered', 'days': 30, 'volume': 0, 'price': 100}]
        result = package_module.select_packages(catalog, _forecast(3.0))
        # Even though unlimited is cheaper, the finite offer covers the demand.
        self.assertFalse(result['recommended'].unlimited)
        self.assertEqual(result['recommended'].package_volume_gb, 500)

    def test_unlimited_is_chosen_only_when_nothing_finite_covers_the_demand(self):
        catalog = [{'id': 1, 'name': 'small', 'days': 30, 'volume': 10, 'price': 50},
                   {'id': 2, 'name': 'unmetered', 'days': 30, 'volume': 0, 'price': 900}]
        result = package_module.select_packages(catalog, _forecast(5.0))
        self.assertTrue(result['recommended'].unlimited)
        self.assertEqual(result['recommended'].reason, 'no_finite_package_covers')
        # Unlimited does cover it, so it is not reported as capacity-limited.
        self.assertFalse(result['recommended'].capacity_limited)

    def test_unlimited_volume_survives_a_zero_usage_catalog(self):
        catalog = [{'id': 1, 'name': 'unmetered', 'days': 30, 'volume': 0, 'price': 900}]
        result = package_module.select_packages(catalog, _forecast(0.0))
        self.assertTrue(result['recommended'].unlimited)
        self.assertEqual(result['recommended'].reason, 'no_usage_lowest')


if __name__ == '__main__':
    unittest.main()
