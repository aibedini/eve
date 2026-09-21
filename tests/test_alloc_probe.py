"""Focused tests for the one-shot allocator diagnostic (panel/core/alloc_probe.py).

Scope is deliberately narrow: the parsing and fallbacks that must not guess, the one-shot
protocol, the staging of the experiment (with an injected sleep, so nothing waits 30 s), and
the no-PII contract. Route-level security lives with the other memory-route tests.
"""
import json
import types
import unittest
from unittest import mock

from panel.core import alloc_probe

MiB = 1024 * 1024
FORBIDDEN = ('password', 'secret', 'token', 'api_key', 'authorization', 'bearer',
             'email', 'phone', 'hostname', 'credential')


class _FakeRedis:
    """Minimal stand-in: set(nx=), get, delete, plus TTL capture."""

    def __init__(self):
        self.store = {}
        self.sets = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.sets.append((key, ex))
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


def _patched_redis(fake):
    return mock.patch('panel.core.redis_client.get_redis', return_value=fake)


class Mallinfo2ParsingTests(unittest.TestCase):
    def test_the_requested_fields_are_parsed_as_integers(self):
        struct = types.SimpleNamespace(arena=111, ordblks=2, hblkhd=3, uordblks=4,
                                       fordblks=5, keepcost=6, smblks=7, hblks=8,
                                       usmblks=9, fsmblks=10)
        row = alloc_probe._mallinfo2_from(struct)
        self.assertTrue(row['available'])
        self.assertEqual(row['arena'], 111)
        self.assertEqual(row['fordblks'], 5)
        self.assertEqual(row['keepcost'], 6)

    def test_a_missing_field_is_none_rather_than_zero(self):
        row = alloc_probe._mallinfo2_from(types.SimpleNamespace(arena=1))
        self.assertEqual(row['arena'], 1)
        self.assertIsNone(row['uordblks'])

    def test_a_non_glibc_libc_reports_unavailable_instead_of_guessing(self):
        with mock.patch.object(alloc_probe, '_load_libc', return_value=None):
            row = alloc_probe.mallinfo2()
        self.assertFalse(row['available'])
        self.assertIn('reason', row)

    def test_a_libc_without_mallinfo2_reports_unavailable(self):
        with mock.patch.object(alloc_probe, '_load_libc',
                               return_value=types.SimpleNamespace()):
            row = alloc_probe.mallinfo2()
        self.assertFalse(row['available'])
        self.assertIn('mallinfo2', row['reason'])

    def test_a_libc_without_malloc_trim_reports_unavailable(self):
        with mock.patch.object(alloc_probe, '_load_libc',
                               return_value=types.SimpleNamespace()):
            row = alloc_probe.malloc_trim()
        self.assertFalse(row['available'])
        self.assertIn('malloc_trim', row['reason'])

    def test_malloc_trim_reports_what_the_call_returned(self):
        calls = []

        class _Libc:
            @staticmethod
            def malloc_trim(value):
                calls.append(value)
                return 1

        with mock.patch.object(alloc_probe, '_load_libc', return_value=_Libc()):
            row = alloc_probe.malloc_trim()
        self.assertTrue(row['available'])
        self.assertTrue(row['trimmed'])
        self.assertEqual(calls, [0])


class ProcMemoryTests(unittest.TestCase):
    SMAPS = ('Rss:             720896 kB\nPss:             656384 kB\n'
             'Pss_Anon:        520192 kB\nPss_File:        120192 kB\n'
             'Pss_Shmem:        16000 kB\nPrivate_Clean:    40960 kB\n'
             'Private_Dirty:   577536 kB\n')
    STATUS = 'VmRSS:\t  720896 kB\nVmSwap:\t    2048 kB\nThreads:\t19\n'

    def test_pss_and_uss_are_reported_from_proc(self):
        files = {'/proc/self/smaps_rollup': self.SMAPS, '/proc/self/status': self.STATUS}
        with mock.patch.object(alloc_probe, '_read', side_effect=files.get):
            row = alloc_probe.proc_memory()
        self.assertTrue(row['available'])
        self.assertEqual(row['pss_bytes'], 656384 * 1024)
        self.assertEqual(row['pss_anon_bytes'], 520192 * 1024)
        # USS is the private part: clean + dirty.
        self.assertEqual(row['uss_bytes'], (40960 + 577536) * 1024)
        self.assertEqual(row['threads'], 19)
        self.assertEqual(row['swap_bytes'], 2048 * 1024)

    def test_a_host_without_proc_is_unavailable_not_zero(self):
        with mock.patch.object(alloc_probe, '_read', return_value=None):
            row = alloc_probe.proc_memory()
        self.assertFalse(row['available'])
        self.assertIsNone(row.get('pss_bytes'))


