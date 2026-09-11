"""Phase 24 tests: background worker inventory and singleton ownership."""
import os
import tempfile
import threading
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import Admin, app, db  # noqa: E402
from panel.jobs import schedulers  # noqa: E402


class WorkerRegistryTests(unittest.TestCase):
    def setUp(self):
        self._registry_backup = dict(schedulers._WORKER_REGISTRY)
        self._singleton_backup = dict(schedulers._SINGLETON_LOCK_FDS)
        self._errors_backup = dict(schedulers._SINGLETON_ERRORS)
        self.addCleanup(self._restore)

    def _restore(self):
        schedulers._WORKER_REGISTRY.clear()
        schedulers._WORKER_REGISTRY.update(self._registry_backup)
        schedulers._SINGLETON_LOCK_FDS.clear()
        schedulers._SINGLETON_LOCK_FDS.update(self._singleton_backup)
        schedulers._SINGLETON_ERRORS.clear()
        schedulers._SINGLETON_ERRORS.update(self._errors_backup)

    def test_started_worker_is_recorded_and_alive(self):
        stop = threading.Event()
        self.assertTrue(schedulers._start_worker("unit_worker", stop.wait))
        self.addCleanup(stop.set)
        info = schedulers._WORKER_REGISTRY["unit_worker"]
        self.assertEqual(info["state"], "started")
        self.assertFalse(info["singleton"])
        self.assertTrue(info["thread"].startswith("eve-unit_worker"))
        inventory = schedulers.worker_inventory()
        self.assertEqual(inventory["pid"], os.getpid())
        self.assertTrue(inventory["workers"]["unit_worker"]["alive"])

    def test_a_thread_that_cannot_start_is_recorded_as_failed(self):
        with mock.patch.object(schedulers.threading, "Thread",
                               side_effect=RuntimeError("no threads left")):
            self.assertFalse(schedulers._start_worker("unit_broken", lambda: None))
        info = schedulers._WORKER_REGISTRY["unit_broken"]
        self.assertEqual(info["state"], "failed")
        self.assertIn("no threads left", info["error"])
        self.assertNotIn("unit_broken", schedulers.worker_inventory()["singletons_owned"])

    def test_inventory_reports_role_and_singleton_errors(self):
        inventory = schedulers.worker_inventory()
        self.assertIn("process_role", inventory)
        self.assertIn("singletons_owned", inventory)
        self.assertEqual(inventory["singleton_errors"], {})

    @unittest.skipIf(os.name == "nt", "fcntl locking is POSIX only")
    def test_a_second_claim_of_the_same_singleton_fails(self):
        name = "unit_singleton_%d" % os.getpid()
        self.assertTrue(schedulers._claim_singleton(name))
        self.assertIn(name, schedulers._SINGLETON_LOCK_FDS)
        self.assertIn(name, schedulers.worker_inventory()["singletons_owned"])
        self.assertFalse(schedulers._claim_singleton(name))

    def test_lock_file_failure_fails_open_and_is_recorded(self):
        name = "unit_singleton_fault_%d" % os.getpid()
        with mock.patch("panel.core.runtime_files.open_private_lock",
                        side_effect=PermissionError(13, "read-only directory")):
            if os.name == "nt":
                # No fcntl on Windows: every process fails open already.
                self.assertTrue(schedulers._claim_singleton(name))
                self.assertEqual(schedulers._SINGLETON_ERRORS, {})
            else:
                self.assertTrue(schedulers._claim_singleton(name))
                self.assertIn(name, schedulers._SINGLETON_ERRORS)
        self.assertNotIn(name, schedulers._SINGLETON_LOCK_FDS)


class ThreadBootstrapTests(unittest.TestCase):
    def setUp(self):
        self._flag = schedulers.BACKGROUND_THREADS_STARTED
        schedulers.BACKGROUND_THREADS_STARTED = False
        self.addCleanup(self._restore)

    def _restore(self):
        schedulers.BACKGROUND_THREADS_STARTED = self._flag

    def test_web_role_starts_only_the_snapshot_reader(self):
        with mock.patch.object(schedulers, "_start_worker") as start, \
                mock.patch("app.PROCESS_ROLE", "web"):
            schedulers.ensure_background_threads_started()
        names = [call.args[0] for call in start.call_args_list]
        self.assertEqual(names, ["snapshot_reader"])

    def test_background_role_starts_the_singleton_set(self):
        with mock.patch.object(schedulers, "_start_worker") as start, \
                mock.patch("app.PROCESS_ROLE", "worker"):
            schedulers.ensure_background_threads_started()
        names = {call.args[0] for call in start.call_args_list}
        for expected in ("scheduler", "health_watchdog", "snapshot_worker",
                         "pulse_scheduler", "bnqo_scheduler", "data_fetcher"):
            self.assertIn(expected, names)
        singletons = {call.args[0] for call in start.call_args_list
                      if call.kwargs.get("singleton")}
        self.assertIn("scheduler", singletons)
        self.assertNotIn("data_fetcher", singletons)  # Redis is off in tests

    def test_bootstrap_runs_once_per_process(self):
        with mock.patch.object(schedulers, "_start_worker") as start, \
                mock.patch("app.PROCESS_ROLE", "web"):
            schedulers.ensure_background_threads_started()
            schedulers.ensure_background_threads_started()
        self.assertEqual(start.call_count, 1)
        self.assertTrue(schedulers.BACKGROUND_THREADS_STARTED)


class DoctorWorkerCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username="worker-admin", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        db.session.add(cls.admin)
        db.session.commit()
        cls.client = app.test_client()
        with cls.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = cls.admin.id
            sess["role"] = "admin"
            sess["is_superadmin"] = False

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_doctor_reports_the_worker_inventory(self):
        response = self.client.get("/api/doctor")
        self.assertEqual(response.status_code, 200, response.data)
        check = response.get_json()["checks"]["workers"]
        self.assertEqual(check["state"], "ok")
        self.assertEqual(check["pid"], os.getpid())
        self.assertIn("process_role", check)
        self.assertIn("workers", check)
        self.assertEqual(check["failed"], [])


if __name__ == "__main__":
    unittest.main()
