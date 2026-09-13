"""The usage-fit-v5 contract and its rollout flag (RFP sections 24-25, 28, 54-57, 69).

End-to-end through the real database: seed telemetry and a verified renewal, ask for the
recommendation, and assert the payload the API/UI will read - including the reported bug's
scenario, where the recommendation must not be a projection of the stale 31-day average.
"""
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import RenewalEvent, Server, UsageCounterState, UsageDaily, app, db  # noqa: E402
from panel.services import billing  # noqa: E402
from panel.services.usage_intelligence import analysis, record_verified_renewal  # noqa: E402
from panel.services.usage_intelligence import recommendation as recommendation_module  # noqa: E402
from panel.services.usage_intelligence import shadow as shadow_module  # noqa: E402

GB = 1024 ** 3
PACKAGES = [
    {'id': 1, 'name': 'starter', 'days': 30, 'volume': 30, 'price': 100},
    {'id': 2, 'name': 'standard', 'days': 30, 'volume': 60, 'price': 200},
    {'id': 3, 'name': 'plus', 'days': 30, 'volume': 120, 'price': 350},
    {'id': 4, 'name': 'max', 'days': 30, 'volume': 200, 'price': 500},
]


@contextmanager
def v5_mode(mode):
    with mock.patch.dict(os.environ, {recommendation_module.FLAG_ENV: mode}):
        yield


