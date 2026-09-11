"""Phase 18 tests: subscription response cache in front of the public route."""
import base64
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

from app import Server, app, db  # noqa: E402
from panel.core import subscription_cache  # noqa: E402
from panel.routes import subscription_pages as sp  # noqa: E402

SERVER_ID = 9401
SUB_ID = 'sub-1'


class SubscriptionCacheUnitTests(unittest.TestCase):
    def setUp(self):
        subscription_cache.reset()
        # Snapshot the environment so the keys popped below are restored; a plain
        # pop would leak into other modules (e.g. the live-content tests).
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in ("EVE_SUBSCRIPTION_CACHE_ENABLED", "EVE_SUBSCRIPTION_CACHE_TTL_SECONDS",
                     "EVE_SUBSCRIPTION_CONFIG_CACHE_TTL_SECONDS",
                     "EVE_SUBSCRIPTION_CACHE_MAX_ENTRIES",
                     "EVE_SUBSCRIPTION_CACHE_WAIT_SECONDS"):
            os.environ.pop(name, None)
        self.addCleanup(subscription_cache.reset)

    def test_set_get_and_expiry(self):
        key = subscription_cache.make_key(1, 'a')
        self.assertIsNone(subscription_cache.get(key))
        subscription_cache.set(key, ('body', 200, {}), ttl=60)
        self.assertEqual(subscription_cache.get(key), ('body', 200, {}))
        subscription_cache.set(subscription_cache.make_key(1, 'b'), ('x', 200, {}), ttl=0.01)
        time.sleep(0.05)
        self.assertIsNone(subscription_cache.get(subscription_cache.make_key(1, 'b')))
        self.assertEqual(subscription_cache.metrics()['expired'], 1)

    def test_lru_eviction_is_bounded(self):
        with mock.patch.dict(os.environ, {"EVE_SUBSCRIPTION_CACHE_MAX_ENTRIES": "2"}):
            subscription_cache.set(subscription_cache.make_key(1, 'a'), ('a', 200, {}), ttl=60)
            subscription_cache.set(subscription_cache.make_key(1, 'b'), ('b', 200, {}), ttl=60)
            subscription_cache.set(subscription_cache.make_key(1, 'c'), ('c', 200, {}), ttl=60)
            self.assertEqual(subscription_cache.metrics()['entries'], 2)
            self.assertEqual(subscription_cache.metrics()['evictions'], 1)
            self.assertIsNone(subscription_cache.get(subscription_cache.make_key(1, 'a')))

    def test_invalidate_server_only_drops_that_server(self):
        subscription_cache.set(subscription_cache.make_key(1, 'a'), ('a', 200, {}), ttl=60)
        subscription_cache.set(subscription_cache.make_key(2, 'a'), ('b', 200, {}), ttl=60)
        self.assertEqual(subscription_cache.invalidate_server(1), 1)
        self.assertIsNone(subscription_cache.get(subscription_cache.make_key(1, 'a')))
        self.assertEqual(subscription_cache.get(subscription_cache.make_key(2, 'a')), ('b', 200, {}))

    def test_disabled_cache_stores_nothing(self):
        with mock.patch.dict(os.environ, {"EVE_SUBSCRIPTION_CACHE_ENABLED": "0"}):
            key = subscription_cache.make_key(1, 'a')
            self.assertFalse(subscription_cache.set(key, ('a', 200, {}), ttl=60))
            self.assertIsNone(subscription_cache.get(key))

    def test_single_flight_bookkeeping(self):
        key = subscription_cache.make_key(1, 'a')
        self.assertTrue(subscription_cache.begin(key))
        self.assertFalse(subscription_cache.begin(key))
        self.assertTrue(subscription_cache.in_flight(key))

        result = {}

        def follower():
            result['filled'] = subscription_cache.wait_for_fill(key, timeout=2)

        thread = threading.Thread(target=follower)
        thread.start()
        time.sleep(0.05)
        subscription_cache.set(key, ('body', 200, {}), ttl=60)
        subscription_cache.end(key)
        thread.join(timeout=5)
        self.assertTrue(result.get('filled'))
        self.assertFalse(subscription_cache.in_flight(key))
        self.assertEqual(subscription_cache.get(key), ('body', 200, {}))
        self.assertGreaterEqual(subscription_cache.metrics()['stampede_fills'], 1)

    def test_metrics_shape(self):
        subscription_cache.set(subscription_cache.make_key(1, 'a'), ('a', 200, {}), ttl=60)
        subscription_cache.get(subscription_cache.make_key(1, 'a'))
        subscription_cache.note_miss()
        metrics = subscription_cache.metrics()
        self.assertEqual(metrics['hits'], 1)
        self.assertEqual(metrics['misses'], 1)
        self.assertEqual(metrics['hit_rate'], 0.5)
        self.assertEqual(metrics['ttl_seconds']['full'], 30)
        self.assertEqual(metrics['ttl_seconds']['fast'], 300)


