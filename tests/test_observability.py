"""Phase 26 tests: request correlation and in-process HTTP metrics."""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from flask import g  # noqa: E402

from app import Admin, app, db, internal_server_error  # noqa: E402
from panel.core import http_metrics  # noqa: E402


class RequestIdTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_every_response_carries_a_request_id(self):
        for path in ("/login", "/api/does-not-exist"):
            response = self.client.get(path)
            value = response.headers.get("X-Request-ID")
            self.assertTrue(value, path)
            self.assertLessEqual(len(value), 64)
        # The login page is tightly rate limited (10/minute); the shared test
        # fixture clears the limiter before each test, so this is deterministic.
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_two_requests_get_different_ids(self):
        first = self.client.get("/login").headers.get("X-Request-ID")
        second = self.client.get("/login").headers.get("X-Request-ID")
        self.assertNotEqual(first, second)

    def test_a_safe_inbound_id_is_echoed(self):
        response = self.client.get("/login", headers={"X-Request-ID": "trace-42.abc_1"})
        self.assertEqual(response.headers.get("X-Request-ID"), "trace-42.abc_1")

    def test_a_hostile_inbound_id_is_sanitised(self):
        response = self.client.get("/login", headers={"X-Request-ID": "bad value!!<x>"})
        value = response.headers.get("X-Request-ID")
        self.assertNotEqual(value, "bad value!!<x>")
        self.assertRegex(value, r"^[A-Za-z0-9._:-]+$")

    def test_error_payloads_include_the_request_id(self):
        with app.test_request_context("/api/x", headers={"Accept": "application/json"}):
            g.request_id = "unit-trace-1"
            result = internal_server_error(RuntimeError("boom"))
        response, status = (result if isinstance(result, tuple)
                            else (result, result.status_code))
        self.assertEqual(status, 500)
        self.assertEqual(response.get_json()["request_id"], "unit-trace-1")


class HttpMetricsTests(unittest.TestCase):
    def setUp(self):
        http_metrics.reset()
        self.addCleanup(http_metrics.reset)
        self.client = app.test_client()

    def test_snapshot_starts_empty(self):
        snapshot = http_metrics.snapshot()
        self.assertEqual(snapshot["total_requests"], 0)
        self.assertEqual(snapshot["error_rate"], 0.0)
        self.assertEqual(snapshot["tracked_endpoints"], 0)

    def test_requests_are_tracked_by_endpoint_with_status_classes(self):
        self.client.get("/login")
        self.client.get("/this-path-does-not-exist")
        snapshot = http_metrics.snapshot()
        self.assertEqual(snapshot["total_requests"], 2)
        endpoints = {item["endpoint"] for item in snapshot["busiest"]}
        self.assertIn("GET unmatched", endpoints)
        self.assertEqual(snapshot["error_requests"], 0)
        # A 404 is a client error, not a server error.
        unmatched = [item for item in snapshot["busiest"]
                     if item["endpoint"] == "GET unmatched"][0]
        self.assertEqual(unmatched["client_errors"], 1)
        self.assertEqual(unmatched["errors"], 0)
        self.assertEqual(unmatched["last_status"], 404)

    def test_server_errors_count_towards_the_error_rate(self):
        http_metrics.observe("api.broken", "GET", 500, 12.0)
        http_metrics.observe("api.broken", "GET", 200, 8.0)
        snapshot = http_metrics.snapshot()
        self.assertEqual(snapshot["error_requests"], 1)
        self.assertEqual(snapshot["error_rate"], 0.5)
        self.assertEqual(snapshot["slowest"][0]["endpoint"], "GET api.broken")
        self.assertEqual(snapshot["slowest"][0]["mean_ms"], 10.0)

    def test_slow_requests_are_counted(self):
        http_metrics.observe("api.slow", "GET", 200, http_metrics.SLOW_REQUEST_MS + 1)
        self.assertEqual(http_metrics.snapshot()["slow_requests"], 1)

    def test_the_map_is_bounded(self):
        with mock.patch.object(http_metrics, "MAX_KEYS", 3):
            for index in range(6):
                http_metrics.observe("api.endpoint_%d" % index, "GET", 200, 1.0)
        snapshot = http_metrics.snapshot()
        self.assertLessEqual(snapshot["tracked_endpoints"], 3)
        self.assertEqual(snapshot["total_requests"], 6)

    def test_observe_tolerates_garbage(self):
        http_metrics.observe(None, None, "not-a-status", "not-a-number")
        snapshot = http_metrics.snapshot()
        self.assertEqual(snapshot["total_requests"], 1)
        self.assertIn("GET unmatched", snapshot["busiest"][0]["endpoint"])


class DoctorMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username="metrics-admin", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        db.session.add(cls.admin)
        db.session.commit()
        cls.admin_id = cls.admin.id
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        http_metrics.reset()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = self.admin_id
            sess["role"] = "admin"
            sess["is_superadmin"] = False

    def test_doctor_reports_http_metrics(self):
        self.client.get("/login")
        response = self.client.get("/api/doctor")
        self.assertEqual(response.status_code, 200, response.data)
        check = response.get_json()["checks"]["http_metrics"]
        self.assertIn(check["state"], ("ok", "warning"))
        # The in-flight doctor request is sampled by the after_request hook, so
        # the payload always describes the requests completed before it.
        self.assertGreaterEqual(check["total_requests"], 1)
        self.assertIn("slowest", check)
        self.assertIn("busiest", check)


if __name__ == "__main__":
    unittest.main()