class ArenaMaxTests(unittest.TestCase):
    def test_unset_is_reported_as_unset(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertEqual(alloc_probe.arena_max(), {'set': False, 'value': None})

    def test_a_numeric_value_is_reported_as_a_number(self):
        with mock.patch.dict('os.environ', {'MALLOC_ARENA_MAX': '2'}, clear=True):
            self.assertEqual(alloc_probe.arena_max(), {'set': True, 'value': 2})

    def test_a_non_numeric_value_is_not_invented(self):
        with mock.patch.dict('os.environ', {'MALLOC_ARENA_MAX': 'lots'}, clear=True):
            row = alloc_probe.arena_max()
        self.assertTrue(row['set'])
        self.assertIsNone(row['value'])


class ClassificationTests(unittest.TestCase):
    def test_gc_reclaim_when_collect_frees_a_material_amount(self):
        self.assertEqual(alloc_probe.classify(baseline_pss=1000 * MiB, after_gc_pss=500 * MiB,
                                              after_trim_pss=500 * MiB), 'GC_RECLAIM')

    def test_fragmentation_when_only_trim_frees_a_material_amount(self):
        self.assertEqual(alloc_probe.classify(baseline_pss=1000 * MiB, after_gc_pss=990 * MiB,
                                              after_trim_pss=700 * MiB),
                         'GLIBC_FRAGMENTATION')

    def test_live_retained_when_neither_moves_materially(self):
        self.assertEqual(alloc_probe.classify(baseline_pss=1000 * MiB, after_gc_pss=999 * MiB,
                                              after_trim_pss=995 * MiB), 'LIVE_RETAINED')

    def test_an_unknown_baseline_is_unknown_not_live_retained(self):
        self.assertEqual(alloc_probe.classify(baseline_pss=None, after_gc_pss=10,
                                              after_trim_pss=5), 'UNKNOWN')
        self.assertEqual(alloc_probe.classify(baseline_pss=10, after_gc_pss=10,
                                              after_trim_pss=None), 'UNKNOWN')


class OneShotProtocolTests(unittest.TestCase):
    def setUp(self):
        alloc_probe.reset_for_tests()
        self.fake = _FakeRedis()

    def test_a_request_is_claimed_once_and_refused_while_pending(self):
        with _patched_redis(self.fake):
            first = alloc_probe.request_probe(now=lambda: 100.0)
            second = alloc_probe.request_probe(now=lambda: 101.0)
            pending = alloc_probe.read_request(now=lambda: 105.0)
        self.assertTrue(first['ok'])
        self.assertFalse(second['ok'])
        self.assertIn('already pending', second['reason'])
        self.assertTrue(pending['pending'])
        self.assertEqual(pending['probe_id'], first['probe_id'])
        self.assertEqual(pending['age_seconds'], 5.0)
        # One request at a time: exactly one key was written.
        self.assertEqual([key for key, _ in self.fake.sets], [alloc_probe.REQUEST_KEY])

    def test_the_request_and_result_carry_bounded_ttls(self):
        with _patched_redis(self.fake):
            alloc_probe.request_probe(now=lambda: 100.0)
            alloc_probe.publish_result({'probe_id': 'x'})
        ttls = dict(self.fake.sets)
        self.assertEqual(ttls[alloc_probe.REQUEST_KEY], alloc_probe.REQUEST_TTL_SECONDS)
        self.assertEqual(ttls[alloc_probe.RESULT_KEY], alloc_probe.RESULT_TTL_SECONDS)

    def test_watch_once_is_a_no_op_when_nothing_was_requested(self):
        with _patched_redis(self.fake), \
                mock.patch.object(alloc_probe, 'run') as runner:
            self.assertFalse(alloc_probe.watch_once(now=lambda: 100.0))
        runner.assert_not_called()
        self.assertNotIn(alloc_probe.RESULT_KEY, self.fake.store)

    def test_watch_once_consumes_the_request_and_runs_exactly_once(self):
        with _patched_redis(self.fake):
            alloc_probe.request_probe(now=lambda: 100.0)
            with mock.patch.object(alloc_probe, 'run',
                                   return_value={'probe_id': 'p', 'classification': 'X'}) as runner:
                self.assertTrue(alloc_probe.watch_once(now=lambda: 101.0))
                self.assertFalse(alloc_probe.watch_once(now=lambda: 102.0))
        self.assertEqual(runner.call_count, 1)
        self.assertNotIn(alloc_probe.REQUEST_KEY, self.fake.store)
        self.assertIn(alloc_probe.RESULT_KEY, self.fake.store)

    def test_the_result_states_are_pending_then_done_then_none(self):
        with _patched_redis(self.fake):
            state = alloc_probe.read_result(now=lambda: 100.0)
            self.assertEqual(state['state'], 'none')
            alloc_probe.request_probe(now=lambda: 100.0)
            self.assertEqual(alloc_probe.read_result(now=lambda: 101.0)['state'], 'pending')
            alloc_probe.take_request(now=lambda: 102.0)
            self.assertEqual(alloc_probe.read_result(now=lambda: 103.0)['state'], 'none')
            alloc_probe.publish_result({'probe_id': 'done-1'})
            done = alloc_probe.read_result(now=lambda: 104.0)
        self.assertEqual(done['state'], 'done')
        self.assertEqual(done['result']['probe_id'], 'done-1')

    def test_no_redis_is_reported_rather_than_crashing(self):
        with mock.patch('panel.core.redis_client.get_redis', return_value=None):
            self.assertFalse(alloc_probe.request_probe()['ok'])
            self.assertFalse(alloc_probe.read_result()['available'])
            self.assertFalse(alloc_probe.publish_result({}))
            self.assertIsNone(alloc_probe.watch_once() or None)


class StagedExperimentTests(unittest.TestCase):
    """The staging and the wiring; the classification logic is tested above."""

    READINGS = [{'available': True, 'pss_bytes': 1000 * MiB},
                {'available': True, 'pss_bytes': 950 * MiB},
                {'available': True, 'pss_bytes': 900 * MiB},
                {'available': True, 'pss_bytes': 500 * MiB},
                {'available': True, 'pss_bytes': 500 * MiB}]

    def _run(self, **overrides):
        slept = []
        patches = {
            'proc_memory': mock.patch.object(alloc_probe, 'proc_memory',
                                            side_effect=list(self.READINGS)),
            'cache_counts': mock.patch.object(alloc_probe, 'cache_counts',
                                              return_value={'db_identity_map': 3}),
            'mallinfo2': mock.patch.object(alloc_probe, 'mallinfo2',
                                           return_value={'available': True, 'arena': 1}),
            'malloc_trim': mock.patch.object(alloc_probe, 'malloc_trim',
                                             return_value={'available': True, 'trimmed': True}),
            'collect_once': mock.patch.object(alloc_probe, 'collect_once',
                                              return_value={'collected': 7, 'counts': [1, 2],
                                                            'stats': None}),
            'classify': mock.patch.object(alloc_probe, 'classify',
                                          return_value='GLIBC_FRAGMENTATION'),
        }
        patches.update(overrides)
        with patches['proc_memory'], patches['cache_counts'], patches['mallinfo2'], \
                patches['malloc_trim'], patches['collect_once'], patches['classify'] as cls:
            result = alloc_probe.run('probe-1', sleep=slept.append,
                                     trim_delays=(0.0, 0.5, 1.0),
                                     now=lambda: 1000.0)
        return result, slept, cls

    def test_the_stages_are_recorded_in_order(self):
        result, slept, _ = self._run()
        self.assertEqual(result['probe_id'], 'probe-1')
        self.assertEqual(result['baseline']['memory']['pss_bytes'], 1000 * MiB)
        self.assertEqual(result['after_gc']['collected'], 7)
        self.assertEqual(result['after_gc']['delta_pss_bytes'], -50 * MiB)
        self.assertEqual([s['after_seconds'] for s in result['trim']['samples']], [0.0, 0.5, 1.0])
        self.assertIn('classification', result)

    def test_it_waits_only_between_the_settle_samples(self):
        _, slept, _ = self._run()
        # Delays 0.0 -> 0.5 -> 1.0 are slept as the deltas, so nothing waits 30 s here.
        self.assertEqual(slept, [0.5, 0.5])

    def test_the_classification_is_wired_to_the_measured_pss_values(self):
        _, _, cls = self._run()
        cls.assert_called_once_with(baseline_pss=1000 * MiB, after_gc_pss=950 * MiB,
                                   after_trim_pss=500 * MiB)

    def test_a_host_without_malloc_trim_still_reports_the_other_stages(self):
        result, slept, _ = self._run(malloc_trim=mock.patch.object(
            alloc_probe, 'malloc_trim',
            return_value={'available': False, 'reason': 'not glibc'}))
        self.assertFalse(result['trim']['outcome']['available'])
        self.assertEqual(result['trim']['samples'], [])
        self.assertEqual(slept, [])

    def test_the_result_carries_no_customer_data_or_secrets(self):
        result, _, _ = self._run()
        text = json.dumps(result).lower()
        for forbidden in FORBIDDEN:
            self.assertNotIn(forbidden, text, forbidden)
        self._assert_plain_types(result)

    def _assert_plain_types(self, value, path='result'):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertIsInstance(key, str)
                self._assert_plain_types(item, '%s.%s' % (path, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._assert_plain_types(item, '%s[%d]' % (path, index))
        else:
            self.assertIn(type(value), (int, float, bool, str, type(None)), path)

    def test_the_note_says_the_experiment_is_one_shot(self):
        result, _, _ = self._run()
        self.assertIn('one-shot', result['note'])


if __name__ == '__main__':
    unittest.main()