class RecommendationV5Tests(unittest.TestCase):
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

    def setUp(self):
        RenewalEvent.query.delete()
        UsageDaily.query.delete()
        UsageCounterState.query.delete()
        Server.query.delete()
        db.session.commit()
        self.server = Server(name='v5', host='https://v5.invalid', username='u',
                             password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()
        shadow_module.reset_shadow_metrics()

    def _daily(self, sub_id, days_ago, gb):
        observed = datetime.utcnow() - timedelta(days=days_ago)
        used = int(gb * GB)
        db.session.add(UsageDaily(
            server_id=self.server.id, sub_id=sub_id,
            usage_date=date.today() - timedelta(days=days_ago),
            upload_bytes=0, download_bytes=used,
            opening_upload_bytes=0, opening_download_bytes=0,
            closing_upload_bytes=0, closing_download_bytes=used,
            sample_count=1, first_observed_at=observed, last_observed_at=observed))

    def _renewal(self, sub_id, *, days_ago, limit_gb, remaining_gb, granted_gb,
                 new_limit_gb, days=30, traffic_reset=False):
        renewed_at = datetime.utcnow() - timedelta(days=days_ago)

        def to_ms(value):
            return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)

        record_verified_renewal(
            server_id=self.server.id, sub_id=sub_id, operation_id='op-%s' % sub_id,
            days=days,
            previous_volume_limit_bytes=int(limit_gb * GB),
            new_volume_limit_bytes=int(new_limit_gb * GB),
            previous_remaining_bytes=int(remaining_gb * GB),
            granted_volume_bytes=int(granted_gb * GB),
            previous_expiry_ms=to_ms(renewed_at),
            new_expiry_ms=to_ms(renewed_at + timedelta(days=days)),
            traffic_reset=traffic_reset, renewed_at=renewed_at)
        db.session.commit()

    def _counter(self, sub_id, total_gb, *, minutes_ago=1):
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0,
            download_bytes=int(total_gb * GB), total_bytes=int(total_gb * GB),
            observed_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
        db.session.commit()

    def _recommend(self, sub_id, *, mode='on', live=None):
        with v5_mode(mode):
            return billing._build_subscription_package_recommendation(
                self.server.id, sub_id, PACKAGES, live_usage=live)

    def test_the_reported_bug_is_not_answered_with_the_stale_average(self):
        """RFP section 46/69: 60GB in 31 days, 30GB in the 8 days since the renewal."""
        sub_id = 'bug'
        # At the boundary the counter read 30GB (50GB cap, 20GB unused); it reads 60GB now.
        self._renewal(sub_id, days_ago=8, limit_gb=50, remaining_gb=20, granted_gb=50,
                      new_limit_gb=100)
        # A month of history: ~3.75GB/day since the renewal, ~1.3GB/day before it.
        for offset in range(31):
            self._daily(sub_id, offset, 3.75 if offset < 8 else 1.3)
        self._counter(sub_id, 60)

        payload = self._recommend(sub_id, live={'total_bytes': 60 * GB,
                                               'observed_at': datetime.utcnow()})
        self.assertIsNotNone(payload)
        self.assertEqual(payload['model_version'], 'usage-fit-v5')
        self.assertTrue(payload['current_cycle']['available'])
        self.assertEqual(payload['trend']['state'], 'strong_increase')
        self.assertGreater(payload['forecast']['average_daily_gb'],
                           payload['rolling_31d']['average_daily_gb'])
        # The trajectory: ~3.75 GB/day in the cycle against ~1.9 GB/day over the month.
        self.assertGreater(payload['current_cycle']['average_daily_gb'], 3.5)
        self.assertLess(payload['rolling_31d']['average_daily_gb'], 2.2)
        # A 60GB projection is what the old model would have said; the new forecast is far
        # above it, and the recommended package must cover the forecast, not the average.
        self.assertGreater(payload['forecast']['projected_31d_gb'], 100.0)
        self.assertEqual(payload['forecast_basis'], 'current_cycle_dominant')
        self.assertGreaterEqual(payload['package_volume'], payload['forecast']['projected_31d_gb'])

    def test_the_payload_carries_the_section_24_shape(self):
        sub_id = 'shape'
        self._renewal(sub_id, days_ago=10, limit_gb=50, remaining_gb=0, granted_gb=50,
                      new_limit_gb=50)
        for offset in range(0, 12):
            self._daily(sub_id, offset, 2)
        self._counter(sub_id, 24)

        payload = self._recommend(sub_id)
        self.assertEqual(payload['model_version'], 'usage-fit-v5')
        for key in ('current_cycle', 'rolling_31d', 'historical_baseline', 'trend',
                    'forecast', 'signals', 'confidence', 'recommendation'):
            self.assertIn(key, payload)
        self.assertEqual(set(payload['confidence']), {'data', 'behavior_stability'})
        self.assertIn(payload['confidence']['data'], ('high', 'medium', 'early'))
        self.assertIn(payload['confidence']['behavior_stability'], ('high', 'medium', 'low'))
        self.assertEqual(payload['recommendation']['package_id'], payload['package_id'])
        self.assertIn(payload['forecast_basis'],
                      ('current_cycle_dominant', 'blended', 'rolling_history',
                       'live_fallback'))
        self.assertLessEqual(payload['evidence']['queries'], analysis.QUERY_BUDGET)

    def test_the_transition_fields_derive_from_v5(self):
        sub_id = 'compat'
        self._renewal(sub_id, days_ago=9, limit_gb=50, remaining_gb=0, granted_gb=50,
                      new_limit_gb=50)
        for offset in range(0, 10):
            self._daily(sub_id, offset, 3)
        self._counter(sub_id, 27)

        payload = self._recommend(sub_id)
        # Legacy consumers still find what they read, and it agrees with v5.
        self.assertEqual(payload['confidence_label'], payload['confidence']['data'])
        self.assertEqual(payload['average_daily_gb'],
                         payload['forecast']['average_daily_gb'])
        self.assertEqual(payload['projected_31d_gb'],
                         payload['forecast']['projected_31d_gb'])
        self.assertEqual(payload['package_id'], payload['recommendation']['package_id'])
        self.assertEqual(payload['fast_cycle'], payload['signals']['early_exhaustion'])
        self.assertIn(payload['source'], ('current_cycle', 'last_31_days', 'live_counter'))
        self.assertGreaterEqual(payload['basis_days'], 1.0)

    def test_early_exhaustion_produces_the_expected_payload(self):
        sub_id = 'exhausted'
        # A 30-day package: 50GB cap, nothing left at the boundary, 50GB granted, and the
        # counter has already run to the 100GB cap the renewal created.
        self._renewal(sub_id, days_ago=8, limit_gb=50, remaining_gb=0, granted_gb=50,
                      new_limit_gb=100, days=30)
        self._daily(sub_id, 1, 5)
        self._daily(sub_id, 2, 5)
        self._counter(sub_id, 100)

        payload = self._recommend(sub_id)
        self.assertTrue(payload['signals']['early_exhaustion'])
        self.assertEqual(payload['signals']['exhaustion_severity'], 'critical')
        self.assertEqual(payload['fast_cycle'], True)
        self.assertGreaterEqual(payload['forecast']['projected_31d_gb'],
                                payload['projected_31d_gb'])

    def test_no_usage_at_all_yields_no_v5_recommendation(self):
        payload = self._recommend('nothing')
        self.assertIsNone(payload)


