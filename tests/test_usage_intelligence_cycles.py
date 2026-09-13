"""Cycle and window metrics (RFP sections 11-14, acceptance scenario 69).

The numbers here are the ones the reported bug is about: a cycle rate must be measured over
the *precise* elapsed time since the verified renewal, and a 12-hour-old cycle must not be
divided by a whole day.
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import RenewalEvent, Server, UsageCounterState, UsageDaily, app, db  # noqa: E402
from panel.services.usage_intelligence import cycles, metrics  # noqa: E402
from panel.services.usage_intelligence import record_verified_renewal  # noqa: E402
from panel.services.usage_intelligence.schemas import (  # noqa: E402
    MIN_EFFECTIVE_ELAPSED_DAYS, maturity_for,
)

GB = 1024 ** 3


class CycleMetricsTests(unittest.TestCase):
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
        self.server = Server(name='cycles', host='https://cycles.invalid',
                             username='u', password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()

    def _daily(self, sub_id, days_ago, gb, *, hour_offset=0):
        observed = datetime.utcnow() - timedelta(days=days_ago) + timedelta(hours=hour_offset)
        used = int(gb * GB)
        row = UsageDaily(
            server_id=self.server.id, sub_id=sub_id,
            usage_date=date.today() - timedelta(days=days_ago),
            upload_bytes=0, download_bytes=used,
            opening_upload_bytes=0, opening_download_bytes=0,
            closing_upload_bytes=0, closing_download_bytes=used,
            sample_count=2, first_observed_at=observed, last_observed_at=observed,
        )
        db.session.add(row)
        return row

    def _state(self, sub_id, total_gb, *, minutes_ago=1):
        state = UsageCounterState(
            server_id=self.server.id, sub_id=sub_id,
            upload_bytes=0, download_bytes=int(total_gb * GB),
            total_bytes=int(total_gb * GB),
            observed_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
        )
        db.session.add(state)
        return state

    def _renewal(self, sub_id, *, days_ago, previous_limit_gb, previous_remaining_gb,
                 granted_gb, new_limit_gb, traffic_reset=False, operation_id=None):
        event = record_verified_renewal(
            server_id=self.server.id, sub_id=sub_id,
            operation_id=operation_id or 'op-%s' % sub_id,
            previous_volume_limit_bytes=int(previous_limit_gb * GB),
            new_volume_limit_bytes=int(new_limit_gb * GB),
            previous_remaining_bytes=int(previous_remaining_gb * GB),
            granted_volume_bytes=int(granted_gb * GB),
            previous_expiry_ms=1_700_000_000_000, new_expiry_ms=1_702_678_400_000,
            traffic_reset=traffic_reset,
            renewed_at=datetime.utcnow() - timedelta(days=days_ago),
        )
        db.session.commit()
        return event

    # ── current cycle ────────────────────────────────────────────────────────

    def test_without_a_verified_renewal_there_is_no_current_cycle(self):
        result = cycles.build_current_cycle(self.server.id, 'no-cycle')
        self.assertFalse(result.available)
        self.assertEqual(result.reason, 'no_verified_renewal')
        self.assertEqual(result.to_dict()['available'], False)

    def test_the_golden_scenario_uses_the_cycle_rate_not_the_rolling_average(self):
        """RFP section 46/69: 8 days, 30GB after renewal, 60GB in the rolling 31 days."""
        sub_id = 'golden'
        # Before the renewal: 50GB cap, 20GB unused → the counter read 30GB at the boundary.
        self._renewal(sub_id, days_ago=8, previous_limit_gb=50, previous_remaining_gb=20,
                      granted_gb=50, new_limit_gb=100)
        # The counter now reads 60GB: 30GB of it happened inside this cycle.
        self._state(sub_id, 60)
        # 31 days of history totalling 60GB, including the 30GB since the renewal.
        for offset, gb in enumerate([2, 2, 2, 2, 2, 2, 2, 2, 4, 4, 4, 4, 4, 4, 4, 4, 2, 2, 2, 2, 2]):
            self._daily(sub_id, offset, gb)
        db.session.commit()

        rolling = cycles.build_rolling_window(self.server.id, sub_id)
        cycle = cycles.build_current_cycle(self.server.id, sub_id,
                                           live_usage={'total_bytes': 60 * GB,
                                                       'observed_at': datetime.utcnow()})

        self.assertTrue(cycle.available)
        self.assertEqual(cycle.usage_source, 'counter_delta')
        self.assertAlmostEqual(cycle.usage_bytes / GB, 30.0, places=1)
        self.assertGreater(cycle.elapsed_days, 7.9)
        self.assertLess(cycle.elapsed_days, 8.1)
        # 30GB / ~8 days ≈ 3.75 GB/day, not 60/31 ≈ 1.94.
        self.assertAlmostEqual(cycle.average_daily_gb, 30.0 / cycle.elapsed_days, places=2)
        self.assertGreater(cycle.average_daily_gb, 3.6)
        self.assertLess(cycle.average_daily_gb, 3.9)
        self.assertGreater(cycle.projected_31d_gb, 110.0)
        # The rolling window still exists and is smaller: neither replaces the other.
        self.assertTrue(rolling.available)
        self.assertGreater(rolling.basis_days, 0)
        self.assertLess(rolling.average_daily_gb, cycle.average_daily_gb)

    def test_a_twelve_hour_cycle_is_not_divided_by_a_whole_day(self):
        sub_id = 'halfday'
        self._renewal(sub_id, days_ago=0.5, previous_limit_gb=10, previous_remaining_gb=5,
                      granted_gb=10, new_limit_gb=20)
        # The counter read 5GB at the boundary and 10GB now: 5GB inside this cycle.
        self._state(sub_id, 10)
        db.session.commit()

        cycle = cycles.build_current_cycle(self.server.id, sub_id)
        self.assertTrue(cycle.available)
        self.assertAlmostEqual(cycle.elapsed_days, 0.5, places=1)
        # 5GB / 0.5 days = 10 GB/day, not 5GB / 1 day.
        self.assertAlmostEqual(cycle.average_daily_gb, 10.0, places=1)
        self.assertEqual(cycle.maturity, 'very_early')

    def test_minutes_old_evidence_is_floored_before_it_becomes_a_forecast(self):
        sub_id = 'fresh'
        # Freshly renewed with nothing consumed before it: the anchor is 0 and only 1GB
        # has been used since - a rate that only the elapsed floor keeps sane.
        self._renewal(sub_id, days_ago=0.01, previous_limit_gb=10, previous_remaining_gb=10,
                      granted_gb=10, new_limit_gb=20)
        self._state(sub_id, 1)
        db.session.commit()

        cycle = cycles.build_current_cycle(self.server.id, sub_id)
        self.assertEqual(cycle.effective_elapsed_days, MIN_EFFECTIVE_ELAPSED_DAYS)
        self.assertEqual(cycle.maturity, 'insufficient')
        self.assertAlmostEqual(cycle.usage_bytes / GB, 1.0, places=1)
        # 1GB over the 0.25-day floor, not over ~14 minutes.
        self.assertAlmostEqual(cycle.average_daily_gb, 4.0, places=1)

    def test_a_traffic_reset_cycle_reads_the_counter_directly(self):
        sub_id = 'reset'
        self._renewal(sub_id, days_ago=3, previous_limit_gb=50, previous_remaining_gb=0,
                      granted_gb=50, new_limit_gb=50, traffic_reset=True)
        self._state(sub_id, 12)   # the counter restarted, so 12GB are the cycle's usage
        db.session.commit()

        cycle = cycles.build_current_cycle(self.server.id, sub_id)
        self.assertEqual(cycle.usage_source, 'counter_delta')
        self.assertAlmostEqual(cycle.usage_bytes / GB, 12.0, places=1)
        self.assertTrue(cycle.traffic_reset)
        self.assertEqual(cycle.maturity, 'early')

    def test_without_an_anchor_the_daily_rows_since_the_boundary_are_summed(self):
        sub_id = 'unanchored'
        # An unlimited account: no previous limit, so no counter anchor exists.
        record_verified_renewal(
            server_id=self.server.id, sub_id=sub_id, operation_id='op-unanchored',
            previous_volume_limit_bytes=0, new_volume_limit_bytes=0,
            previous_remaining_bytes=None, granted_volume_bytes=0,
            previous_expiry_ms=1, new_expiry_ms=0,
            renewed_at=datetime.utcnow() - timedelta(days=4))
        db.session.commit()
        self._daily(sub_id, 1, 5)
        self._daily(sub_id, 2, 5)
        self._state(sub_id, 5)
        db.session.commit()

        cycle = cycles.build_current_cycle(self.server.id, sub_id)
        self.assertTrue(cycle.available)
        self.assertEqual(cycle.usage_source, 'daily_sum')
        self.assertAlmostEqual(cycle.usage_bytes / GB, 10.0, places=1)

    def test_a_counter_that_moved_backwards_is_a_reset_not_a_negative_cycle(self):
        sub_id = 'backwards'
        self._renewal(sub_id, days_ago=2, previous_limit_gb=50, previous_remaining_gb=10,
                      granted_gb=50, new_limit_gb=100)
        self._state(sub_id, 3)   # counter restarted mid-cycle: 3GB is all of it
        db.session.commit()
        cycle = cycles.build_current_cycle(self.server.id, sub_id)
        self.assertAlmostEqual(cycle.usage_bytes / GB, 3.0, places=1)
        self.assertGreaterEqual(cycle.average_daily_gb, 0.0)

    def test_an_unverified_reset_never_anchors_the_cycle(self):
        sub_id = 'unverified'
        db.session.add(RenewalEvent(
            server_id=self.server.id, sub_id=sub_id, renewed_at=datetime.utcnow(),
            event_type='inferred_reset', source='counter_reset', verified=False))
        db.session.commit()
        self._daily(sub_id, 1, 20)
        db.session.commit()
        result = cycles.build_current_cycle(self.server.id, sub_id)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, 'no_verified_renewal')

    def test_maturity_buckets_follow_the_rfp_table(self):
        self.assertEqual(maturity_for(0.1), 'insufficient')
        self.assertEqual(maturity_for(0.5), 'very_early')
        self.assertEqual(maturity_for(3), 'early')
        self.assertEqual(maturity_for(10), 'medium')
        self.assertEqual(maturity_for(14), 'mature')
        self.assertEqual(maturity_for(40), 'mature')

    # ── rolling window and baseline ──────────────────────────────────────────

    def test_the_rolling_window_carries_its_own_basis_and_rate(self):
        sub_id = 'rolling'
        for offset, gb in enumerate([1, 1, 2, 2, 2]):     # 8GB over 5 observed days
            self._daily(sub_id, offset, gb)
        db.session.commit()

        window = cycles.build_rolling_window(self.server.id, sub_id)
        self.assertTrue(window.available)
        self.assertAlmostEqual(window.usage_bytes / GB, 8.0, places=1)
        self.assertEqual(window.basis_days, 5.0)
        self.assertEqual(window.observed_dates, 5)
        self.assertAlmostEqual(window.average_daily_gb, 1.6, places=2)
        self.assertAlmostEqual(window.projected_31d_gb, 49.6, places=1)

    def test_the_baseline_is_the_window_before_the_cycle_started(self):
        sub_id = 'baseline'
        self._renewal(sub_id, days_ago=10, previous_limit_gb=50, previous_remaining_gb=0,
                      granted_gb=50, new_limit_gb=50, operation_id='op-baseline')
        # Before the boundary: 4GB/day for five days. After it: 12GB/day for three days.
        for offset in range(11, 16):
            self._daily(sub_id, offset, 4)
        for offset in range(1, 4):
            self._daily(sub_id, offset, 12)
        self._state(sub_id, 36)
        db.session.commit()

        from panel.services.usage_intelligence import latest_cycle_boundary
        boundary = latest_cycle_boundary(self.server.id, sub_id)
        baseline = cycles.build_historical_baseline(
            self.server.id, sub_id, cycle_start=boundary.renewed_at)
        self.assertTrue(baseline.available)
        self.assertAlmostEqual(baseline.usage_bytes / GB, 20.0, places=1)
        self.assertEqual(baseline.basis_days, 5.0)
        self.assertAlmostEqual(baseline.average_daily_gb, 4.0, places=2)

    def test_a_window_with_no_rows_is_unavailable_not_zero_rate(self):
        window = cycles.build_rolling_window(self.server.id, 'nothing')
        self.assertFalse(window.available)
        self.assertEqual(window.usage_bytes, 0)
        self.assertEqual(window.average_daily_gb, 0.0)
        self.assertEqual(window.to_dict()['available'], False)


class TelemetryFreshnessTests(unittest.TestCase):
    def test_freshness_thresholds(self):
        now = datetime.utcnow()
        self.assertEqual(metrics.freshness(now - timedelta(minutes=2), now=now)[0], 'fresh')
        self.assertEqual(metrics.freshness(now - timedelta(minutes=10), now=now)[0],
                         'acceptable')
        self.assertEqual(metrics.freshness(now - timedelta(minutes=45), now=now)[0], 'stale')
        self.assertEqual(metrics.freshness(None, now=now)[0], 'unknown')

    def test_counter_delta_is_reset_safe(self):
        self.assertEqual(metrics.counter_delta(100, 40), 60)
        self.assertEqual(metrics.counter_delta(30, 40), 30)
        self.assertEqual(metrics.counter_delta(None, 40), 0)


if __name__ == '__main__':
    unittest.main()
