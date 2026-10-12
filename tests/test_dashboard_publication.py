"""Revision publication is atomic and shared across web-worker snapshot readers."""
import copy
import json
import os
import unittest
from unittest import mock

os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
from panel.core import redis_client as cache, snapshot_delta as delta


class Redis:
    def __init__(self):
        self.values = {}
        self.lists = {}
        self.fail = False

    def get(self, key):
        return self.values.get(key)

    def lrange(self, key, start, stop):
        return self.lists.get(key, [])[start:stop + 1]

    def pipeline(self):
        return Pipeline(self)


class Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def watch(self, *keys):
        pass

    def unwatch(self):
        pass

    def reset(self):
        pass

    def get(self, key):
        return self.redis.get(key)

    def multi(self):
        pass

    def set(self, key, value, **kwargs):
        self.commands.append(('set', key, value))

    def lpush(self, key, value):
        self.commands.append(('lpush', key, value))

    def ltrim(self, key, start, stop):
        self.commands.append(('ltrim', key, (start, stop)))

    def expire(self, *args):
        pass

    def execute(self):
        if self.redis.fail:
            raise RuntimeError('publication unavailable')
        for operation, key, value in self.commands:
            if operation == 'set':
                self.redis.values[key] = value
            elif operation == 'lpush':
                self.redis.lists.setdefault(key, []).insert(0, value)
            elif operation == 'ltrim':
                start, stop = value
                self.redis.lists[key] = self.redis.lists.get(key, [])[start:stop + 1]


class DashboardPublicationTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cache.GLOBAL_SERVER_DATA)
        self.loaded = cache._LAST_LOADED_SNAPSHOT_VERSION
        self.versions = dict(cache._LAST_LOADED_SERVER_VERSIONS)
        self.published = dict(cache._PUBLISHED_SERVER_VERSIONS)
        self.redis = Redis()
        patch = mock.patch.object(cache, 'get_redis', return_value=self.redis)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.restore)
        cache._LAST_LOADED_SNAPSHOT_VERSION = None
        cache._LAST_LOADED_SERVER_VERSIONS = {}
        delta.reset_state()
        cache.GLOBAL_SERVER_DATA.clear()
        cache.GLOBAL_SERVER_DATA.update({
            'inbounds': [{'server_id': sid, 'id': 1, 'remark': f'ECO{sid}', 'clients': []}
                         for sid in (4, 5)],
            'servers_status': [{'server_id': sid} for sid in (4, 5)],
            'stats': {}, 'last_update': 'first',
        })

    def restore(self):
        cache.GLOBAL_SERVER_DATA.clear()
        cache.GLOBAL_SERVER_DATA.update(self.saved)
        cache._LAST_LOADED_SNAPSHOT_VERSION = self.loaded
        cache._LAST_LOADED_SERVER_VERSIONS = self.versions
        cache._PUBLISHED_SERVER_VERSIONS = self.published
        delta.reset_state()

    def test_two_worker_readers_adopt_one_revision_and_fetch_only_changed_inbound(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        first = delta.current_revision(cache.GLOBAL_SERVER_DATA)
        eco5 = copy.deepcopy(cache.GLOBAL_SERVER_DATA['inbounds'][1])
        cache.GLOBAL_SERVER_DATA['inbounds'][0]['remark'] = 'renamed ECO4'
        cache.GLOBAL_SERVER_DATA['last_update'] = 'second'
        self.assertTrue(cache.publish_snapshot_to_redis([4]))
        published = cache.GLOBAL_SERVER_DATA['_published_revision']
        for worker in range(2):
            with self.subTest(worker=worker):
                delta.reset_state()
                self.assertTrue(cache.load_snapshot_from_redis(force=True))
                sync = delta.build_sync(cache.GLOBAL_SERVER_DATA, first)
                self.assertEqual(sync['revision'], published)
                self.assertEqual(sync['mode'], 'delta')
                self.assertEqual(sync['changed'], [[4, 1]])
                self.assertEqual(cache.GLOBAL_SERVER_DATA['inbounds'][1], eco5)
        self.assertEqual(len(self.redis.lists[delta.REDIS_HISTORY_KEY]), 2)

    def test_failed_publication_does_not_advance_revision_or_history(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        first = delta.current_revision(cache.GLOBAL_SERVER_DATA)
        history = list(self.redis.lists[delta.REDIS_HISTORY_KEY])
        self.redis.fail = True
        cache.GLOBAL_SERVER_DATA['inbounds'][0]['remark'] = 'unpublished'
        cache.GLOBAL_SERVER_DATA['last_update'] = 'unpublished'
        delta.mark_dirty(server_ids=[4])
        self.assertFalse(cache.publish_snapshot_to_redis([4]))
        self.assertEqual(delta.current_revision(cache.GLOBAL_SERVER_DATA), first)
        self.assertEqual(self.redis.lists[delta.REDIS_HISTORY_KEY], history)

    def test_missing_history_recovers_with_full_snapshot(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        first = delta.current_revision(cache.GLOBAL_SERVER_DATA)
        cache.GLOBAL_SERVER_DATA['last_update'] = 'second'
        self.assertTrue(cache.publish_snapshot_to_redis([4]))
        self.redis.lists[delta.REDIS_HISTORY_KEY] = []
        delta.reset_state()
        sync = delta.build_sync(cache.GLOBAL_SERVER_DATA, first)
        self.assertEqual(sync['mode'], 'full')
        self.assertEqual(sync['reason'], 'unknown_revision')

    def test_removed_then_recreated_inbound_is_in_latest_delta(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        first = delta.current_revision(cache.GLOBAL_SERVER_DATA)
        cache.GLOBAL_SERVER_DATA['inbounds'] = [row for row in cache.GLOBAL_SERVER_DATA['inbounds']
                                               if row['server_id'] != 4]
        cache.GLOBAL_SERVER_DATA['last_update'] = 'removed'
        self.assertTrue(cache.publish_snapshot_to_redis([4]))
        cache.GLOBAL_SERVER_DATA['inbounds'].append({'server_id': 4, 'id': 1, 'remark': 'recreated', 'clients': []})
        cache.GLOBAL_SERVER_DATA['last_update'] = 'recreated'
        self.assertTrue(cache.publish_snapshot_to_redis([4]))
        sync = delta.build_sync(cache.GLOBAL_SERVER_DATA, first)
        self.assertEqual(sync['changed'], [[4, 1]])
        self.assertEqual(sync['removed'], [])

    def test_mutation_fence_rejects_older_scheduler_publication(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        revision = cache.GLOBAL_SERVER_DATA['_published_revision']
        self.redis.values[cache._redis_server_revision_key(4)] = 8
        self.assertFalse(cache.publish_snapshot_to_redis([4], expected_server_revisions={4: 7}))
        self.assertEqual(cache.GLOBAL_SERVER_DATA['_published_revision'], revision)

    def test_missing_server_block_cannot_acknowledge_manifest_and_is_retried(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        revision = cache.GLOBAL_SERVER_DATA['_published_revision']
        manifest = cache._decode_snapshot(self.redis.values[cache.REDIS_SNAPSHOT_MANIFEST_KEY])
        manifest['server_versions'][4] = 'external'
        manifest['revision'] = revision + 1
        self.redis.values[cache.REDIS_SNAPSHOT_MANIFEST_KEY] = cache._encode_snapshot(manifest)
        self.redis.values[cache.REDIS_SNAPSHOT_VERSION_KEY] = b'external'
        del self.redis.values[cache._redis_server_snapshot_key(4)]
        cache.load_snapshot_from_redis()
        self.assertEqual(cache.GLOBAL_SERVER_DATA['_published_revision'], revision)
        self.assertNotEqual(cache._LAST_LOADED_SNAPSHOT_VERSION, b'external')
        self.redis.values[cache._redis_server_snapshot_key(4)] = cache._encode_snapshot([
            {'server_id': 4, 'id': 2, 'clients': []}])
        self.assertTrue(cache.load_snapshot_from_redis())
        self.assertEqual(cache.GLOBAL_SERVER_DATA['_published_revision'], revision + 1)

    def test_event_history_contains_keys_without_client_objects(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        event = json.loads(self.redis.lists[delta.REDIS_HISTORY_KEY][0])
        self.assertEqual(sorted(event['changed']), [[4, 1], [5, 1]])
        self.assertNotIn('clients', event)

    def test_publishing_one_server_keeps_another_unpublished_fetch(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        cache.GLOBAL_SERVER_DATA['inbounds'][1]['remark'] = 'new unpublished ECO5'
        cache.GLOBAL_SERVER_DATA['last_update'] = 'new fetch'
        self.assertTrue(cache.publish_snapshot_to_redis([4]))
        self.assertEqual(cache.GLOBAL_SERVER_DATA['inbounds'][1]['remark'], 'new unpublished ECO5')
        self.assertTrue(cache.publish_snapshot_to_redis([5]))
        cache.GLOBAL_SERVER_DATA['inbounds'] = []
        cache.load_snapshot_from_redis(force=True)
        self.assertEqual(cache.GLOBAL_SERVER_DATA['inbounds'][1]['remark'], 'new unpublished ECO5')

    def test_partial_block_cannot_replace_a_published_successful_server(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        original = self.redis.values[cache._redis_server_snapshot_key(4)]
        cache.GLOBAL_SERVER_DATA['inbounds'] = [row for row in cache.GLOBAL_SERVER_DATA['inbounds'] if row['server_id'] != 4]
        cache.GLOBAL_SERVER_DATA['servers_status'][0]['stats'] = {'total_inbounds': 1}
        self.assertFalse(cache.publish_snapshot_to_redis([4]))
        self.assertEqual(self.redis.values[cache._redis_server_snapshot_key(4)], original)

    def test_reader_preserves_fetched_block_awaiting_publication(self):
        self.assertTrue(cache.publish_snapshot_to_redis())
        cache.GLOBAL_SERVER_DATA['inbounds'][1]['remark'] = 'not yet published'
        cache.GLOBAL_SERVER_DATA['_pending_snapshot_servers'] = {5}
        self.assertTrue(cache.load_snapshot_from_redis(force=True))
        self.assertEqual(cache.GLOBAL_SERVER_DATA['inbounds'][1]['remark'], 'not yet published')
        self.assertFalse(cache.GLOBAL_SERVER_DATA['_snapshot_complete'])
        self.assertTrue(cache.publish_snapshot_to_redis([5]))
        cache.load_snapshot_from_redis(force=True)
        self.assertTrue(cache.GLOBAL_SERVER_DATA['_snapshot_complete'])
