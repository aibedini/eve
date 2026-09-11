"""Phase 10 tests: the performance baseline harness."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO_ROOT, "scripts", "benchmark_baseline.py")


def _load_harness():
    spec = importlib.util.spec_from_file_location("benchmark_baseline", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HarnessSmokeTests(unittest.TestCase):
    """Run the real CLI in a subprocess against its own database."""

    @classmethod
    def setUpClass(cls):
        cls.harness = _load_harness()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out_path = os.path.join(cls._tmp.name, "report.json")
        env = dict(os.environ)
        env["EVE_BENCH_DATABASE_URL"] = os.path.join(cls._tmp.name, "bench.db")
        env["FLASK_ENV"] = "development"
        env["DISABLE_BACKGROUND_THREADS"] = "1"
        result = subprocess.run(
            [sys.executable, HARNESS, "--quick", "--repeat", "2", "--warmup", "1",
             "--out", cls.out_path],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=900,
        )
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
        for key in ("generated_at", "app_version", "git_sha", "python", "platform",
                    "repeat", "warmup", "sizes", "scenarios"):
            self.assertIn(key, report)
        for name in ("html_login", "html_dashboard", "api_permissions",
                     "api_refresh_superadmin", "api_refresh_reseller",
                     "api_transactions_page", "api_transactions_search",
                     "api_payments_page", "api_finance_stats", "api_bank_cards"):
            self.assertIn(name, report["scenarios"], name)

    def test_every_scenario_returns_200_and_numeric_metrics(self):
        with open(self.out_path, encoding="utf-8") as handle:
            report = json.load(handle)
        for name, row in report["scenarios"].items():
            self.assertEqual(row["status"], 200, name)
            for metric in ("mean_ms", "p50_ms", "p95_ms", "min_ms", "max_ms",
                           "response_bytes", "sql_statements"):
                self.assertIsInstance(row[metric], (int, float), name + "." + metric)
            self.assertGreaterEqual(row["mean_ms"], 0)
            self.assertGreater(row["p50_ms"], 0, name)
            self.assertGreater(row["response_bytes"], 0, name)
            self.assertGreaterEqual(row["sql_statements"], 0, name)


class CompareReportTests(unittest.TestCase):
    def setUp(self):
        self.harness = _load_harness()

    def _scenario(self, mean_ms, p95_ms):
        return {"mean_ms": mean_ms, "p95_ms": p95_ms, "response_bytes": 1000,
                "sql_statements": 10}

    def test_regressions_and_improvements_are_detected(self):
        baseline = {"scenarios": {"slower": self._scenario(100.0, 120.0),
                                  "faster": self._scenario(100.0, 120.0)}}
        current = {"scenarios": {"slower": self._scenario(130.0, 150.0),
                                 "faster": self._scenario(50.0, 60.0)}}
        result = self.harness.compare_reports(baseline, current, tolerance_pct=10.0)
        flagged = {(item["scenario"], item["metric"]) for item in result["regressions"]}
        self.assertIn(("slower", "mean_ms"), flagged)
        self.assertIn(("slower", "p95_ms"), flagged)
        self.assertEqual(result["improvements"][0]["scenario"], "faster")
        rows = {row["scenario"]: row for row in result["rows"]}
        self.assertAlmostEqual(rows["slower"]["mean_ms_delta_pct"], 30.0, places=1)

    def test_new_scenarios_are_reported_without_crashing_the_compare(self):
        result = self.harness.compare_reports({"scenarios": {}},
                                              {"scenarios": {"brand-new": {"mean_ms": 1.0}}})
        self.assertEqual(result["rows"][0]["status"], "new")
        self.assertEqual(result["regressions"], [])

    def test_baseline_scenarios_missing_from_current_are_ignored(self):
        result = self.harness.compare_reports(
            {"scenarios": {"gone": self._scenario(1.0, 1.0)}}, {"scenarios": {}})
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["regressions"], [])


if __name__ == "__main__":
    unittest.main()
