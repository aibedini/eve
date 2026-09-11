"""Phase 14 tests: the refresh fan-out must not hold the shared snapshot lock."""
import inspect
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

import app as app_module  # noqa: E402
from app import Server, app, db  # noqa: E402
from panel.core.redis_client import GLOBAL_FETCH_LOCK, GLOBAL_REFRESH_LOCK, fetch_guard  # noqa: E402
from panel.jobs import refresh as refresh_jobs  # noqa: E402
from panel.jobs import schedulers  # noqa: E402

SERVER_IDS = (9201, 9202)


class FetchLockScopingTests(unittest.TestCase):
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
        refresh_jobs.REFRESH_BACKOFF.clear()
        Server.query.filter(Server.id.in_(SERVER_IDS)).delete(synchronize_session=False)
        for server_id in SERVER_IDS:
            db.session.add(Server(id=server_id, name="lock-%d" % server_id,
                                  host="https://lock.invalid", username="u", password="p",
                                  panel_type="auto", enabled=True))
        db.session.commit()
        self.release = threading.Event()
        self.started = threading.Event()

    def _slow_fetch_worker(self, server_dict):
        self.started.set()
        self.release.wait(timeout=10)
        return (server_dict["id"], [], None, None, None, None, "auto")

    def test_the_fan_out_does_not_block_readers(self):
        errors = []
        finished = threading.Event()

        def run_fetch():
            try:
                # Flask contexts are thread-local: the worker thread needs its own,
                # exactly like background_data_fetcher does in production.
                with app.app_context():
                    with mock.patch.object(app_module, "fetch_worker", self._slow_fetch_worker):
                        schedulers.fetch_and_update_global_data(force=True)
            except Exception as exc:  # pragma: no cover - surfaced by the assert
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=run_fetch, daemon=True)
        worker.start()
        self.assertTrue(self.started.wait(timeout=5), "fetch never started")
        try:
            started = time.perf_counter()
            with GLOBAL_REFRESH_LOCK:
                pass
            wait_ms = (time.perf_counter() - started) * 1000.0
        finally:
            self.release.set()
        self.assertTrue(finished.wait(timeout=10), "fetch never finished")
        worker.join(timeout=5)
        self.assertEqual(errors, [])
        # Before this phase the caller held the lock for the whole fan-out; the
        # fetch now blocks a reader only for its short in-memory commits.
        self.assertLess(wait_ms, 200.0, "reader waited %.1f ms for the snapshot lock" % wait_ms)

    def test_second_fetch_is_skipped_while_one_is_running(self):
        calls = []
        finished = threading.Event()

        def counting_fetch_worker(server_dict):
            calls.append(server_dict["id"])
            self.started.set()
            self.release.wait(timeout=10)
            return (server_dict["id"], [], None, None, None, None, "auto")

        def run_fetch():
            try:
                with app.app_context():
                    with mock.patch.object(app_module, "fetch_worker", counting_fetch_worker):
                        schedulers.fetch_and_update_global_data(force=True)
            finally:
                finished.set()

        worker = threading.Thread(target=run_fetch, daemon=True)
        worker.start()
        self.assertTrue(self.started.wait(timeout=5))
        try:
            with mock.patch.object(app_module, "fetch_worker", counting_fetch_worker):
                started = time.perf_counter()
                second = schedulers.fetch_and_update_global_data(
                    force=True, wait_seconds=0)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
        finally:
            self.release.set()
        self.assertFalse(second)
        self.assertLess(elapsed_ms, 100.0)
        self.assertTrue(finished.wait(timeout=10))
        worker.join(timeout=5)

    def test_fetch_guard_reports_a_busy_slot_without_waiting(self):
        with fetch_guard(0) as owns:
            self.assertTrue(owns)
            with fetch_guard(0) as second:
                self.assertFalse(second)
        with fetch_guard(0) as again:
            self.assertTrue(again)

    def test_snapshot_is_still_updated_and_the_flag_is_cleared(self):
        release_event = threading.Event()
        release_event.set()

        def fast_fetch_worker(server_dict):
            return (server_dict["id"], [], {"pairs": set(), "emails": set()},
                    {"xui_version": "3.0"}, None, None, "auto")

        with mock.patch.object(app_module, "fetch_worker", fast_fetch_worker):
            ok = schedulers.fetch_and_update_global_data(force=True)
        self.assertTrue(ok)
        self.assertFalse(schedulers.GLOBAL_SERVER_DATA.get("is_updating"))
        statuses = {row.get("server_id") for row in schedulers.GLOBAL_SERVER_DATA.get("servers_status") or []}
        self.assertTrue(set(SERVER_IDS).issubset(statuses), statuses)

    def test_background_fetcher_does_not_take_the_snapshot_lock(self):
        source = inspect.getsource(schedulers.background_data_fetcher)
        self.assertNotIn("GLOBAL_REFRESH_LOCK", source)
        self.assertIn("fetch_and_update_global_data", source)


class LockBenchmarkScriptTests(unittest.TestCase):
    def test_quick_script_writes_a_valid_result(self):
        import json
        import subprocess
        import sys
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "benchmark_locks.py")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "locks.json")
            result = subprocess.run(
                [sys.executable, script, "--quick", "--json", out],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
        for key in ("servers", "fetch_ms", "reader_worst_block_ms",
                    "reader_mean_block_ms", "lock_sections",
                    "lock_section_max_ms", "lock_section_total_ms"):
            self.assertIn(key, payload)
        self.assertGreater(payload["fetch_ms"], 0)
        self.assertGreaterEqual(payload["lock_sections"], 1)
        # The reader must not be blocked for anything close to the fetch.
        self.assertLess(payload["reader_worst_block_ms"], payload["fetch_ms"] / 2)


if __name__ == "__main__":
    unittest.main()
