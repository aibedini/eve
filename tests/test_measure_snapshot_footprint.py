"""The snapshot measurement's counters: rows, not strings, and one shared vocabulary.

``scripts/measure_snapshot_footprint.py`` is the measurement half of
``docs/performance/MEMORY.md``: it produces the byte-level attribution that ranks the
optimization plan. The ranking is only sound if the script and the live endpoint
(``panel.core.memory_report.snapshot_footprint``, rendered by Settings -> Overview) mean
the same thing by the same counter name, so the consistency test below is the point of
this module.

The counters are deliberately app-free (``row_stats`` takes a plain snapshot), which is
what makes them testable without importing the application.
"""
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'scripts'))

os.environ.setdefault('FLASK_ENV', 'development')
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')

import measure_snapshot_footprint as footprint  # noqa: E402
from panel.core import memory_report  # noqa: E402


def _client(uid, *, raw=True, formatted=True):
    row = {
        'id': uid,
        'email': '%s@example.com' % uid,
        'up': 1024,
        'down': 2048,
        'totalGB': 5.0,
        'enable': True,
    }
    if formatted:
        # Four formatted keys, the production field set: counting the strings instead of
        # the rows multiplies the reported row count by exactly this many.
        row.update({
            'up_formatted': '1 KB',
            'down_formatted': '2 KB',
            'totalGB_formatted': '5.0 GB',
            'remaining_formatted': '4.9 GB',
        })
    if raw:
        row['raw_client'] = {'id': uid, 'email': row['email'], 'enable': True,
                             'totalGB': 5.0, 'expiryTime': 0}
    return row


def _snapshot(clients_per_inbound):
    """One server, one inbound per uid list - the shape ``process_inbounds`` caches."""
    inbounds = []
    for index, uids in enumerate(clients_per_inbound):
        inbounds.append({
            'server_id': 1,
            'id': 100 + index,
            'remark': 'inbound-%d' % index,
            'clients': [_client(uid) for uid in uids],
        })
    return {'inbounds': inbounds, 'servers_status': [{'id': 1, 'active': True}]}


class RowStatsTests(unittest.TestCase):
    def test_formatted_rows_count_rows_not_strings(self):
        stats = footprint.row_stats(_snapshot([['a', 'b', 'c']])['inbounds'])
        self.assertEqual(stats['rows'], 3)
        self.assertEqual(stats['rows_with_formatted_strings'], 3)
        # The per-key histogram is still per key: that is what tells an implementer which
        # strings a row carries.
        self.assertEqual(stats['formatted_keys']['up_formatted'], 3)
        self.assertEqual(len(stats['formatted_keys']), 4)

    def test_a_row_without_formatted_strings_is_not_counted(self):
        snapshot = _snapshot([['a']])
        snapshot['inbounds'][0]['clients'].append(_client('b', formatted=False))
        stats = footprint.row_stats(snapshot['inbounds'])
        self.assertEqual(stats['rows'], 2)
        self.assertEqual(stats['rows_with_formatted_strings'], 1)
        self.assertEqual(stats['rows_with_raw_client'], 2)

    def test_the_v3_mirror_is_counted_as_rows_over_unique_clients(self):
        # The same account assigned to three inbounds, which is what v3 returns.
        stats = footprint.row_stats(_snapshot([['a'], ['a'], ['a']])['inbounds'])
        self.assertEqual(stats['rows'], 3)
        self.assertEqual(stats['unique_clients'], 1)

    def test_the_script_and_the_live_endpoint_agree_on_every_counter(self):
        snapshot = _snapshot([['a', 'b'], ['a', 'c']])
        stats = footprint.row_stats(snapshot['inbounds'])
        live = memory_report.snapshot_footprint(snapshot)
        self.assertEqual(stats['rows'], live['client_rows'])
        self.assertEqual(stats['unique_clients'], live['unique_clients'])
        self.assertEqual(stats['rows_with_raw_client'], live['rows_with_raw_client'])
        self.assertEqual(stats['rows_with_formatted_strings'],
                         live['rows_with_formatted_strings'])
        self.assertEqual(stats['rows'] - stats['unique_clients'], live['duplicate_rows'])

    def test_an_empty_snapshot_reports_zero_instead_of_failing(self):
        stats = footprint.row_stats([])
        self.assertEqual(stats['rows'], 0)
        self.assertEqual(stats['unique_clients'], 0)
        self.assertEqual(stats['formatted_keys'], {})

    def test_a_malformed_row_is_skipped_rather_than_crashing_the_measurement(self):
        stats = footprint.row_stats([{'clients': ['not-a-dict', None, _client('a')]},
                                     'not-an-inbound'])
        self.assertEqual(stats['rows'], 1)


class DeletionDeltaTests(unittest.TestCase):
    def test_the_counterfactual_is_measured_on_a_copy(self):
        snapshot = _snapshot([['a', 'b']])
        variant = footprint._variant(snapshot, drop_formatted=True)
        self.assertEqual(variant['rows'], 2)
        for inbound in snapshot['inbounds']:
            for client in inbound['clients']:
                self.assertIn('up_formatted', client)

    def test_dropping_raw_client_reduces_the_measured_size(self):
        snapshot = _snapshot([['a', 'b']])
        full = footprint._sizes(snapshot)
        without = footprint._variant(snapshot, drop_raw=True)
        self.assertLess(without['deep_bytes'], full['deep_bytes'])
        self.assertLess(without['gzip_bytes'], full['gzip_bytes'])

    def test_deep_size_visits_a_shared_object_once(self):
        shared = {'id': 'x', 'payload': 'y' * 64}
        holder = {'first': shared, 'second': shared}
        once = footprint.deep_size(shared)
        self.assertGreaterEqual(footprint.deep_size(holder), once)
        self.assertLess(footprint.deep_size(holder), 2 * once)


if __name__ == '__main__':
    unittest.main()
