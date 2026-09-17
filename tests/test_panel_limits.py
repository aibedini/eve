"""Phase 15 tests: bounded, coalesced panel access."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from panel.core import panel_limits  # noqa: E402


class CoalesceTests(unittest.TestCase):
    def setUp(self):
        panel_limits.reset_panel_metrics()
        os.environ.pop("EVE_PANEL_CONCURRENCY", None)
        os.environ.pop("EVE_PANEL_FETCH_WAIT_SECONDS", None)
        os.environ.pop("EVE_REFRESH_WORKERS", None)
        self.addCleanup(panel_limits.reset_panel_metrics)

    def test_duplicate_callers_run_the_work_once(self):
        executions = []
        results = []
        started = threading.Event()

        def worker():
            with panel_limits.coalesce("server:7") as slot:
                if not slot.leader:
                    results.append(slot.result)
                    return
                started.set()
                deadline = time.monotonic() + 5
                while panel_limits.panel_metrics()["coalesced"] < 4:
                    if time.monotonic() >= deadline:
                        self.fail("followers did not overlap the leader")
                    time.sleep(0.01)
                executions.append(1)
                slot.result = "payload"
                results.append(slot.result)

        threads = []
        for index in range(5):
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
            if index == 0:
                self.assertTrue(started.wait(timeout=2))
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(executions), 1)
        self.assertEqual(results, ["payload"] * 5)
        metrics = panel_limits.panel_metrics()
        self.assertEqual(metrics["started"], 1)
        self.assertEqual(metrics["coalesced"], 4)

    def test_follower_times_out_when_the_leader_is_slow(self):
        release = threading.Event()
        leader_done = threading.Event()

        def leader():
            with panel_limits.coalesce("server:8", wait_seconds=5):
                release.wait(timeout=5)
            leader_done.set()

        thread = threading.Thread(target=leader, daemon=True)
        thread.start()
        time.sleep(0.05)
        with self.assertRaises(panel_limits.PanelBusy):
            with panel_limits.coalesce("server:8", wait_seconds=0.05):
                self.fail("a follower must not run the work")
        release.set()
        self.assertTrue(leader_done.wait(timeout=5))
        self.assertGreaterEqual(panel_limits.panel_metrics()["timed_out"], 1)

    def test_leader_error_is_re_raised_for_followers(self):
        release = threading.Event()
        leader_started = threading.Event()
        leader_error = []
        follower_result = {}

        def leader():
            try:
                with panel_limits.coalesce("server:9", wait_seconds=5):
                    leader_started.set()
                    release.wait(timeout=5)
                    raise RuntimeError("panel unreachable")
            except RuntimeError as exc:
                leader_error.append(str(exc))

        follower_result = {}

        def follower():
            try:
                with panel_limits.coalesce("server:9", wait_seconds=5) as slot:
                    follower_result["leader"] = slot.leader
            except RuntimeError as exc:
                follower_result["error"] = str(exc)

        leader_thread = threading.Thread(target=leader, daemon=True)
        leader_thread.start()
        self.assertTrue(leader_started.wait(timeout=2))
        follower_thread = threading.Thread(target=follower)
        follower_thread.start()
        # Wait until the follower actually joined the flight before letting the
        # leader fail, otherwise the follower becomes a new leader of its own.
        deadline = time.time() + 3
        while time.time() < deadline:
            if panel_limits.panel_metrics()["coalesced"] >= 1:
                break
            time.sleep(0.01)
        else:
            self.fail("follower never coalesced with the leader")
        release.set()
        follower_thread.join(timeout=5)
        leader_thread.join(timeout=5)
        self.assertEqual(leader_error, ["panel unreachable"])
        self.assertEqual(follower_result.get("error"), "panel unreachable")
        # The follower must never have run the work itself.
        self.assertNotIn("leader", follower_result)

    def test_global_cap_bounds_simultaneous_fetches(self):
        os.environ["EVE_PANEL_CONCURRENCY"] = "2"
        self.addCleanup(os.environ.pop, "EVE_PANEL_CONCURRENCY", None)
        self.assertEqual(panel_limits.concurrency_limit(), 2)
        current = {"value": 0, "max": 0}
        lock = threading.Lock()

        def worker(index):
            with panel_limits.coalesce("unique:%d" % index, wait_seconds=5):
                with lock:
                    current["value"] += 1
                    current["max"] = max(current["max"], current["value"])
                time.sleep(0.05)
                with lock:
                    current["value"] -= 1

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertLessEqual(current["max"], 2)
        self.assertEqual(panel_limits.panel_metrics()["in_flight"], 0)

    def test_panel_slot_rejects_when_the_cap_is_held(self):
        os.environ["EVE_PANEL_CONCURRENCY"] = "1"
        self.addCleanup(os.environ.pop, "EVE_PANEL_CONCURRENCY", None)
        release = threading.Event()
        holder_ready = threading.Event()

        def holder():
            with panel_limits.panel_slot(wait_seconds=5):
                holder_ready.set()
                release.wait(timeout=5)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(holder_ready.wait(timeout=2))
        started = time.perf_counter()
        with self.assertRaises(panel_limits.PanelBusy):
            with panel_limits.panel_slot(wait_seconds=0.05):
                self.fail("the cap must reject the second slot")
        self.assertLess((time.perf_counter() - started) * 1000.0, 1000.0)
        release.set()
        thread.join(timeout=5)
        self.assertGreaterEqual(panel_limits.panel_metrics()["rejected"], 1)

    def test_configuration_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"EVE_PANEL_CONCURRENCY": "3",
                                          "EVE_PANEL_FETCH_WAIT_SECONDS": "1.5",
                                          "EVE_REFRESH_WORKERS": "2"}):
            self.assertEqual(panel_limits.concurrency_limit(), 3)
            self.assertEqual(panel_limits.fetch_wait_seconds(), 1.5)
            self.assertEqual(panel_limits.refresh_worker_limit(), 2)
        with mock.patch.dict(os.environ, {"EVE_PANEL_CONCURRENCY": "bad"}):
            self.assertEqual(panel_limits.concurrency_limit(), 12)


class FetchCoalescingTests(unittest.TestCase):
    def setUp(self):
        panel_limits.reset_panel_metrics()
        self.addCleanup(panel_limits.reset_panel_metrics)

    def test_fetch_and_update_server_data_is_single_flight(self):
        from panel.jobs import refresh as refresh_jobs
        calls = []
        started = threading.Event()

        def slow_inner(server_id):
            started.set()
            deadline = time.monotonic() + 5
            while panel_limits.panel_metrics()["coalesced"] < 3:
                if time.monotonic() >= deadline:
                    self.fail("fetch followers did not overlap the leader")
                time.sleep(0.01)
            calls.append(server_id)
            return {"server_id": server_id}

        results = []

        def caller():
            results.append(refresh_jobs.fetch_and_update_server_data(1))

        with mock.patch.object(refresh_jobs, "_fetch_and_update_server_data_inner",
                               side_effect=slow_inner):
            threads = []
            for index in range(4):
                thread = threading.Thread(target=caller)
                threads.append(thread)
                thread.start()
                if index == 0:
                    self.assertTrue(started.wait(timeout=2))
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(calls, [1])
        self.assertEqual(results, [{"server_id": 1}] * 4)
        metrics = panel_limits.panel_metrics()
        self.assertEqual(metrics["started"], 1)
        self.assertEqual(metrics["coalesced"], 3)


class PanelLimitsScriptTests(unittest.TestCase):
    def test_quick_script_writes_a_valid_result(self):
        import json
        import subprocess
        import sys
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "benchmark_panel_limits.py")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "limits.json")
            result = subprocess.run(
                [sys.executable, script, "--quick", "--json", out],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(payload["coalesced_executions"], 1)
        self.assertEqual(payload["unbounded_executions"], payload["callers"])
        self.assertLessEqual(payload["cap_max_concurrency"], payload["cap_limit"])
        self.assertGreaterEqual(payload["cap_limit"], 1)


if __name__ == "__main__":
    unittest.main()