class RecommendationModeTests(unittest.TestCase):
    """RFP sections 54-57: off / shadow / on, and shadow computes without deciding."""

    def test_the_mode_comes_from_the_environment(self):
        for raw, expected in (('1', 'on'), ('on', 'on'), ('shadow', 'shadow'),
                              ('0', 'off'), ('off', 'off'), ('nonsense', 'on')):
            with mock.patch.dict(os.environ, {recommendation_module.FLAG_ENV: raw}):
                self.assertEqual(recommendation_module.recommendation_mode(), expected,
                                 raw)

    def test_v5_is_the_default_when_nothing_is_configured(self):
        """Activation (RFP section 57): the model answers unless an operator says otherwise."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(recommendation_module.FLAG_ENV, None)
            with mock.patch('panel.models.SystemSetting') as setting:
                setting.query.filter_by.return_value.first.return_value = None
                self.assertEqual(recommendation_module.recommendation_mode(), 'on')

    def test_the_system_setting_can_roll_back_without_a_restart(self):
        class _Row:
            value = 'off'

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(recommendation_module.FLAG_ENV, None)
            with mock.patch('panel.models.SystemSetting') as setting:
                setting.query.filter_by.return_value.first.return_value = _Row()
                self.assertEqual(recommendation_module.recommendation_mode(), 'off')

    def test_an_explicit_environment_value_beats_the_setting(self):
        class _Row:
            value = 'on'

        with mock.patch.dict(os.environ, {recommendation_module.FLAG_ENV: 'off'}):
            with mock.patch('panel.models.SystemSetting') as setting:
                setting.query.filter_by.return_value.first.return_value = _Row()
                self.assertEqual(recommendation_module.recommendation_mode(), 'off')

    def test_off_keeps_answering_with_v4(self):
        with v5_mode('off'):
            with mock.patch.object(billing, '_build_recommendation_v4',
                                   return_value={'model_version': 'usage-fit-v4'}) as v4:
                with mock.patch.object(billing, '_shadow_compare_v5') as shadow:
                    result = billing._build_subscription_package_recommendation(
                        1, 'acct', PACKAGES)
        self.assertEqual(result['model_version'], 'usage-fit-v4')
        self.assertTrue(v4.called)
        self.assertFalse(shadow.called)

    def test_shadow_computes_v5_but_answers_with_v4(self):
        with v5_mode('shadow'):
            with mock.patch.object(billing, '_build_recommendation_v4',
                                   return_value={'model_version': 'usage-fit-v4',
                                                 'package_id': 1,
                                                 'projected_31d_gb': 60.0}) as v4:
                with mock.patch.object(billing, '_shadow_compare_v5') as shadow:
                    result = billing._build_subscription_package_recommendation(
                        1, 'acct', PACKAGES)
        self.assertEqual(result['model_version'], 'usage-fit-v4')
        self.assertTrue(shadow.called)
        self.assertTrue(v4.called)

    def test_on_uses_v5_and_falls_back_when_v5_has_nothing(self):
        with v5_mode('on'):
            with mock.patch.object(recommendation_module, 'build_recommendation_v5',
                                   return_value={'model_version': 'usage-fit-v5'}) as v5:
                result = billing._build_subscription_package_recommendation(
                    1, 'acct', PACKAGES)
        self.assertEqual(result['model_version'], 'usage-fit-v5')
        self.assertTrue(v5.called)

        with v5_mode('on'):
            with mock.patch.object(recommendation_module, 'build_recommendation_v5',
                                   return_value=None):
                with mock.patch.object(billing, '_build_recommendation_v4',
                                       return_value={'model_version': 'usage-fit-v4'}):
                    fallback = billing._build_subscription_package_recommendation(
                        1, 'acct', PACKAGES)
        self.assertEqual(fallback['model_version'], 'usage-fit-v4')

    def test_a_v5_error_never_breaks_the_recommendation(self):
        with v5_mode('on'):
            with mock.patch.object(recommendation_module, 'build_recommendation_v5',
                                   side_effect=RuntimeError('boom')):
                with mock.patch.object(billing, '_build_recommendation_v4',
                                       return_value={'model_version': 'usage-fit-v4'}):
                    result = billing._build_subscription_package_recommendation(
                        1, 'acct', PACKAGES)
        self.assertEqual(result['model_version'], 'usage-fit-v4')


class ShadowComparisonTests(unittest.TestCase):
    def setUp(self):
        shadow_module.reset_shadow_metrics()

    def test_the_comparison_counts_and_stays_pii_free(self):
        v4 = {'package_id': 2, 'projected_31d_gb': 60.0, 'capacity_limited': False}
        v5 = {'recommendation': {'package_id': 3, 'capacity_limited': False},
              'forecast': {'projected_31d_gb': 105.0},
              'trend': {'state': 'strong_increase'},
              'signals': {'early_exhaustion': False}}
        with mock.patch.object(shadow_module.logger, 'info') as log:
            record = shadow_module.record_shadow_comparison(7, 'acct', v4, v5,
                                                            account='redacted')
        self.assertTrue(record['package_changed'])
        self.assertAlmostEqual(record['forecast_delta_percent'], 75.0, places=1)
        self.assertEqual(record['trend_state'], 'strong_increase')
        metrics = shadow_module.shadow_metrics()
        self.assertEqual(metrics['comparisons'], 1)
        self.assertEqual(metrics['package_changed_percent'], 100.0)
        self.assertEqual(metrics['strong_trend_count'], 1)
        # The log line carries ids and numbers only.
        self.assertTrue(log.called)
        message = log.call_args[0][0] % log.call_args[0][1:]
        self.assertIn('server_id=7', message)
        self.assertNotIn('@', message)

    def test_a_missing_v5_is_counted_not_raised(self):
        metrics_before = shadow_module.shadow_metrics()['comparisons']
        record = shadow_module.record_shadow_comparison(7, 'acct', {'package_id': 1}, None)
        self.assertFalse(record['v5_available'])
        metrics = shadow_module.shadow_metrics()
        self.assertEqual(metrics['comparisons'], metrics_before + 1)
        self.assertEqual(metrics['v5_unavailable'], 1)


if __name__ == '__main__':
    unittest.main()
