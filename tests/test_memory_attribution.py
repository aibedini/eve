"""The on-host collector: it must never import the app, and never invent zeroes.

``scripts/memory_attribution.py`` runs on a live install, so the two properties worth
pinning are the ones that make it safe there: it cannot drag the application (and its
import-time migrations) into its own process, and a host or a Redis it cannot read reports
"unavailable" instead of a confident ``0 B``.

Every fixture here is mocked, so the module behaves identically on a Linux CI runner and
on a Windows dev checkout.
"""
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'scripts'))

os.environ.setdefault('FLASK_ENV', 'development')
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')

import memory_attribution as collector  # noqa: E402
from panel.core import memory_report  # noqa: E402

MB = 1024 ** 2
GB = 1024 ** 3

HOST = {
    'available': True,
    'total_bytes': 4 * GB,
    'available_bytes': 860 * MB,
    'free_bytes': 120 * MB,
    'used_bytes': 3 * GB + 236 * MB,
    'cache_bytes': 620 * MB,
    'swap_total_bytes': 2 * GB,
    'swap_used_bytes': 40 * MB,
    'available_pct': 21.0,
    'pressure': {'available': True, 'some_avg10': 1.2, 'full_avg10': 0.1},
    'health': 'ok',
}


def _bucket(pss_bytes, *, processes=1, threads=4, pids=(10,)):
    return {'processes': processes, 'rss_bytes': pss_bytes + 40, 'pss_bytes': pss_bytes,
            'private_bytes': pss_bytes - 20, 'threads': threads,
            'peak_rss_bytes': pss_bytes + 80, 'pids': list(pids),
            'max_uptime_seconds': 600.0, 'pss_approximated': False}


EVE = {
    'available': True,
    'roles': {
        'web': _bucket(420 * MB, pids=(10,)),
        'background': _bucket(610 * MB, threads=19, pids=(11,)),
        'xray': _bucket(42 * MB, processes=3, pids=(12, 13, 14)),
    },
    'services': {
        'redis': _bucket(74 * MB, threads=6, pids=(20,)),
        'postgres': _bucket(182 * MB, processes=8, threads=16, pids=(21,)),
    },
    'other_processes': 37,
    'other_rss_bytes': 300 * MB,
    'other_pss_bytes': 260 * MB,
    'other_private_bytes': 250 * MB,
    'other_threads': 45,
    'eve_pss_bytes': 1030 * MB,
    'eve_pss_with_xray_bytes': 1072 * MB,
    'service_pss_bytes': 256 * MB,
}

UNCLASSIFIED = {'available': True, 'count': 0, 'truncated': False, 'processes': []}


def _patched(host=HOST, eve=EVE, unclassified=UNCLASSIFIED):
    return (
        mock.patch.object(memory_report, 'host_memory', return_value=host),
        mock.patch.object(memory_report, 'eve_processes', return_value=eve),
        mock.patch.object(memory_report, 'unclassified_processes', return_value=unclassified),
    )


class CollectorSafetyTests(unittest.TestCase):
    def test_the_collector_never_reads_the_in_process_snapshot(self):
        # snapshot_footprint() imports the app, which runs its import-time migrations in
        # this process. A read-only collector must never be able to do that.
        host, eve, unclassified = _patched()
        with host, eve, unclassified, \
                mock.patch.object(memory_report, 'snapshot_footprint',
                                  side_effect=AssertionError('the collector imported the app')):
            data = collector.collect(top=3, now=1000.0)
        self.assertNotIn('snapshot', data)
        self.assertIn('not included', data['collector']['note'])

    def test_an_unreadable_host_is_unavailable_and_never_reconciled_to_zero(self):
        host, eve, unclassified = _patched(
            host={'available': False, 'reason': 'no /proc/meminfo'},
            eve={'available': False, 'reason': 'no /proc', 'roles': {}})
        with host, eve, unclassified:
            data = collector.collect(top=1, now=1000.0)
        text = collector.render(data)
        self.assertIn('no /proc/meminfo', text)
        self.assertIn('not reconciled', text)
        self.assertNotIn('0 B of', text)


