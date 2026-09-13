"""The one-pass analysis context: bounded queries, exhaustion signals, freshness.

RFP sections 14-16, 20, 35. The baseline metrics land here with the exhaustion and
freshness signals that depend on them, and the query budget is asserted on the real
loader rather than assumed.
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import RenewalEvent, Server, UsageCounterState, UsageDaily, app, db  # noqa: E402
from panel.services.usage_intelligence import analysis, record_verified_renewal  # noqa: E402

GB = 1024 ** 3


class UsageContextTests(unittest.TestCase):
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
        self.server = Server(name='context', host='https://context.invalid',
                             username='u', password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()

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
                 new_limit_gb, days=31, traffic_reset=False):
        renewed_at = datetime.utcnow() - timedelta(days=days_ago)
        # Panel expiries are epoch milliseconds; the project stores naive UTC, so the
        # conversion must treat the naive value as UTC (not as local time).
        def to_ms(value):
            return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)
        event = record_verified_renewal(
            server_id=self.server.id, sub_id=sub_id,
            operation_id='op-%s' % sub_id, days=days,
            previous_volume_limit_bytes=int(limit_gb * GB),
            new_volume_limit_bytes=int(new_limit_gb * GB),
            previous_remaining_bytes=int(remaining_gb * GB),
            granted_volume_bytes=int(granted_gb * GB),
            previous_expiry_ms=to_ms(renewed_at),
            new_expiry_ms=to_ms(renewed_at + timedelta(days=days)),
            traffic_reset=traffic_reset,
            renewed_at=renewed_at,
        )
        db.session.commit()
        return event

    def test_the_context_carries_every_window_and_stays_inside_the_query_budget(self):
        sub_id = 'ctx-1'
        self._renewal(sub_id, days_ago=8, limit_gb=50, remaining_gb=20, granted_gb=50,
                      new_limit_gb=100)
        for offset in range(0, 20):
            self._daily(sub_id, offset, 3)
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0, download_bytes=60 * GB,
            total_bytes=60 * GB, observed_at=datetime.utcnow()))
        db.session.commit()

        context = analysis.load_usage_context(self.server.id, sub_id)
        self.assertTrue(context.has_cycle)
        self.assertTrue(context.cycle.available)
        self.assertTrue(context.rolling.available)
        self.assertLessEqual(context.queries, analysis.QUERY_BUDGET, context.queries)
        self.assertGreater(context.queries, 0)

    def test_the_baseline_is_independent_of_the_cycle_window(self):
        sub_id = 'ctx-2'
        self._renewal(sub_id, days_ago=6, limit_gb=50, remaining_gb=0, granted_gb=50,
                      new_limit_gb=50)
        for offset in range(7, 20):        # before the cycle: ~1GB/day
            self._daily(sub_id, offset, 1)
        for offset in range(0, 6):         # inside the cycle: ~5GB/day
            self._daily(sub_id, offset, 5)
        db.session.commit()

        context = analysis.load_usage_context(self.server.id, sub_id)
        self.assertTrue(context.baseline.available)
        self.assertAlmostEqual(context.baseline.average_daily_gb, 1.0, places=1)
        self.assertGreater(context.cycle.average_daily_gb, 4.0)

    def test_exhaustion_is_flagged_when_the_quota_ran_out_early(self):
        sub_id = 'ctx-exhausted'
        # 31-day package, quota gone after 8 days: 8/31 ≈ 0.26 → critical.
        self._renewal(sub_id, days_ago=8, limit_gb=0, remaining_gb=0, granted_gb=50,
                      new_limit_gb=50, days=31)
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0, download_bytes=50 * GB,
            total_bytes=50 * GB, observed_at=datetime.utcnow()))
        db.session.commit()

        context = analysis.load_usage_context(self.server.id, sub_id)
        self.assertTrue(context.signals.early_exhaustion)
        self.assertEqual(context.signals.exhaustion_severity, 'critical')
        self.assertAlmostEqual(context.signals.exhaustion_ratio, 8.0 / 31.0, places=2)

    def test_a_quota_that_is_still_available_is_not_exhausted(self):
        sub_id = 'ctx-partial'
        self._renewal(sub_id, days_ago=8, limit_gb=0, remaining_gb=0, granted_gb=50,
                      new_limit_gb=50, days=31)
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0, download_bytes=30 * GB,
            total_bytes=30 * GB, observed_at=datetime.utcnow()))
        db.session.commit()

        context = analysis.load_usage_context(self.server.id, sub_id)
        self.assertFalse(context.signals.early_exhaustion)
        self.assertEqual(context.signals.exhaustion_severity, 'normal')

    def test_stale_telemetry_is_reported_as_a_signal(self):
        sub_id = 'ctx-stale'
        self._renewal(sub_id, days_ago=5, limit_gb=50, remaining_gb=0, granted_gb=50,
                      new_limit_gb=50)
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0, download_bytes=5 * GB,
            total_bytes=5 * GB, observed_at=datetime.utcnow() - timedelta(minutes=45)))
        db.session.commit()

        context = analysis.load_usage_context(self.server.id, sub_id)
        self.assertTrue(context.signals.telemetry_stale)
        self.assertEqual(context.signals.telemetry_freshness, 'stale')
        self.assertGreaterEqual(context.signals.telemetry_age_seconds, 30 * 60)

    def test_an_account_with_no_history_still_produces_a_context(self):
        context = analysis.load_usage_context(self.server.id, 'ctx-empty')
        self.assertFalse(context.has_cycle)
        self.assertEqual(context.cycle.reason, 'no_verified_renewal')
        self.assertFalse(context.rolling.available)
        self.assertFalse(context.signals.early_exhaustion)
        self.assertLessEqual(context.queries, analysis.QUERY_BUDGET)

    def test_expected_duration_comes_from_the_event_not_a_guess(self):
        boundary = self._renewal('ctx-duration', days_ago=3, limit_gb=50, remaining_gb=0,
                                 granted_gb=50, new_limit_gb=50, days=30)
        self.assertAlmostEqual(analysis.estimate_expected_duration_days(boundary), 30.0,
                               places=1)
        self.assertIsNone(analysis.estimate_expected_duration_days(None))


if __name__ == '__main__':
    unittest.main()
