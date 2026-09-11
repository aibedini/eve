"""Phase 13 tests: targeted per-server snapshot loads and cache metrics."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from panel.core import redis_client  # noqa: E402


class FakeRedis:
    """Read-only Redis stand-in that records which keys were fetched."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.gets = []

    def get(self, key):
        self.gets.append(key)
        return self.values.get(key)


def _manifest(server_versions):
    return {
        "format": 2,
        "version": "v2",
        "server_versions": server_versions,
        "stats": {"total_clients": 3},
        "servers_status": [{"server_id": sid} for sid in sorted(server_versions)],
        "last_update": "t2",
    }


class PerServerLoadTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(redis_client.GLOBAL_SERVER_DATA)
        self.addCleanup(self._restore)
        redis_client.reset_snapshot_metrics()
        redis_client._LAST_LOADED_SNAPSHOT_VERSION = None
        redis_client._LAST_LOADED_SERVER_VERSIONS = {}
        redis_client.GLOBAL_SERVER_DATA.update({
            "inbounds": [], "servers_status": [], "stats": {}, "last_update": None,
        })

    def _restore(self):
        redis_client.GLOBAL_SERVER_DATA.clear()
        redis_client.GLOBAL_SERVER_DATA.update(self._saved)
        redis_client.reset_snapshot_metrics()
        redis_client._LAST_LOADED_SNAPSHOT_VERSION = None
        redis_client._LAST_LOADED_SERVER_VERSIONS = {}

    def _seed_local(self, blocks):
        inbounds = []
        for sid in sorted(blocks):
            for inbound_id in blocks[sid]:
                inbounds.append({"server_id": sid, "id": inbound_id, "clients": []})
        redis_client.GLOBAL_SERVER_DATA["inbounds"] = inbounds
        redis_client.GLOBAL_SERVER_DATA["last_update"] = "t1"

    def _client(self, server_versions, remote_blocks):
        values = {
            redis_client.REDIS_SNAPSHOT_VERSION_KEY: b"v2",
            redis_client.REDIS_SNAPSHOT_MANIFEST_KEY:
                redis_client._encode_snapshot(_manifest(server_versions)),
        }
        for sid, block in remote_blocks.items():
            values[redis_client._redis_server_snapshot_key(sid)] = \
                redis_client._encode_snapshot(block)
        return FakeRedis(values)

    def test_targeted_load_reads_only_the_requested_server_block(self):
        self._seed_local({1: [1], 2: [1], 3: [1]})
        client = self._client({1: "a", 2: "b", 3: "c"},
                              {2: [{"server_id": 2, "id": 9, "clients": []}]})
        with mock.patch.object(redis_client, "get_redis", return_value=client):
            changed = redis_client._load_snapshot_from_redis_unlocked(
                force=True, server_ids=[2])
        self.assertTrue(changed)
        block_keys = [key for key in client.gets
                      if key.startswith(redis_client.REDIS_SERVER_SNAPSHOT_PREFIX)]
        self.assertEqual(block_keys, [redis_client._redis_server_snapshot_key(2)])
        rows = [(row["server_id"], row["id"]) for row in redis_client.GLOBAL_SERVER_DATA["inbounds"]]
        self.assertEqual(rows, [(1, 1), (2, 9), (3, 1)])
        metrics = redis_client.snapshot_metrics()
        self.assertEqual(metrics["blocks_decoded"], 1)
        self.assertEqual(metrics["targeted_loads"], 1)
        self.assertEqual(metrics["full_loads"], 0)
        # A targeted load must not claim the whole version was merged.
        self.assertIsNone(redis_client._LAST_LOADED_SNAPSHOT_VERSION)
        self.assertEqual(redis_client._LAST_LOADED_SERVER_VERSIONS, {2: "b"})

    def test_targeted_load_decodes_a_server_that_has_no_local_copy(self):
        self._seed_local({1: [1]})
        client = self._client({1: "a", 2: "b"},
                              {2: [{"server_id": 2, "id": 5, "clients": []}]})
        with mock.patch.object(redis_client, "get_redis", return_value=client):
            redis_client._load_snapshot_from_redis_unlocked(force=True, server_ids=[1])
        rows = [(row["server_id"], row["id"]) for row in redis_client.GLOBAL_SERVER_DATA["inbounds"]]
        self.assertIn((2, 5), rows)
        self.assertEqual(redis_client.snapshot_metrics()["blocks_decoded"], 1)

    def test_full_load_decodes_only_servers_whose_version_changed(self):
        self._seed_local({1: [1], 2: [1]})
        redis_client._LAST_LOADED_SERVER_VERSIONS = {1: "a", 2: "old"}
        client = self._client({1: "a", 2: "b"},
                              {2: [{"server_id": 2, "id": 7, "clients": []}]})
        with mock.patch.object(redis_client, "get_redis", return_value=client):
            redis_client._load_snapshot_from_redis_unlocked(force=False)
        metrics = redis_client.snapshot_metrics()
        self.assertEqual(metrics["blocks_decoded"], 1)
        self.assertEqual(metrics["full_loads"], 1)
        rows = [(row["server_id"], row["id"]) for row in redis_client.GLOBAL_SERVER_DATA["inbounds"]]
        self.assertIn((2, 7), rows)
        self.assertEqual(redis_client._LAST_LOADED_SNAPSHOT_VERSION, b"v2")

    def test_unchanged_version_short_circuits_a_targeted_load(self):
        self._seed_local({1: [1]})
        redis_client._LAST_LOADED_SNAPSHOT_VERSION = b"v2"
        client = self._client({1: "a"}, {})
        with mock.patch.object(redis_client, "get_redis", return_value=client):
            changed = redis_client._load_snapshot_from_redis_unlocked(force=False)
        self.assertFalse(changed)
        self.assertEqual(client.gets, [redis_client.REDIS_SNAPSHOT_VERSION_KEY])

    def test_load_snapshot_from_redis_forwards_server_ids(self):
        with mock.patch.object(redis_client, "_load_snapshot_from_redis_unlocked",
                               return_value=True) as loader:
            redis_client.load_snapshot_from_redis(server_ids=[4])
        loader.assert_called_once_with(force=False, server_ids=[4])

    def test_serialized_write_cycle_targets_its_own_server(self):
        client = mock.Mock()
        client.set.return_value = True
        client.eval.return_value = 1
        calls = []
        with (
            mock.patch.object(redis_client, "get_redis", return_value=client),
            mock.patch.object(redis_client, "_load_snapshot_from_redis_unlocked",
                              side_effect=lambda **kwargs: calls.append(kwargs)),
        ):
            with redis_client.serialized_server_snapshot_write(5):
                pass
        self.assertEqual(calls, [{"force": True, "server_ids": [5]}])


class PublishMetricTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(redis_client.GLOBAL_SERVER_DATA)
        self.addCleanup(self._restore)
        redis_client.reset_snapshot_metrics()
        redis_client.GLOBAL_SERVER_DATA.update({
            "inbounds": [{"server_id": 1, "id": 1, "clients": []},
                         {"server_id": 2, "id": 1, "clients": []}],
            "servers_status": [{"server_id": 1}, {"server_id": 2}],
            "stats": {}, "last_update": "t1",
        })

    def _restore(self):
        redis_client.GLOBAL_SERVER_DATA.clear()
        redis_client.GLOBAL_SERVER_DATA.update(self._saved)
        redis_client.reset_snapshot_metrics()

    def _client(self):
        client = mock.Mock()
        pipe = mock.Mock()
        client.pipeline.return_value = pipe
        pipe.get.return_value = b"0"
        return client, pipe

    def test_metadata_only_publish_encodes_no_blocks(self):
        client, pipe = self._client()
        with mock.patch.object(redis_client, "get_redis", return_value=client):
            published = redis_client.publish_snapshot_to_redis([])
        self.assertTrue(published)
        metrics = redis_client.snapshot_metrics()
        self.assertEqual(metrics["publishes"], 1)
        self.assertEqual(metrics["blocks_encoded"], 0)

    def test_publishing_one_changed_server_encodes_one_block(self):
        client, pipe = self._client()
        with mock.patch.object(redis_client, "get_redis", return_value=client):
            published = redis_client.publish_snapshot_to_redis(
                [2], expected_server_revisions={2: 0})
        self.assertTrue(published)
        metrics = redis_client.snapshot_metrics()
        self.assertEqual(metrics["blocks_encoded"], 1)
        self.assertGreater(metrics["bytes_encoded"], 0)


class CacheBenchmarkScriptTests(unittest.TestCase):
    def test_quick_script_writes_a_valid_result(self):
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "benchmark_cache.py")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "result.json")
            result = subprocess.run(
                [sys.executable, script, "--quick", "--json", out],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
        for key in ("servers", "full_ms", "targeted_ms", "speedup",
                    "full_blocks_decoded", "targeted_blocks_decoded",
                    "full_bytes", "targeted_bytes", "traffic_reduction"):
            self.assertIn(key, payload)
        self.assertEqual(payload["targeted_blocks_decoded"], 1)
        self.assertEqual(payload["full_blocks_decoded"], payload["servers"])
        self.assertGreaterEqual(payload["traffic_reduction"], 1.0)


if __name__ == "__main__":
    unittest.main()
