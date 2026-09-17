"""Phase 12 tests: the mutation path is O(1) in the number of panels."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO_ROOT, 'scripts', 'benchmark_mutation_scale.py')
_DOC = os.path.join(_REPO_ROOT, 'docs', 'performance', 'MUTATION_SCALE.md')
_ARTIFACT = os.path.join(_REPO_ROOT, 'docs', 'performance', 'mutation-scale.json')
_WORKFLOW = os.path.join(_REPO_ROOT, '.github', 'workflows', 'tests.yml')

VERDICTS = (
    'mutation_is_flat',
    'delta_is_one_block',
    'full_snapshot_grows_with_the_install',
    'redis_ops_are_constant',
    'cache_read_makes_no_panel_call',
    'per_server_polling_is_bounded',
)


def _load_script():
    spec = importlib.util.spec_from_file_location('benchmark_mutation_scale', _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MutationScaleBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = os.path.join(cls._tmp.name, 'scale.json')
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

    def test_every_o1_relation_holds(self):
        self.assertEqual(self.result.returncode, 0,
                         self.result.stdout[-4000:] + self.result.stderr[-2000:])
        self.assertIsNotNone(self.payload, 'the benchmark wrote no JSON result')
        self.assertTrue(self.payload['passed'], self.payload.get('verdicts'))
        self.assertEqual(set(self.payload['verdicts']), set(VERDICTS))
        for name, verdict in self.payload['verdicts'].items():
            self.assertTrue(verdict['passed'], '%s: %s' % (name, verdict))

    def test_the_mutation_cost_does_not_grow_with_the_panel_count(self):
        rows = {row['servers']: row for row in self.payload['rows']}
        smallest, largest = min(rows), max(rows)
        self.assertLess(
            rows[largest]['mutation_p95_ms'],
            2.0 * rows[smallest]['mutation_p95_ms'],
            'mutation p95 grew with the install: %s' % [(n, rows[n]['mutation_p95_ms'])
                                                        for n in sorted(rows)])
        # CPU per mutation is flat too: this is the O(1) claim, not a JIT artefact.
        self.assertLessEqual(
            rows[largest]['mutation_cpu_p95_ms'],
            2.0 * rows[smallest]['mutation_cpu_p95_ms'])

    def test_the_delta_is_one_panel_block_while_the_snapshot_grows(self):
        rows = {row['servers']: row for row in self.payload['rows']}
        smallest, largest = min(rows), max(rows)
        self.assertEqual(rows[largest]['delta_mode'], 'delta')
        self.assertLess(rows[largest]['delta_bytes'], 2 * rows[smallest]['delta_bytes'])
        self.assertGreater(rows[largest]['full_inbounds_bytes'],
                           4 * rows[smallest]['full_inbounds_bytes'])
        # The delta the browser merges is a fraction of the full snapshot.
        self.assertLess(rows[largest]['delta_bytes'],
                        rows[largest]['full_inbounds_bytes'] / 4)

    def test_the_read_path_never_calls_a_panel(self):
        for row in self.payload['rows']:
            self.assertEqual(row['outbound_http_calls_per_cache_read'], 0,
                             '%d panels: the cache read made an outbound call' % row['servers'])
            self.assertEqual(row['cache_read_delta_bytes'] < row['cache_read_full_bytes'],
                             True)

    def test_redis_work_per_mutation_is_constant(self):
        totals = {row['redis_ops_total_per_mutation'] for row in self.payload['rows']}
        self.assertEqual(len(totals), 1, totals)
        self.assertGreater(totals.pop(), 0)
        # The per-server cache is what keeps this small; a full-snapshot publish would
        # scale the op count (and the payload) with the install.
        for row in self.payload['rows']:
            self.assertLessEqual(row['redis_ops_total_per_mutation'], 40,
                                 row['redis_ops_per_mutation'])

    def test_watched_polling_stays_bounded(self):
        verdict = self.payload['verdicts']['per_server_polling_is_bounded']
        self.assertTrue(verdict['passed'], verdict)
        for row in self.payload['rows']:
            watched = min(row['servers'], self.payload['watch_limit'])
            bound = watched * 30 + (row['servers'] - watched) * 2
            self.assertLessEqual(row['xui_requests_per_minute_watched'], bound,
                                 row)
            if row['servers'] > self.payload['watch_limit']:
                # Bigger than the watch limit: strictly below "everything every 2 s".
                self.assertLess(row['xui_requests_per_minute_watched'],
                                row['naive_all_servers_every_2s_per_minute'], row)

    def test_the_artifact_and_the_documentation_agree(self):
        self.assertTrue(os.path.exists(_ARTIFACT), 'run the benchmark with --json')
        with open(_ARTIFACT, encoding='utf-8') as handle:
            artifact = json.load(handle)
        self.assertEqual({row['servers'] for row in artifact['rows']}, {10, 50, 100})
        self.assertTrue(artifact['passed'])
        with open(_DOC, encoding='utf-8') as handle:
            doc = handle.read()
        for token in ('mutation_is_flat', 'delta_is_one_block', 'redis_ops_are_constant',
                      'EVE_SERVER_POLL_WATCH_LIMIT', 'scripts/benchmark_mutation_scale.py'):
            self.assertIn(token, doc, token)

    def test_ci_runs_the_scale_benchmark(self):
        with open(_WORKFLOW, encoding='utf-8') as handle:
            workflow = handle.read()
        self.assertIn('benchmark_mutation_scale.py', workflow)
        self.assertIn('mutation-scale', workflow)


class MutationScaleUnitTests(unittest.TestCase):
    def test_least_noisy_round_keeps_wall_and_cpu_samples_paired(self):
        module = _load_script()
        wall, cpu = module._least_noisy_round(
            [([8.0, 9.0], [80.0, 90.0]),
             ([1.0, 2.0], [10.0, 20.0]),
             ([3.0, 4.0], [30.0, 40.0])])
        self.assertEqual(wall, [1.0, 2.0])
        self.assertEqual(cpu, [10.0, 20.0])

    def test_p95_and_the_counting_redis(self):
        module = _load_script()
        self.assertEqual(module.p95([1.0, 2.0, 3.0, 4.0]), 4.0)
        fake = module.CountingRedis()
        fake.set('k', 'v', nx=True, ex=30)
        fake.get('k')
        pipe = fake.pipeline()
        pipe.incr('k')
        pipe.expire('k', 60)
        pipe.execute()
        counts = fake.counts()
        self.assertEqual(counts['set'], 1)
        self.assertEqual(counts['get'], 1)
        self.assertEqual(counts['incr'], 1)
        self.assertEqual(counts['expire'], 1)
        self.assertEqual(counts['pipeline'], 1)
        self.assertEqual(counts['execute'], 1)
        self.assertEqual(fake.total(), 6)

    def test_the_scales_and_bounds_are_stable(self):
        module = _load_script()
        self.assertEqual(module.SCALES, (10, 50, 100))
        self.assertLess(module.MUTATION_SCALE_TOLERANCE, 3.0)
        self.assertEqual(module.MAX_OUTBOUND_HTTP_PER_CACHE_READ, 0)


if __name__ == '__main__':
    unittest.main()