class CollectorReportTests(unittest.TestCase):
    def _data(self, **kwargs):
        host, eve, unclassified = _patched(**kwargs)
        with host, eve, unclassified:
            return collector.collect(top=5, now=1000.0)

    def test_host_services_are_shown_and_kept_out_of_the_eve_figure(self):
        text = collector.render(self._data())
        self.assertIn('HOST SERVICES (not EVE)', text)
        self.assertIn('postgres', text)
        self.assertIn('host service, not EVE', text)
        # EVE is reported as EVE: the services are separate lines, not folded in.
        self.assertIn(collector.human_bytes(EVE['eve_pss_bytes']), text)

    def test_the_report_reconciles_the_host_and_names_the_residual(self):
        data = self._data()
        acct = data['accounting']
        self.assertEqual(acct['process_pss_bytes'],
                         EVE['eve_pss_with_xray_bytes'] + EVE['service_pss_bytes']
                         + EVE['other_pss_bytes'])
        expected_residual = (HOST['total_bytes'] - HOST['free_bytes']
                             - HOST['cache_bytes'] - acct['process_pss_bytes'])
        self.assertEqual(acct['residual_bytes'], expected_residual)
        text = collector.render(data)
        self.assertIn('Residual', text)
        self.assertIn('no process owns it', text)
        self.assertIn('Sum', text)
        self.assertIn(collector.human_bytes(HOST['total_bytes']), text)

    def test_an_unreadable_proc_says_unavailable_and_invents_no_counts(self):
        host, eve, unclassified = _patched(
            eve={'available': False, 'reason': 'no /proc', 'roles': {}, 'services': {},
                 'other_processes': None, 'other_rss_bytes': None, 'other_pss_bytes': None,
                 'other_private_bytes': None, 'other_threads': None, 'eve_pss_bytes': None,
                 'eve_pss_with_xray_bytes': None, 'service_pss_bytes': None})
        with host, eve, unclassified:
            text = collector.render(collector.collect(top=1, now=1000.0))
        self.assertIn('unavailable: no /proc', text)
        self.assertNotIn('None', text)
        self.assertNotIn('0 processes', text)

    def test_the_payload_is_the_endpoint_shape_plus_the_collector_note(self):
        data = self._data()
        for key in ('host', 'eve', 'accounting', 'redis_snapshot', 'caches', 'trend',
                    'health', 'sampled_at', 'pid', 'process_role'):
            self.assertIn(key, data)
        self.assertIn('unclassified', data)
        self.assertEqual(data['collector']['top'], 5)

    def test_the_role_and_service_tables_carry_every_phase_one_column(self):
        text = collector.render(self._data())
        for header in ('PROCS', 'RSS', 'PSS', 'USS', 'THREADS', 'PEAK', 'PIDS'):
            self.assertIn(header, text)
        self.assertIn('12,13,14', text)          # the managed Xray pids


class HumanBytesTests(unittest.TestCase):
    def test_unknown_is_never_rendered_as_zero(self):
        self.assertEqual(collector.human_bytes(None), '?')
        self.assertEqual(collector.human_bytes('not-a-number'), '?')
        self.assertEqual(collector.human_bytes(0), '0 B')

    def test_units_and_negative_values(self):
        self.assertEqual(collector.human_bytes(1536), '1.50 KB')
        self.assertEqual(collector.human_bytes(3 * MB), '3.00 MB')
        self.assertEqual(collector.human_bytes(-2048), '-2.00 KB')


class OrderingTests(unittest.TestCase):
    def test_known_keys_come_first_and_unknown_keys_are_sorted(self):
        self.assertEqual(collector.ordered({'zeta', 'web', 'alpha'}, ('web', 'background')),
                         ['web', 'alpha', 'zeta'])


if __name__ == '__main__':
    unittest.main()
