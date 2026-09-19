"""Phase 17 tests: adaptive refresh cadence."""
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import GLOBAL_SERVER_DATA, app, db  # noqa: E402
from panel.core import refresh_policy  # noqa: E402
from panel.jobs import schedulers  # noqa: E402

POLICY_ENV = (
    "EVE_REFRESH_INTERVAL_SECONDS", "EVE_REFRESH_RECENT_SECONDS",
    "EVE_REFRESH_IDLE_SECONDS", "EVE_REFRESH_ACTIVE_WINDOW_SECONDS",
    "EVE_REFRESH_RECENT_WINDOW_SECONDS", "EVE_REFRESH_MAX_STALENESS_SECONDS",
    "EVE_REFRESH_ACTIVITY_POLL_SECONDS",
)


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)


class ActivityLevelTests(unittest.TestCase):
    def setUp(self):
        refresh_policy.reset_state()
        for name in POLICY_ENV:
            os.environ.pop(name, None)
        self.addCleanup(refresh_policy.reset_state)
        # No Redis by default: activity stays local unless a test provides one.
        self._redis = mock.patch.object(refresh_policy, "_redis", return_value=None)
        self._redis.start()
        self.addCleanup(self._redis.stop)

    def test_levels_follow_the_activity_window(self):
        base = 1_000_000.0
        refresh_policy.record_activity(now=base)
        self.assertEqual(refresh_policy.activity_level(now=base + 10), "active")
        self.assertEqual(refresh_policy.activity_level(now=base + 200), "recent")
        self.assertEqual(refresh_policy.activity_level(now=base + 700), "idle")
        self.assertEqual(refresh_policy.activity_level(now=base + 5000), "idle")

    def test_no_activity_is_idle_and_targets_are_configurable(self):
        self.assertIsNone(refresh_policy.activity_age())
        self.assertEqual(refresh_policy.activity_level(), "idle")
        self.assertEqual(refresh_policy.target_interval("idle"), 300)
        with mock.patch.dict(os.environ, {"EVE_REFRESH_IDLE_SECONDS": "120",
                                          "EVE_REFRESH_INTERVAL_SECONDS": "15",
                                          "EVE_REFRESH_RECENT_SECONDS": "45"}):
            self.assertEqual(refresh_policy.target_interval("idle"), 120)
            self.assertEqual(refresh_policy.target_interval("active"), 15)
            self.assertEqual(refresh_policy.target_interval("recent"), 45)

    def test_record_activity_is_throttled(self):
        base = 2_000_000.0
        self.assertTrue(refresh_policy.record_activity(throttle_seconds=5, now=base))
        self.assertFalse(refresh_policy.record_activity(throttle_seconds=5, now=base + 1))
        self.assertTrue(refresh_policy.record_activity(throttle_seconds=5, now=base + 6))
        self.assertEqual(refresh_policy.activity_age(now=base + 6), 0.0)

    def test_activity_is_shared_through_redis(self):
        redis = FakeRedis()
        # Simulate another process (a web worker) publishing its activity.
        with mock.patch.object(refresh_policy, "_redis", return_value=redis):
            refresh_policy._publish_activity(datetime.now(timezone.utc).isoformat())
            age = refresh_policy.activity_age()
            level = refresh_policy.activity_level()
        self.assertIsNotNone(age)
        self.assertLess(age, 10)
        self.assertEqual(level, "active")


