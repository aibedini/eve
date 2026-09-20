"""Bounded and privacy-safe background memory lifecycle telemetry."""
import unittest
from unittest import mock

from panel.core import memory_probe


class MemoryProbeTests(unittest.TestCase):
    def setUp(self):
        memory_probe.reset_for_tests()

    def test_records_ordered_aggregate_only_checkpoints(self):
        readings = iter((
            {'pss_bytes': 100, 'uss_bytes': 80, 'pss_is_rss_fallback': False},
            {'pss_bytes': 130, 'uss_bytes': 90, 'pss_is_rss_fallback': False},
        ))
        with mock.patch.object(memory_probe, '_memory', side_effect=lambda: next(readings)):
            memory_probe.record('cycle-1', 'idle_before_fetch',
                                counts={'servers': 2, 'email': 'private@example.com'})
            memory_probe.record('cycle-1', 'after_panel_fetch',
                                counts={'inbounds': 4, 'raw_client_rows': 9,
                                        'inflight_work': 1,
                                        'retained_server_results': 2,
                                        'raw_client': {'secret': True}})
        rows = memory_probe.report()['samples']
        self.assertEqual([row['checkpoint'] for row in rows],
                         ['idle_before_fetch', 'after_panel_fetch'])
        self.assertEqual(rows[0]['counts'], {'servers': 2})
        self.assertEqual(rows[1]['counts'], {
            'inbounds': 4, 'raw_client_rows': 9, 'inflight_work': 1,
            'retained_server_results': 2})
        self.assertNotIn('private@example.com', repr(rows))
        self.assertNotIn('secret', repr(rows))

    def test_ring_is_fixed_size(self):
        with mock.patch.object(memory_probe, '_memory', return_value={
                'pss_bytes': 1, 'uss_bytes': 1, 'pss_is_rss_fallback': False}):
            for index in range(memory_probe.MAX_SAMPLES + 7):
                memory_probe.record(index, 'after_snapshot_commit')
        rows = memory_probe.report()['samples']
        self.assertEqual(len(rows), memory_probe.MAX_SAMPLES)
        self.assertEqual(rows[0]['cycle_id'], '7')

    def test_unknown_checkpoint_is_rejected(self):
        with self.assertRaises(ValueError):
            memory_probe.record('x', 'contains_client_payload')

    def test_settled_timer_captures_counts_not_payload_objects(self):
        callbacks = []

        class FakeTimer:
            daemon = False

            def __init__(self, delay, callback):
                self.delay = delay
                callbacks.append(callback)

            def start(self):
                return None

        with mock.patch.object(memory_probe.threading, 'Timer', FakeTimer), \
                mock.patch.object(memory_probe, '_memory', return_value={
                    'pss_bytes': 1, 'uss_bytes': 1, 'pss_is_rss_fallback': False}):
            memory_probe.schedule_settled('c1', counts={
                'client_entities': 3, 'payload': object()}, delay=0)
            callbacks[0]()
        self.assertEqual(memory_probe.report()['samples'][0]['counts'],
                         {'client_entities': 3})

    def test_report_reads_the_background_ring_cross_process(self):
        class FakeRedis:
            def __init__(self):
                self.items = []

            def lpush(self, key, value):
                self.items.insert(0, value)

            def ltrim(self, key, start, stop):
                self.items = self.items[start:stop + 1]

            def expire(self, key, ttl):
                return True

            def lrange(self, key, start, stop):
                return self.items[start:stop + 1]

        fake = FakeRedis()
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake), \
                mock.patch.object(memory_probe, '_memory', return_value={
                    'pss_bytes': 4, 'uss_bytes': 3, 'pss_is_rss_fallback': False}):
            memory_probe.record('background-1', 'idle_before_fetch', counts={'servers': 2})
            # Simulate the web process: its local ring is empty, Redis is shared.
            memory_probe.reset_for_tests()
            report = memory_probe.report()
        self.assertEqual(report['source'], 'redis')
        self.assertEqual(report['samples'][0]['cycle_id'], 'background-1')
        self.assertEqual(report['samples'][0]['counts'], {'servers': 2})


if __name__ == '__main__':
    unittest.main()
