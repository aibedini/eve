"""Recommendation observability (RFP sections 41-44).

Counters, latency and one structured log line - with the privacy rule enforced by the module
rather than trusted to its callers.
"""
import os
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import RenewalEvent, Server, UsageCounterState, UsageDaily, app, db  # noqa: E402
from panel.services.usage_intelligence import observability, record_verified_renewal  # noqa: E402
from panel.services.usage_intelligence.recommendation import build_recommendation_v5  # noqa: E402

GB = 1024 ** 3
PACKAGES = [
    {'id': 1, 'name': 'standard', 'days': 30, 'volume': 60, 'price': 200},
    {'id': 2, 'name': 'plus', 'days': 30, 'volume': 120, 'price': 350},
    {'id': 3, 'name': 'max', 'days': 30, 'volume': 200, 'price': 500},
]


class RedactionTests(unittest.TestCase):
    def test_the_reference_is_stable_and_hides_the_input(self):
        first = observability.redact_ref('09121234567@example.test')
        second = observability.redact_ref('09121234567@example.test')
        other = observability.redact_ref('someone-else')
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith('acct-'))
        self.assertNotIn('0912', first)
        self.assertNotIn('@', first)
        self.assertEqual(observability.redact_ref(None), 'redacted')


class ObservabilityTests(unittest.TestCase):
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
        observability.reset()
        RenewalEvent.query.delete()
        UsageDaily.query.delete()
        UsageCounterState.query.delete()
        Server.query.delete()
        db.session.commit()
        self.server = Server(name='obs', host='https://obs.invalid', username='u',
                             password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()

    def _seed_and_build(self, *, exhausted=False):
        sub_id = '09121234567@example.test'
        renewed_at = datetime.utcnow() - timedelta(days=8)

        def to_ms(value):
            return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)

        record_verified_renewal(
            server_id=self.server.id, sub_id=sub_id, operation_id='op-obs', days=30,
            # Exhausted: the counter already reached the 50GB that was granted on top of a
            # used-up cycle. Not exhausted: 20GB of the 50GB is still available.
            previous_volume_limit_bytes=(50 if exhausted else 100) * GB,
            new_volume_limit_bytes=100 * GB,
            previous_remaining_bytes=(0 if exhausted else 80) * GB,
            granted_volume_bytes=50 * GB,
            previous_expiry_ms=to_ms(renewed_at),
            new_expiry_ms=to_ms(renewed_at + timedelta(days=30)),
            renewed_at=renewed_at)
        for offset in range(31):
            observed = datetime.utcnow() - timedelta(days=offset)
            used = int((3.75 if offset < 8 else 1.3) * GB)
            db.session.add(UsageDaily(
                server_id=self.server.id, sub_id=sub_id,
                usage_date=date.today() - timedelta(days=offset),
                upload_bytes=0, download_bytes=used,
                opening_upload_bytes=0, opening_download_bytes=0,
                closing_upload_bytes=0, closing_download_bytes=used,
                sample_count=1, first_observed_at=observed, last_observed_at=observed))
        # Deliberately stale telemetry so the stale counter has something to record.
        total_gb = 100 if exhausted else 60
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0,
            download_bytes=total_gb * GB, total_bytes=total_gb * GB,
            observed_at=datetime.utcnow() - timedelta(minutes=45)))
        db.session.commit()
        return sub_id

    def test_a_built_recommendation_is_counted_and_timed(self):
        sub_id = self._seed_and_build()
        with mock.patch.object(observability.logger, 'info') as log:
            payload = build_recommendation_v5(self.server.id, sub_id, PACKAGES)
        self.assertIsNotNone(payload)
        snapshot = observability.snapshot()
        self.assertEqual(snapshot['recommendation_total'], 1)
        self.assertEqual(snapshot['cycle_available_total'], 1)
        self.assertEqual(snapshot['stale_telemetry_total'], 1)
        self.assertEqual(snapshot['basis'].get('current_cycle_dominant'), 1)
        self.assertEqual(snapshot['trend'].get('strong_increase'), 1)
        self.assertGreater(snapshot['latency']['samples'], 0)
        self.assertGreater(snapshot['latency']['p95_ms'], 0.0)
        self.assertTrue(log.called)

    def test_the_log_line_carries_the_rfp_fields_and_no_pii(self):
        sub_id = self._seed_and_build()
        with mock.patch.object(observability.logger, 'info') as log:
            build_recommendation_v5(self.server.id, sub_id, PACKAGES)
        template = log.call_args[0][0]
        args = log.call_args[0][1:]
        message = template % args
        for field in ('model=', 'server_id=', 'account=acct-', 'cycle_days=', 'cycle_rate=',
                      'rolling_rate=', 'trend_ratio=', 'forecast=', 'recommended_package=',
                      'confidence=', 'basis=', 'latency_ms='):
            self.assertIn(field, message, field)
        self.assertNotIn('@', message)
        self.assertNotIn('09121234567', message)
        self.assertIsNone(re.search(r'\b09\d{9}\b', message))

    def test_an_exhausted_quota_increments_its_own_counter(self):
        sub_id = self._seed_and_build(exhausted=True)
        build_recommendation_v5(self.server.id, sub_id, PACKAGES)
        snapshot = observability.snapshot()
        self.assertEqual(snapshot['early_exhaustion_total'], 1)

    def test_errors_are_counted_without_raising(self):
        observability.observe(server_id=1, account='acct', error=RuntimeError('boom'))
        snapshot = observability.snapshot()
        self.assertEqual(snapshot['recommendation_total'], 1)
        self.assertEqual(snapshot['recommendation_errors_total'], 1)

    def test_the_shadow_counter_advances(self):
        observability.note_shadow_comparison()
        self.assertEqual(observability.snapshot()['shadow_comparisons_total'], 1)

    def test_the_latency_window_is_bounded(self):
        for index in range(observability.MAX_LATENCY_SAMPLES + 50):
            observability.observe(latency_ms=float(index))
        snapshot = observability.snapshot()
        self.assertEqual(snapshot['latency']['samples'], observability.MAX_LATENCY_SAMPLES)

    def test_the_doctor_endpoint_exposes_the_block(self):
        from panel.routes import doctor as doctor_module
        source = open(doctor_module.__file__.replace('.pyc', '.py'), encoding='utf-8').read()
        self.assertIn("checks['usage_intelligence']", source)
        self.assertIn('recommendation_mode()', source)


if __name__ == '__main__':
    unittest.main()
