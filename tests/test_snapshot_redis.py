import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from panel.core import redis_client, snapshot_model  # noqa: E402


class FakeRedis:
    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def manifest(version='new'):
    return {
        'format': 2,
        'version': version,
        'server_versions': {'7': version},
        'stats': {},
        'servers_status': [{'server_id': 7}],
        'last_update': 'now',
    }


class SnapshotRedisSchemaTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(redis_client.GLOBAL_SERVER_DATA)
        redis_client.GLOBAL_SERVER_DATA.update({
            'inbounds': [], 'stats': {}, 'servers_status': [], 'last_update': None,
            'normalized_server_ids': set(),
        })
        redis_client._LAST_LOADED_SNAPSHOT_VERSION = None
        redis_client._LAST_LOADED_SERVER_VERSIONS = {}
        self.addCleanup(self.restore)

    def restore(self):
        redis_client.GLOBAL_SERVER_DATA.clear()
        redis_client.GLOBAL_SERVER_DATA.update(self.saved)
        redis_client._LAST_LOADED_SNAPSHOT_VERSION = None
        redis_client._LAST_LOADED_SERVER_VERSIONS = {}

    def client(self, block, version='new'):
        return FakeRedis({
            redis_client.REDIS_SNAPSHOT_VERSION_KEY: version.encode(),
            redis_client.REDIS_SNAPSHOT_MANIFEST_KEY:
                redis_client._encode_snapshot(manifest(version)),
            redis_client._redis_server_snapshot_key(7):
                redis_client._encode_snapshot(block),
        })

    def test_legacy_block_remains_expanded_and_isolated(self):
        block = [{'server_id': 7, 'id': 10, 'clients': [{'email': 'a@x'}]}]
        with mock.patch.object(redis_client, 'get_redis', return_value=self.client(block)):
            self.assertTrue(redis_client._load_snapshot_from_redis_unlocked(force=True))
        self.assertEqual(redis_client.GLOBAL_SERVER_DATA['inbounds'], block)
        self.assertNotIn(7, redis_client.GLOBAL_SERVER_DATA['normalized_server_ids'])

    def test_v2_block_hydrates_shared_entities(self):
        uuid = '4ce7db6e-4576-4e55-bb2a-452487fe1bb6'
        expanded = [
            {'server_id': 7, 'id': 10, 'clients': [{'id': uuid, 'email': 'a@x'}]},
            {'server_id': 7, 'id': 20, 'clients': [{'id': uuid, 'email': 'a@x'}]},
        ]
        block = snapshot_model.normalize_server_block(expanded, 7)
        with mock.patch.object(redis_client, 'get_redis', return_value=self.client(block)):
            self.assertTrue(redis_client._load_snapshot_from_redis_unlocked(force=True))
        rows = redis_client.GLOBAL_SERVER_DATA['inbounds']
        self.assertIs(rows[0]['clients'][0], rows[1]['clients'][0])
        self.assertIn(7, redis_client.GLOBAL_SERVER_DATA['normalized_server_ids'])

    def test_unknown_schema_keeps_last_good_block_and_version(self):
        old = [{'server_id': 7, 'id': 9, 'clients': []}]
        redis_client.GLOBAL_SERVER_DATA['inbounds'] = old
        redis_client._LAST_LOADED_SERVER_VERSIONS = {7: 'old'}
        unknown = {'schema_version': 99, 'server_id': 7, 'clients': {}, 'inbounds': []}
        with mock.patch.object(redis_client, 'get_redis', return_value=self.client(unknown)):
            self.assertTrue(redis_client._load_snapshot_from_redis_unlocked(force=True))
        self.assertEqual(redis_client.GLOBAL_SERVER_DATA['inbounds'], old)
        self.assertEqual(redis_client._LAST_LOADED_SERVER_VERSIONS[7], 'old')


if __name__ == '__main__':
    unittest.main()
