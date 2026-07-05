import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    GLOBAL_SERVER_DATA,
    RenewalEvent,
    app,
    db,
    _build_subscription_package_recommendation,
    _select_subscription_package,
)


PACKAGES = [
    {'id': 1, 'name': '10 GB', 'days': 31, 'volume': 10, 'price': 150_000},
    {'id': 2, 'name': '20 GB', 'days': 31, 'volume': 20, 'price': 270_000},
    {'id': 3, 'name': '30 GB', 'days': 31, 'volume': 30, 'price': 390_000},
    {'id': 4, 'name': '50 GB', 'days': 31, 'volume': 50, 'price': 600_000},
]


class PackageRecommendationRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()
        try:
            os.unlink(_DB_FILE.name)
        except OSError:
            pass

    def test_forecast_above_catalog_uses_largest_available(self):
        selected, comfort, _ = _select_subscription_package(
            PACKAGES, daily_gb=3.0, safety_margin=0.2,
        )
        self.assertEqual(selected['id'], 4)
        self.assertIsNone(comfort)

    def test_live_counter_prevents_hourly_rollup_blind_spot(self):
        recommendation = _build_subscription_package_recommendation(
            999999,
            'live-only-account',
            PACKAGES,
            terminal=True,
            live_usage={
                'total_bytes': 10 * 1024 ** 3,
                'volume_limit_bytes': 10 * 1024 ** 3,
                'expiry_ts_ms': 0,
                'observed_at': datetime.utcnow(),
            },
        )
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation['package_id'], 1)
        self.assertEqual(recommendation['model_version'], 'usage-fit-v3')

    def test_high_live_usage_is_visible_and_truthfully_labeled(self):
        recommendation = _build_subscription_package_recommendation(
            999999,
            'high-usage-live-account',
            PACKAGES,
            terminal=True,
            live_usage={
                'total_bytes': 100 * 1024 ** 3,
                'volume_limit_bytes': 100 * 1024 ** 3,
                'expiry_ts_ms': 0,
                'observed_at': datetime.utcnow(),
            },
        )
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation['package_id'], 4)
        self.assertTrue(recommendation['capacity_limited'])

    def test_all_callers_fall_back_to_shared_live_snapshot(self):
        previous = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': 777777,
            'clients': [{
                'subId': 'template-account',
                'up': 2 * 1024 ** 3,
                'down': 8 * 1024 ** 3,
                'totalGB': 10 * 1024 ** 3,
                'expiryTimestamp': 0,
            }],
        }]
        try:
            recommendation = _build_subscription_package_recommendation(
                777777, 'template-account', PACKAGES, terminal=True,
            )
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation['package_id'], 1)

    def test_zero_usage_still_has_no_recommendation(self):
        recommendation = _build_subscription_package_recommendation(
            999999,
            'never-used-account',
            PACKAGES,
            live_usage={'total_bytes': 0},
        )
        self.assertIsNone(recommendation)

    def test_live_fallback_survives_rollup_schema_failure(self):
        with patch.object(RenewalEvent.query_class, 'filter_by', side_effect=RuntimeError('migration pending')):
            recommendation = _build_subscription_package_recommendation(
                999999,
                'schema-recovery-account',
                PACKAGES,
                terminal=True,
                live_usage={
                    'total_bytes': 10 * 1024 ** 3,
                    'volume_limit_bytes': 10 * 1024 ** 3,
                    'observed_at': datetime.utcnow(),
                },
            )
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation['package_id'], 1)


if __name__ == '__main__':
    unittest.main()
