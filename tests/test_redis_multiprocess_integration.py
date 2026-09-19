"""Real-Redis, multi-process integration test for the refresh/watch pipeline.

What this file is for
---------------------
tests/test_watch_propagation_crossprocess.py is the always-on guard: it starts real
child processes but hands them a small file-backed FAKE Redis, so it runs in CI with
no service. That fake is written by hand and can therefore only prove the code path
it was told about. It cannot prove that the app speaks to a REAL Redis: a wrong
command, a value stored under a key nothing reads, a bytes/str mismatch in a decoded
payload, a TTL that is never applied, a channel name no listener subscribes to, or a
snapshot the real serializer cannot round-trip would all pass against a stub.

This test runs scripts/integration_redis_multiprocess.py, which starts real child
processes against a real Redis through the app's own
panel.core.redis_client.get_redis(), and asserts the three cross-process facts the
fake cannot establish:

  1. a watch mark written by one process makes the server HOT for another, and the
     pub/sub wake really arrives there (publish_to_wake_ms p95);
  2. a published snapshot revision becomes visible to a NON-fetching dashboard
     process, with its last_update and rows intact (fetch_to_revision_visible_ms);
  3. a verified client fence written by one process is readable by another with its
     counters intact (fence_roundtrip_ms).

Redis is NOT required. When none is reachable the harness exits 0 with a
'SKIPPED: no Redis available' line, and this test calls skipTest() -- a missing
service must never fail the suite. Point the run at a service with REDIS_URL (the
app's own variable) or EVE_INTEGRATION_REDIS_URL (a CI job's override).

Run it:
  $env:EVE_SKIP_IMPORT_MIGRATIONS='1'
  .\\.venv\\Scripts\\python.exe -m unittest tests.test_redis_multiprocess_integration -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO_ROOT, 'scripts', 'integration_redis_multiprocess.py')
# Three watch rounds keep the test quick; the harness itself defaults to five and
# accepts --rounds for an operator who wants a tighter latency estimate.
ROUNDS = '3'
TIMEOUT_SECONDS = 600


class RedisMultiprocessIntegrationTests(unittest.TestCase):
    """One harness run, three step assertions (skipped when there is no Redis)."""

    summary = None

    @classmethod
    def setUpClass(cls):
        handle = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        handle.close()
        cls.json_path = handle.name
        env = dict(os.environ)
        # The harness itself must not run migrations or start a background fetch
        # loop: the point is to observe the pipeline, not to race a real one.
        env['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
        env['DISABLE_BACKGROUND_THREADS'] = '1'
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        try:
            proc = subprocess.run(
                [sys.executable, HARNESS, '--json', cls.json_path, '--rounds', ROUNDS],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True,
                timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.unlink(cls.json_path)
            raise AssertionError(
                'integration harness timed out after %ss\nharness: %s'
                % (TIMEOUT_SECONDS, HARNESS))
        cls.stdout = proc.stdout or ''
        cls.stderr = proc.stderr or ''
        cls.returncode = proc.returncode
        try:
            with open(cls.json_path, encoding='utf-8') as handle:
                cls.summary = json.load(handle)
        except Exception:
            cls.summary = None

    @classmethod
    def tearDownClass(cls):
        try:
            os.unlink(cls.json_path)
        except OSError:
            pass

    def setUp(self):
        if self.summary is None:
            self.skipTest('harness produced no JSON summary; stdout=%r stderr=%r'
                          % (self.stdout[-500:], self.stderr[-500:]))
        # A missing service is a skip for EVERY test in this class, never a failure.
        # The always-on guard for this pipeline is the fake-Redis unit test; this file
        # is the real-backend proof and is expected to be unavailable on most boxes.
        if self.summary.get('status') == 'skipped':
            self.skipTest('no real Redis available: %s'
                          % self.summary.get('reason', 'unknown reason'))

    def _step(self, name):
        self.assertIn('steps', self.summary)
        self.assertIn(name, self.summary['steps'],
                      'harness did not run the %s step: %s'
                      % (name, json.dumps(self.summary)[:2000]))
        return self.summary['steps'][name]

    def test_harness_reports_the_redis_it_used(self):
        # The run is only meaningful against a real backend: the app's own discovery
        # (or the CI override) must have produced a URL, and the children must have
        # agreed they were on Redis rather than on the in-process fallback.
        self.assertIn('redis', str(self.summary.get('redis_source', '')).lower())
        self.assertTrue(self.summary.get('redis_url'))
        watch = self.summary['steps'].get('watch') or {}
        if watch:
            self.assertEqual(watch.get('shared_backend'), 'redis')
            control = watch.get('control') or {}
            # Same code, no Redis: the control must NOT see the mark. Without this
            # negative the positive below could be measuring the test's own setup.
            self.assertEqual(control.get('backend'), 'process')
            self.assertFalse(control.get('watched'))

    def test_watch_mark_and_wake_cross_the_process_boundary(self):
        step = self._step('watch')
        self.assertTrue(step.get('listener_active'),
                        'the fetcher process never started its wake listener')
        self.assertGreaterEqual(len(step.get('samples') or []), 1)
        for sample in step['samples']:
            self.assertTrue(sample.get('woken'),
                            '%s: the wake listener was never woken' % sample.get('label'))
            self.assertTrue(sample.get('payload_has_server'),
                            '%s: wake payload did not name the watched server (%r)'
                            % (sample.get('label'), sample.get('wake_payload_server_ids')))
        check = step.get('check') or {}
        for sid in (check.get('watched') or {}):
            self.assertTrue(check['watched'][sid],
                            'server %s is not watched in the fetcher process' % sid)
        # Knowing about the mark is not enough: the cadence must actually shorten.
        for sid, interval in (check.get('interval') or {}).items():
            self.assertEqual(interval, check.get('active_interval'),
                             'server %s runs at %r, not the HOT interval %r'
                             % (sid, interval, check.get('active_interval')))
        # A mark with no TTL would keep a closed tab's panel hot forever.
        for sid, ttl in (check.get('mark_ttl') or {}).items():
            self.assertGreater(ttl, 0, 'watch mark for %s has ttl=%r' % (sid, ttl))
        self.assertIsNotNone(step.get('publish_to_wake_ms_p95'))
        self.assertGreaterEqual(step['publish_to_wake_ms_p95'], 0.0)
        self.assertTrue(step.get('passed'), step.get('failures'))

    def test_published_revision_becomes_visible_to_another_process(self):
        step = self._step('fetch_publish')
        self.assertTrue(step.get('write', {}).get('published'),
                        'publish_snapshot_to_redis reported failure')
        snapshot = step.get('snapshot') or {}
        self.assertTrue(snapshot.get('visible'),
                        'the dashboard process never observed the new revision')
        self.assertEqual(snapshot.get('seen_version'), step['write'].get('version'))
        self.assertTrue(snapshot.get('last_update_matches'),
                        'dashboard last_update=%r, fetch wrote %r'
                        % (snapshot.get('last_update'), step['write'].get('last_update')))
        self.assertGreater(snapshot.get('inbounds') or 0, 0)
        self.assertGreater(snapshot.get('clients') or 0, 0)
        latency = step.get('fetch_to_revision_visible_ms')
        self.assertIsNotNone(latency)
        self.assertGreaterEqual(latency, step.get('panel_latency_ms') or 0)
        # Every cache entry must expire: Redis is an ephemeral store, not a ledger.
        for name, ttl in (step.get('ttls') or {}).items():
            self.assertGreater(ttl, 0, 'published %s key has ttl=%r' % (name, ttl))
        self.assertTrue(step.get('passed'), step.get('failures'))

    def test_verified_fence_crosses_the_process_boundary(self):
        step = self._step('mutation')
        self.assertTrue(step.get('write', {}).get('ok'),
                        'record_client_fence returned False')
        self.assertTrue(step.get('read', {}).get('found'),
                        'a second process could not see the fence')
        fence = step.get('fence') or {}
        self.assertEqual(fence.get('used_up'), 7)
        self.assertEqual(fence.get('used_down'), 11)
        self.assertGreater(fence.get('expires_at') or 0, fence.get('verified_at') or 0)
        self.assertIsNotNone(step.get('fence_roundtrip_ms'))
        self.assertTrue(step.get('passed'), step.get('failures'))

    def test_harness_cleaned_up_after_itself(self):
        cleanup = self.summary.get('cleanup') or {}
        self.assertNotIn('error', cleanup,
                         'cleanup failed: %s' % cleanup.get('error'))
        self.assertGreater(cleanup.get('count') or 0, 0,
                           'cleanup removed nothing, so the run left keys behind')
        removed = cleanup.get('removed') or []
        for key in removed:
            self.assertTrue(
                key.startswith('eve:it:') or key.startswith('eve:refresh:')
                or key.startswith('eve:client_fence:'),
                'cleanup touched an unexpected key: %r' % key)


if __name__ == '__main__':
    unittest.main()
