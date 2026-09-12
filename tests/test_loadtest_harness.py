"""Phase 27 tests: the load-test harness."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOADTEST = os.path.join(REPO_ROOT, "scripts", "loadtest.py")


def _load_harness():
    spec = importlib.util.spec_from_file_location("loadtest", LOADTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class EngineTests(unittest.TestCase):
    """The arrival-rate engine, without a Flask app."""

    @classmethod
    def setUpClass(cls):
        cls.harness = _load_harness()

    def test_a_fixed_rate_is_approximated_and_counted(self):
        scenarios = [
            ("one", "first", lambda clients: _Response(200)),
            ("two", "second", lambda clients: _Response(200)),
        ]
        result = self.harness.run_load(
            scenarios, rate=40, duration=0.5, workers=2,
            clients_factory=lambda: {})
        self.assertGreaterEqual(result["total_requests"], 5)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["rejected"], 0)
        for name in ("one", "two"):
            self.assertGreater(result["scenarios"][name]["requests"], 0)
            self.assertEqual(result["scenarios"][name]["status_counts"], {"200": result["scenarios"][name]["requests"]})

    def test_percentiles_are_ordered(self):
        result = self.harness.run_load(
            [("one", "first", lambda clients: _Response(200))],
            rate=60, duration=0.4, workers=3, clients_factory=lambda: {})
        row = result["scenarios"]["one"]
        self.assertLessEqual(row["p50_ms"], row["p95_ms"])
        self.assertLessEqual(row["p95_ms"], row["p99_ms"])
        self.assertLessEqual(row["p99_ms"], row["max_ms"])

    def test_transport_failures_are_counted_as_errors(self):
        def boom(clients):
            raise ConnectionError("down")
        result = self.harness.run_load(
            [("broken", "always fails", boom)],
            rate=30, duration=0.3, workers=2, clients_factory=lambda: {})
        self.assertGreater(result["scenarios"]["broken"]["errors"], 0)
        self.assertEqual(result["error_rate"], 1.0)
        self.assertIn("0", result["scenarios"]["broken"]["status_counts"])

    def test_client_errors_are_not_server_errors(self):
        result = self.harness.run_load(
            [("denied", "401", lambda clients: _Response(429))],
            rate=30, duration=0.3, workers=2, clients_factory=lambda: {})
        row = result["scenarios"]["denied"]
        self.assertEqual(row["errors"], 0)
        self.assertEqual(row["rejected"], row["requests"])


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out_path = os.path.join(cls._tmp.name, "loadtest.json")
        env = dict(os.environ)
        env["EVE_BENCH_DATABASE_URL"] = os.path.join(cls._tmp.name, "load.db")
        env["FLASK_ENV"] = "development"
        env["DISABLE_BACKGROUND_THREADS"] = "1"
        result = subprocess.run(
            [sys.executable, LOADTEST, "--quick", "--disable-limits",
             "--rate", "20", "--duration", "2", "--workers", "3",
             "--json", cls.out_path],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=900)
        cls.returncode = result.returncode
        cls.output = (result.stdout or "") + (result.stderr or "")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_quick_run_succeeds_and_writes_a_report(self):
        self.assertEqual(self.returncode, 0, self.output)
        self.assertTrue(os.path.isfile(self.out_path))

    def test_report_has_the_expected_shape(self):
        with open(self.out_path, encoding="utf-8") as handle:
            report = json.load(handle)
        expected = ("generated_at", "app_version", "git_sha", "python", "platform",
                    "target", "target_rate", "duration_seconds", "workers",
                    "dataset", "concurrency", "wall_seconds", "total_requests",
                    "achieved_rps", "errors", "error_rate", "rejected",
                    "rejected_rate", "scenarios")
        missing = [key for key in expected if key not in report]
        self.assertEqual(missing, [], "%s\nreport=%s" % (self.output, sorted(report)))
        self.assertEqual(report["target"], "in-process wsgi", self.output)
        self.assertEqual(float(report["target_rate"]), 20.0, self.output)
        # The role is whatever PROCESS_ROLE the deployment sets; only its presence
        # and type are part of the contract.
        self.assertIsInstance(report["concurrency"].get("process_role"), str, self.output)
        self.assertIn("panel_concurrency", report["concurrency"], self.output)
        for name in ("html_login", "api_refresh_delta", "static_style",
                     "api_permissions"):
            self.assertIn(name, report["scenarios"], sorted(report["scenarios"]))

    def test_target_rate_is_approximated_without_errors(self):
        with open(self.out_path, encoding="utf-8") as handle:
            report = json.load(handle)
        # 20/s for 2s is 40 offered requests; allow generous scheduling slack on a
        # busy or virtualised machine, but a stalled generator must still fail.
        self.assertGreaterEqual(report["total_requests"], 8, report)
        self.assertEqual(report["errors"], 0, report["scenarios"])
        self.assertEqual(report["rejected"], 0, report["scenarios"])
        self.assertEqual(report["error_rate"], 0.0, report)


if __name__ == "__main__":
    unittest.main()
