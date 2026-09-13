"""Usage-intelligence performance and index verification (RFP sections 30, 31, 50, 51).

Runs the benchmark against a freshly seeded database and fails when the recommendation gets
slow, issues more statements than the budget allows, touches a panel, or stops using the
indexes.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO_ROOT, 'scripts', 'benchmark_usage_intelligence.py')
_ARTIFACT = os.path.join(_REPO_ROOT, 'docs', 'performance', 'usage-intelligence.json')
_DOC = os.path.join(_REPO_ROOT, 'docs', 'performance', 'USAGE_INTELLIGENCE_PERF.md')


def _load_script():
    spec = importlib.util.spec_from_file_location('benchmark_usage_intelligence', _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UsageIntelligencePerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = os.path.join(cls._tmp.name, 'usage.json')
        cls.result = subprocess.run(
            [sys.executable, _SCRIPT, '--quick', '--json', out],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=1800)
        cls.payload = None
        if os.path.exists(out):
            with open(out, encoding='utf-8') as handle:
                cls.payload = json.load(handle)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_budget_holds(self):
        self.assertEqual(self.result.returncode, 0,
                         self.result.stdout[-3000:] + self.result.stderr[-1500:])
        self.assertIsNotNone(self.payload)
        self.assertTrue(self.payload['passed'], self.payload.get('verdicts'))
        for name, verdict in self.payload['verdicts'].items():
            self.assertTrue(verdict['passed'], '%s: %s' % (name, verdict))

    def test_the_latency_budget_is_met_on_a_warm_database(self):
        latency = self.payload['latency_ms']
        self.assertLess(latency['p95'], 50.0)
        self.assertLessEqual(latency['p95'], latency['max'])
        self.assertGreater(latency['p50'], 0.0)
        self.assertEqual(self.payload['verdicts']['latency_within_budget']['budget_ms'], 50.0)

    def test_the_query_budget_and_the_zero_panel_call_rule_hold(self):
        self.assertLessEqual(self.payload['queries_per_recommendation'], 6)
        self.assertEqual(self.payload['outbound_http_calls'], 0)

    def test_every_window_uses_an_index_rather_than_a_scan(self):
        plans = self.payload['query_plans']
        self.assertEqual(set(plans), {'latest_verified_renewal', 'usage_since_cycle',
                                      'rolling_31d'})
        for name, plan in plans.items():
            self.assertIn('USING INDEX', plan.upper(), '%s: %s' % (name, plan))
            self.assertNotIn('SCAN ', plan.upper(), '%s: %s' % (name, plan))
        # The renewal boundary must use the dedicated composite index added for it.
        self.assertIn('ix_renewal_events_server_sub_verified_renewed',
                      plans['latest_verified_renewal'])
        self.assertFalse(any(self.payload['full_scans'].values()))
        self.assertEqual(self.payload['verdicts']['indexed_reads']['full_scans'], {})

    def test_the_dataset_is_large_enough_to_mean_something(self):
        self.assertGreaterEqual(self.payload['usage_daily_rows'], 5000)
        self.assertGreaterEqual(self.payload['accounts'], 200)
        self.assertGreaterEqual(self.payload['samples'], 10)

    def test_the_committed_artifact_matches_the_shape(self):
        self.assertTrue(os.path.exists(_ARTIFACT), 'run the benchmark with --json')
        with open(_ARTIFACT, encoding='utf-8') as handle:
            artifact = json.load(handle)
        self.assertTrue(artifact['passed'], artifact.get('verdicts'))
        self.assertLess(artifact['latency_ms']['p95'], 50.0)
        self.assertEqual(artifact['outbound_http_calls'], 0)
        self.assertGreaterEqual(artifact['usage_daily_rows'], 400000)

    def test_the_documentation_records_the_budgets_and_the_indexes(self):
        with open(_DOC, encoding='utf-8') as handle:
            doc = handle.read()
        for token in ('EXPLAIN ANALYZE', 'ix_renewal_events_server_sub_verified_renewed',
                      'ix_usage_daily', 'p95', '50 ms', '496,000'):
            self.assertIn(token, doc, token)


class UsageIntelligenceBenchmarkUnitTests(unittest.TestCase):
    def test_p95_uses_the_nearest_rank(self):
        module = _load_script()
        self.assertEqual(module.p95([]), 0.0)
        self.assertEqual(module.p95([1.0, 2.0]), 2.0)
        self.assertEqual(module.p95([float(value) for value in range(1, 101)]), 95.0)

    def test_the_budgets_are_the_agreed_ones(self):
        module = _load_script()
        self.assertEqual(module.LATENCY_BUDGET_P95_MS, 50.0)
        self.assertEqual(module.QUERY_BUDGET, 6)
        self.assertEqual(module.MAX_OUTBOUND_HTTP, 0)


if __name__ == '__main__':
    unittest.main()
