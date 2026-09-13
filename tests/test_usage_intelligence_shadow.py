"""Shadow rollout (RFP sections 54-58): compute v5, answer with v4, compare.

End-to-end through the billing dispatcher on a real database: the customer-visible answer is
unchanged while v5 is evaluated, counted and logged, and the extreme cases are kept for an
operator to review before activation.
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
from panel.services.usage_intelligence import observability, record_verified_renewal  # noqa: E402
from panel.services.usage_intelligence import shadow as shadow_module  # noqa: E402
from panel.services.usage_intelligence.recommendation import FLAG_ENV  # noqa: E402

GB = 1024 ** 3
PACKAGES = [
    {'id': 1, 'name': 'starter', 'days': 30, 'volume': 30, 'price': 100},
    {'id': 2, 'name': 'standard', 'days': 30, 'volume': 60, 'price': 200},
    {'id': 3, 'name': 'plus', 'days': 30, 'volume': 120, 'price': 350},
    {'id': 4, 'name': 'max', 'days': 30, 'volume': 200, 'price': 500},
]


@contextmanager
def mode(value):
    with mock.patch.dict(os.environ, {FLAG_ENV: value}):
        yield


class ShadowRolloutTests(unittest.TestCase):
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
        shadow_module.reset_shadow_metrics()
        observability.reset()
        RenewalEvent.query.delete()
        UsageDaily.query.delete()
        UsageCounterState.query.delete()
        Server.query.delete()
        db.session.commit()
        self.server = Server(name='shadow', host='https://shadow.invalid', username='u',
                             password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()

    def _seed(self, sub_id='shadow-account', *, spike=False):
        renewed_at = datetime.utcnow() - timedelta(days=8)

        def to_ms(value):
            return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)

        record_verified_renewal(
            server_id=self.server.id, sub_id=sub_id, operation_id='op-shadow', days=30,
            previous_volume_limit_bytes=50 * GB, new_volume_limit_bytes=100 * GB,
            previous_remaining_bytes=20 * GB, granted_volume_bytes=50 * GB,
            previous_expiry_ms=to_ms(renewed_at),
            new_expiry_ms=to_ms(renewed_at + timedelta(days=30)),
            renewed_at=renewed_at)
        for offset in range(31):
            observed = datetime.utcnow() - timedelta(days=offset)
            # A heavy cycle after a light month: the case the whole RFP is about.
            used = int((3.75 if offset < 8 else 1.3) * (40 if spike and offset == 0 else 1) * GB)
            db.session.add(UsageDaily(
                server_id=self.server.id, sub_id=sub_id,
                usage_date=date.today() - timedelta(days=offset),
                upload_bytes=0, download_bytes=used,
                opening_upload_bytes=0, opening_download_bytes=0,
                closing_upload_bytes=0, closing_download_bytes=used,
                sample_count=1, first_observed_at=observed, last_observed_at=observed))
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0,
            download_bytes=60 * GB, total_bytes=60 * GB, observed_at=datetime.utcnow()))
        db.session.commit()
        return sub_id

    def test_shadow_answers_with_v4_and_still_evaluates_v5(self):
        sub_id = self._seed()
        with mode('shadow'):
            with mock.patch.object(shadow_module.logger, 'info') as log:
                payload = billing._build_subscription_package_recommendation(
                    self.server.id, sub_id, PACKAGES,
                    live_usage={'total_bytes': 60 * GB,
                                'observed_at': datetime.utcnow()})

        # The customer still gets the v4 answer ...
        self.assertEqual(payload['model_version'], 'usage-fit-v4')
        self.assertEqual(payload['source'], 'last_31_days')
        # ... while v5 was computed, counted and logged.
        metrics = shadow_module.shadow_metrics()
        self.assertEqual(metrics['comparisons'], 1)
        self.assertTrue(log.called)
        message = log.call_args[0][0] % log.call_args[0][1:]
        self.assertIn('v5_forecast=', message)
        self.assertIn('package_changed=', message)
        self.assertNotIn('@', message)
        # The v5 recommendation itself is observed too (metrics + structured line).
        self.assertEqual(observability.snapshot()['recommendation_total'], 1)

    def test_shadow_records_the_package_and_forecast_difference(self):
        sub_id = self._seed()
        with mode('shadow'):
            billing._build_subscription_package_recommendation(
                self.server.id, sub_id, PACKAGES,
                live_usage={'total_bytes': 60 * GB, 'observed_at': datetime.utcnow()})
        metrics = shadow_module.shadow_metrics()
        # v4 projects ~60GB and picks a small package; v5 projects >100GB for this account,
        # so the package changes and the forecast moved by tens of percent.
        self.assertEqual(metrics['package_changed_percent'], 100.0)
        self.assertGreater(metrics['forecast_delta_percent_mean'], 10.0)
        self.assertGreaterEqual(metrics['strong_trend_count'], 0)

    def test_an_extreme_case_is_kept_for_review_without_pii(self):
        sub_id = self._seed(spike=True)
        v4 = {'package_id': 1, 'projected_31d_gb': 60.0, 'capacity_limited': False}
        v5 = {'recommendation': {'package_id': 4, 'capacity_limited': True},
              'forecast': {'projected_31d_gb': 900.0},
              'trend': {'state': 'strong_increase'},
              'signals': {'early_exhaustion': True}}
        record = shadow_module.record_shadow_comparison(self.server.id, sub_id, v4, v5)
        self.assertTrue(record['extreme'])
        cases = shadow_module.extreme_cases()
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case['v4_package'], 1)
        self.assertEqual(case['v5_package'], 4)
        self.assertGreaterEqual(case['forecast_delta_percent'], 100.0)
        self.assertEqual(case['account'], 'redacted')
        self.assertNotIn('shadow-account', str(case))
        self.assertEqual(shadow_module.shadow_metrics()['extreme_cases'], 1)

    def test_a_normal_difference_is_not_flagged_extreme(self):
        v4 = {'package_id': 2, 'projected_31d_gb': 100.0, 'capacity_limited': False}
        v5 = {'recommendation': {'package_id': 3, 'capacity_limited': False},
              'forecast': {'projected_31d_gb': 120.0},
              'trend': {'state': 'increasing'},
              'signals': {'early_exhaustion': False}}
        record = shadow_module.record_shadow_comparison(1, 'acct', v4, v5)
        self.assertFalse(record['extreme'])
        self.assertEqual(shadow_module.extreme_cases(), [])

    def test_switching_to_on_changes_the_answer(self):
        sub_id = self._seed()
        with mode('shadow'):
            shadowed = billing._build_subscription_package_recommendation(
                self.server.id, sub_id, PACKAGES,
                live_usage={'total_bytes': 60 * GB, 'observed_at': datetime.utcnow()})
        with mode('on'):
            activated = billing._build_subscription_package_recommendation(
                self.server.id, sub_id, PACKAGES,
                live_usage={'total_bytes': 60 * GB, 'observed_at': datetime.utcnow()})
        self.assertEqual(shadowed['model_version'], 'usage-fit-v4')
        self.assertEqual(activated['model_version'], 'usage-fit-v5')
        # The activated answer is the one the shadow run had been computing all along.
        self.assertEqual(activated['trend']['state'], 'strong_increase')
        self.assertGreater(activated['projected_31d_gb'], shadowed['projected_31d_gb'])

    def test_off_never_evaluates_v5(self):
        sub_id = self._seed()
        with mode('off'):
            with mock.patch.object(shadow_module, 'record_shadow_comparison') as record:
                billing._build_subscription_package_recommendation(
                    self.server.id, sub_id, PACKAGES,
                    live_usage={'total_bytes': 60 * GB, 'observed_at': datetime.utcnow()})
        self.assertFalse(record.called)
        self.assertEqual(shadow_module.shadow_metrics()['comparisons'], 0)
        self.assertEqual(observability.snapshot()['recommendation_total'], 0)


if __name__ == '__main__':
    unittest.main()