class SubscriptionRouteCacheTests(unittest.TestCase):
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
        subscription_cache.reset()
        self.addCleanup(subscription_cache.reset)
        # Force the cache on regardless of what other modules did to the env.
        self._cache_env = mock.patch.dict(
            os.environ, {'EVE_SUBSCRIPTION_CACHE_ENABLED': '1'})
        self._cache_env.start()
        self.addCleanup(self._cache_env.stop)
        Server.query.filter_by(id=SERVER_ID).delete()
        db.session.add(Server(id=SERVER_ID, name='sub-cache', host='https://sub.invalid',
                              username='u', password='p', panel_type='auto', enabled=True))
        db.session.commit()
        self.calls = {'session': 0, 'configs': 0}

        def fake_session(server):
            self.calls['session'] += 1
            return object(), None

        def fake_configs(*args, **kwargs):
            self.calls['configs'] += 1
            return ['vless://example']

        self._patches = [
            mock.patch.object(sp, '_subscription_statistics_settings',
                              return_value={'enabled': False}),
            mock.patch.object(sp, 'get_xui_session', side_effect=fake_session),
            mock.patch.object(sp, 'fetch_authoritative_subscription_configs',
                              side_effect=fake_configs),
            mock.patch.object(sp, 'sort_subscription_configs',
                              side_effect=lambda configs, *a, **k: configs),
            mock.patch.object(sp, 'find_subscription_client_email', return_value='e@test'),
            mock.patch.object(sp, 'ensure_subscription_identity',
                              side_effect=lambda configs, email: configs),
            mock.patch.object(sp, 'fetch_subscription_profile_metadata',
                              return_value={'sub_title': 'T', 'update_interval': '24'}),
            mock.patch.object(sp, 'build_subscription_profile_title', return_value='T'),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app.test_client()

    def _get(self):
        return self.client.get('/s/%d/%s' % (SERVER_ID, SUB_ID),
                               headers={'User-Agent': 'v2rayng/1.9'})

    def test_first_request_reads_the_panel_and_the_next_ones_hit_the_cache(self):
        first = self._get()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers.get('X-Eve-Cache'), 'miss')
        expected = base64.b64encode(b'vless://example').decode('ascii')
        self.assertEqual(first.get_data(as_text=True), expected)
        self.assertEqual(self.calls['configs'], 1)

        second = self._get()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.headers.get('X-Eve-Cache'), 'hit')
        self.assertEqual(second.get_data(as_text=True), expected)
        self.assertEqual(self.calls['configs'], 1)
        self.assertEqual(self.calls['session'], 1)
        self.assertEqual(subscription_cache.metrics()['hits'], 1)

    def test_invalidation_forces_a_fresh_panel_read(self):
        self._get()
        self.assertEqual(subscription_cache.invalidate_server(SERVER_ID), 1)
        again = self._get()
        self.assertEqual(again.headers.get('X-Eve-Cache'), 'miss')
        self.assertEqual(self.calls['configs'], 2)

    def test_disabled_cache_always_reads_the_panel(self):
        # The setUp patch supplies "1"; override it for this test.
        with mock.patch.dict(os.environ, {"EVE_SUBSCRIPTION_CACHE_ENABLED": "0"}):
            first = self._get()
            second = self._get()
        # With the cache off the route does not advertise a cache state at all.
        self.assertIsNone(first.headers.get('X-Eve-Cache'))
        self.assertIsNone(second.headers.get('X-Eve-Cache'))
        self.assertEqual(self.calls['configs'], 2)

    def test_unknown_server_is_still_a_404_without_caching(self):
        response = self.client.get('/s/999999/whatever', headers={'User-Agent': 'v2rayng'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(subscription_cache.metrics()['stores'], 0)


class SubscriptionCacheScriptTests(unittest.TestCase):
    def test_quick_script_writes_a_valid_result(self):
        import json
        import subprocess
        import sys
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "benchmark_subscription_cache.py")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sub-cache.json")
            result = subprocess.run(
                [sys.executable, script, "--quick", "--json", out],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertGreater(payload["panel_reads_without_cache"], payload["panel_reads_with_cache"])
        self.assertGreater(payload["hit_rate"], 0.5)
        self.assertEqual(payload["stampede_renders"], 1)


if __name__ == "__main__":
    unittest.main()