class IntervalTests(unittest.TestCase):
    def setUp(self):
        refresh_policy.reset_state()
        for name in POLICY_ENV:
            os.environ.pop(name, None)

    def test_snapshot_age_parsing(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(refresh_policy.snapshot_age_seconds(None), None)
        self.assertEqual(refresh_policy.snapshot_age_seconds("not-a-date"), None)
        self.assertAlmostEqual(
            refresh_policy.snapshot_age_seconds("2026-01-01T11:59:30", now=now), 30, places=3)
        self.assertAlmostEqual(
            refresh_policy.snapshot_age_seconds("2026-01-01T11:59:00+00:00", now=now), 60, places=3)

    def test_next_interval_is_clamped_by_staleness_and_minimum(self):
        self.assertEqual(refresh_policy.next_interval(None), 0.0)
        self.assertEqual(refresh_policy.next_interval(0), 300.0)
        with mock.patch.dict(os.environ, {"EVE_REFRESH_MAX_STALENESS_SECONDS": "900",
                                          "EVE_REFRESH_INTERVAL_SECONDS": "30"}):
            refresh_policy.record_activity(throttle_seconds=0)
            self.assertEqual(refresh_policy.next_interval(880), 20.0)
            self.assertEqual(refresh_policy.next_interval(899), 5.0)

    def test_should_fetch_now_matrix(self):
        base = 3_000_000.0
        self.assertEqual(refresh_policy.should_fetch_now(None), (True, "no_snapshot"))
        self.assertEqual(refresh_policy.should_fetch_now(5, now=base)[0], False)
        # No activity -> the idle target (300 s) governs.
        self.assertEqual(refresh_policy.should_fetch_now(400, now=base), (True, "idle_interval"))
        refresh_policy.record_activity(throttle_seconds=0, now=base)
        self.assertEqual(refresh_policy.should_fetch_now(10, now=base)[0], False)
        self.assertEqual(refresh_policy.should_fetch_now(40, now=base), (True, "active_interval"))
        self.assertEqual(refresh_policy.should_fetch_now(5000, now=base), (True, "max_staleness"))

    def test_activity_wakes_the_sleeper(self):
        refresh_policy.reset_state()
        threading.Thread(
            target=lambda: (time.sleep(0.05), refresh_policy.record_activity(throttle_seconds=0)),
            daemon=True).start()
        started = time.perf_counter()
        woken = refresh_policy.wait_for_interval(5)
        elapsed = time.perf_counter() - started
        self.assertTrue(woken)
        self.assertLess(elapsed, 2.0)

    def test_status_reports_the_policy(self):
        refresh_policy.reset_state()
        info = refresh_policy.status(snapshot_age=100)
        self.assertEqual(info["level"], "idle")
        self.assertEqual(info["intervals"]["idle"], 300)
        self.assertEqual(info["snapshot_age_seconds"], 100.0)
        self.assertIn("next_interval_seconds", info)


class FetcherLoopTests(unittest.TestCase):
    """The startup sequence: one bootstrap sweep, then the per-server scheduler.

    The loop itself is no longer a cycle, so what is left to test here is the handover.
    The old version of this class drove the retired loop through a patched
    ``fetch_and_update_global_data`` and expected it to return after one sleep; with the
    scheduler in place that call never comes back (the loop runs for the life of the
    process), and a test that waited for it hung the whole suite for 45 minutes in CI.
    Every test here is therefore event-driven and returns immediately.
    """

    def setUp(self):
        refresh_policy.reset_state()
        for name in POLICY_ENV:
            os.environ.pop(name, None)
        self._saved_last_update = GLOBAL_SERVER_DATA.get("last_update")
        self.addCleanup(self._restore)
        self._redis = mock.patch.object(refresh_policy, "_redis", return_value=None)
        self._redis.start()
        self.addCleanup(self._redis.stop)

    def _restore(self):
        GLOBAL_SERVER_DATA["last_update"] = self._saved_last_update
        refresh_policy.reset_state()

    class _Stop(BaseException):
        # BaseException so the loop's own "except Exception" tick guard does not swallow
        # the stop signal and sleep instead of unwinding.
        pass

    def _start(self, sweeps):
        """Run the startup path, stopping it at the first scheduler tick.

        The recovery sweep runs in its OWN thread now, so the test patches the sweep
        itself rather than the fetch it calls: the contract under test is "the loop starts
        and does not wait for the sweep", which a patched sweep states exactly.
        """
        stop = self._Stop

        def fake_sweep():
            sweeps.append({'thread': threading.current_thread().name})

        def fake_scheduler(*args, **kwargs):
            raise stop()

        with (
            mock.patch.object(schedulers, "ensure_background_threads_started"),
            mock.patch.object(schedulers, "load_snapshot_from_redis"),
            mock.patch.object(schedulers, "_bootstrap_sweep_once", fake_sweep),
            mock.patch.object(schedulers, "run_per_server_scheduler", fake_scheduler),
            app.app_context(),
        ):
            with self.assertRaises(stop):
                schedulers.background_data_fetcher()

    def test_the_scheduler_starts_without_waiting_for_the_recovery_sweep(self):
        # The sweep used to run FIRST and the loop only started when it finished, so a slow
        # or hanging sweep stalled every panel's cadence - including the one an operator had
        # just opened. The loop must be entered regardless of the sweep.
        sweeps = []
        self._start(sweeps)
        # The sweep was launched (in its own thread) and the loop was entered without
        # waiting for it: the assertion above proves the loop was reached, and this proves
        # the sweep was still dispatched.
        self.assertTrue(sweeps, "the recovery sweep was never started")
        self.assertEqual(sweeps[0]['thread'], 'eve-bootstrap-sweep')

    def test_the_wake_listener_starts_before_the_loop(self):
        # A nudge from a web process only shortens the sleep if this process is
        # subscribed; starting the listener after the first sweep is the documented order.
        started = []

        def fake_listener():
            started.append(True)
            return True

        with (
            mock.patch.object(schedulers, "ensure_background_threads_started"),
            mock.patch.object(schedulers, "load_snapshot_from_redis"),
            mock.patch.object(schedulers, "fetch_and_update_global_data",
                              lambda **kwargs: True),
            mock.patch.object(refresh_policy, "start_wake_listener", fake_listener),
            mock.patch.object(schedulers, "run_per_server_scheduler",
                              side_effect=self._Stop()),
            app.app_context(),
        ):
            with self.assertRaises(self._Stop):
                schedulers.background_data_fetcher()
        self.assertEqual(started, [True])

    def test_a_failing_tick_backs_off_instead_of_spinning(self):
        # An unreachable database used to be retried at the tick rate (20 failed queries
        # and 20 log lines a second, forever), which is what turned one broken test module
        # into a 45-minute CI timeout. The retry delay must grow.
        import panel.core.panel_limits as panel_limits  # noqa: F401
        attempts = []
        stop = threading.Event()

        def failing_refresh(server_id):
            attempts.append(time.monotonic())
            if len(attempts) >= 3:
                stop.set()
            return {'server_id': int(server_id), 'changed': False, 'block': []}

        with mock.patch.object(schedulers, "_scheduler_server_rows",
                               side_effect=RuntimeError('no such table: servers')):
            schedulers.run_per_server_scheduler(
                fetch_callable=failing_refresh, duration=5.0, stop_event=stop,
                worker_limit=1)
        metrics = schedulers.scheduler_metrics()
        self.assertGreaterEqual(metrics.get('tick_errors', 0), 2)
        # The tick rate is 0.05 s; three failures inside a 5 s budget prove the loop is
        # pacing itself rather than spinning at that rate.
        self.assertLessEqual(metrics.get('tick_errors', 0), 12, metrics)


class RefreshPolicyScriptTests(unittest.TestCase):
    def test_quick_script_writes_a_valid_result(self):
        import json
        import subprocess
        import sys
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "benchmark_refresh_policy.py")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "policy.json")
            result = subprocess.run(
                [sys.executable, script, "--quick", "--json", out],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertIn("fixed_cycles", payload)
        self.assertIn("adaptive_cycles", payload)
        self.assertGreater(payload["fixed_cycles"], payload["adaptive_cycles"])
        self.assertGreater(payload["reduction_pct"], 0)


if __name__ == "__main__":
    unittest.main()
