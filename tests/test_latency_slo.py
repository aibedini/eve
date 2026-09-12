"""Phase 11 tests: the official latency SLOs are measured and enforced."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO_ROOT, 'scripts', 'benchmark_latency_slo.py')
_DOC = os.path.join(_REPO_ROOT, 'docs', 'performance', 'LATENCY_SLO.md')
_ARTIFACT = os.path.join(_REPO_ROOT, 'docs', 'performance', 'latency-slo.json')
_WORKFLOW = os.path.join(_REPO_ROOT, '.github', 'workflows', 'tests.yml')

EXPECTED_SLOS = {
    'mutation_cache_commit_ms': 100.0,
    'browser_visible_ms': 300.0,
    'cache_read_ms': 50.0,
    'other_tabs_ms': 1000.0,
    'external_xui_ms': 3000.0,
}


def _load_script():
    spec = importlib.util.spec_from_file_location('benchmark_latency_slo', _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LatencySloBenchmarkTests(unittest.TestCase):
    """Run the real benchmark once; every assertion below reads its result."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = os.path.join(cls._tmp.name, 'latency.json')
        cls.result = subprocess.run(
            [sys.executable, _SCRIPT, '--quick', '--json', out],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=900)
        cls.payload = None
        if os.path.exists(out):
            with open(out, encoding='utf-8') as handle:
                cls.payload = json.load(handle)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_every_slo_is_met(self):
        self.assertEqual(self.result.returncode, 0,
                         self.result.stdout + self.result.stderr)
        self.assertIsNotNone(self.payload, 'the benchmark wrote no JSON result')
        self.assertTrue(self.payload['passed'])

    def test_the_five_official_slos_are_measured_with_their_budgets(self):
        self.assertEqual(set(self.payload['slos']), set(EXPECTED_SLOS))
        for name, budget in EXPECTED_SLOS.items():
            row = self.payload['slos'][name]
            self.assertEqual(row['budget_ms'], budget, name)
            self.assertTrue(row['passed'], '%s missed: %s' % (name, row))
            self.assertGreaterEqual(row['samples'], 1, name)
            # A measured p95, not a placeholder.
            self.assertGreater(row['p95_ms'], 0.0, name)
            self.assertLess(row['p95_ms'], row['budget_ms'], name)
            self.assertGreaterEqual(row['max_ms'], row['p95_ms'], name)

    def test_the_script_budgets_match_the_documentation(self):
        module = _load_script()
        self.assertEqual(module.SLOS, EXPECTED_SLOS)
        with open(_DOC, encoding='utf-8') as handle:
            doc = handle.read()
        for name, budget in EXPECTED_SLOS.items():
            self.assertIn('`%s`' % name, doc, name)
            self.assertIn(str(int(budget)), doc, name)

    def test_the_ci_workflow_gates_on_the_benchmark(self):
        with open(_WORKFLOW, encoding='utf-8') as handle:
            workflow = handle.read()
        self.assertIn('benchmark_latency_slo.py', workflow)
        # The gate must be able to fail the job: a bare invocation returns non-zero
        # on a violation, so the step must not be allowed to fail.
        self.assertNotIn('continue-on-error: true', workflow)
        self.assertIn('latency-slo', workflow)

    def test_the_committed_artifact_keeps_the_metric_shape(self):
        self.assertTrue(os.path.exists(_ARTIFACT), 'run the benchmark with --json')
        with open(_ARTIFACT, encoding='utf-8') as handle:
            artifact = json.load(handle)
        self.assertEqual(set(artifact['slos']), set(EXPECTED_SLOS))
        for name in EXPECTED_SLOS:
            self.assertIn('p95_ms', artifact['slos'][name], name)


class LatencySloUnitTests(unittest.TestCase):
    def test_p95_uses_the_nearest_rank(self):
        module = _load_script()
        self.assertEqual(module.p95([]), 0.0)
        self.assertEqual(module.p95([5.0]), 5.0)
        values = [float(value) for value in range(1, 101)]
        self.assertEqual(module.p95(values), 95.0)
        self.assertEqual(module.p95([1.0, 2.0]), 2.0)

    def test_the_script_exposes_a_stable_surface(self):
        module = _load_script()
        for attribute in ('SLOS', 'p95', 'run', 'main'):
            self.assertTrue(hasattr(module, attribute), attribute)
        # The payload shape the artifact and CI rely on.
        source = open(_SCRIPT, encoding='utf-8').read()
        for key in ('p95_ms', 'budget_ms', 'passed', 'samples', 'mean_ms', 'max_ms'):
            self.assertIn("'%s'" % key, source, key)
        self.assertIn('def run(', source)


if __name__ == '__main__':
    unittest.main()
