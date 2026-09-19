"""Memory attribution: host totals, per-role PSS, snapshot duplication, bounded trend."""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from panel.core import memory_report  # noqa: E402

MEMINFO = """MemTotal:        3962752 kB
MemFree:          118340 kB
MemAvailable:     839680 kB
Buffers:           22344 kB
Cached:           604160 kB
SReclaimable:      48896 kB
SwapTotal:       2097152 kB
SwapFree:        1982464 kB
"""

STATUS = """Name:\tpython
VmRSS:\t  720896 kB
VmHWM:\t  901120 kB
VmSwap:\t    2048 kB
Threads:\t19
"""

SMAPS_ROLLUP = """Rss:             720896 kB
Pss:             656384 kB
Pss_Anon:        520192 kB
Pss_File:        120192 kB
Pss_Shmem:        16000 kB
Private_Clean:    40960 kB
Private_Dirty:   577536 kB
Anonymous:       610304 kB
Swap:              2048 kB
"""

PRESSURE = """some avg10=1.20 avg60=0.80 avg300=0.40 total=12345
full avg10=0.10 avg60=0.05 avg300=0.02 total=678
"""


def _fake_read(files):
    def reader(path):
        return files.get(path)
    return reader


class HostMemoryTests(unittest.TestCase):
    def test_used_excludes_reclaimable_cache(self):
        files = {'/proc/meminfo': MEMINFO}
        with mock.patch.object(memory_report, '_read', _fake_read(files)):
            host = memory_report.host_memory(now=1000.0)
        self.assertTrue(host['available'])
        self.assertEqual(host['total_bytes'], 3962752 * 1024)
        self.assertEqual(host['available_bytes'], 839680 * 1024)
        # used = total - MemAvailable: page cache is not application pressure.
        self.assertEqual(host['used_bytes'], (3962752 - 839680) * 1024)
        self.assertEqual(host['cache_bytes'], (22344 + 604160 + 48896) * 1024)
        self.assertEqual(host['swap_used_bytes'], (2097152 - 1982464) * 1024)
        self.assertEqual(host['available_pct'], 21.2)

    def test_health_follows_availability_and_pressure(self):
        cases = (
            ('MemTotal: 1000 kB\nMemAvailable: 500 kB\n', 'ok'),
            ('MemTotal: 1000 kB\nMemAvailable: 150 kB\n', 'warning'),
            ('MemTotal: 1000 kB\nMemAvailable: 50 kB\n', 'critical'),
        )
        for meminfo, expected in cases:
            with mock.patch.object(memory_report, '_read', _fake_read({'/proc/meminfo': meminfo})):
                self.assertEqual(memory_report.host_memory()['health'], expected, meminfo)
        # Memory pressure alone is enough to call it critical even with RAM free.
        files = {'/proc/meminfo': 'MemTotal: 1000 kB\nMemAvailable: 500 kB\n',
                 '/proc/pressure/memory': 'some avg10=30.00 avg60=1 avg300=1 total=1\n'}
        with mock.patch.object(memory_report, '_read', _fake_read(files)):
            self.assertEqual(memory_report.host_memory()['health'], 'critical')

    def test_a_host_without_proc_reports_unavailable_not_zero(self):
        with mock.patch.object(memory_report, '_read', _fake_read({})):
            host = memory_report.host_memory()
        self.assertFalse(host['available'])
        self.assertIn('meminfo', host['reason'])
        self.assertNotIn('used_bytes', host)   # no invented zeros

    def test_pressure_is_parsed_when_present(self):
        files = {'/proc/meminfo': MEMINFO, '/proc/pressure/memory': PRESSURE}
        with mock.patch.object(memory_report, '_read', _fake_read(files)):
            host = memory_report.host_memory()
        self.assertEqual(host['pressure']['some_avg10'], 1.2)
        self.assertEqual(host['pressure']['full_avg10'], 0.1)


class ProcessMemoryTests(unittest.TestCase):
    def test_pss_is_used_and_private_is_reported_separately(self):
        files = {'/proc/4242/status': STATUS, '/proc/4242/smaps_rollup': SMAPS_ROLLUP,
                 '/proc/4242/cmdline': 'python\x00background_worker.py\x00'}
        with mock.patch.object(memory_report, '_read', _fake_read(files)):
            row = memory_report.process_memory(4242)
        self.assertEqual(row['rss_bytes'], 720896 * 1024)
        self.assertEqual(row['pss_bytes'], 656384 * 1024)
        self.assertEqual(row['private_bytes'], (40960 + 577536) * 1024)
        self.assertEqual(row['peak_rss_bytes'], 901120 * 1024)
        self.assertEqual(row['threads'], 19)
        self.assertEqual(row['role'], 'background')
        self.assertNotIn('pss_is_rss_fallback', row)

    def test_without_smaps_pss_falls_back_to_rss_and_says_so(self):
        files = {'/proc/4243/status': STATUS}
        with mock.patch.object(memory_report, '_read', _fake_read(files)):
            row = memory_report.process_memory(4243)
        self.assertEqual(row['pss_bytes'], row['rss_bytes'])
        self.assertTrue(row['pss_is_rss_fallback'])
        self.assertIn('smaps_rollup', row['pss_note'])

    def test_role_detection_covers_every_launch_entry_point(self):
        for cmdline, expected in (
                ('/usr/bin/gunicorn\x00app:app\x00', 'web'),
                ('python\x00/app/background_worker.py\x00', 'background'),
                ('python\x00/app/telegram_bot_worker.py\x00', 'telegram-bot'),
                ('python\x00/app/telegram_egress_worker.py\x00', 'telegram-egress'),
                ('python\x00/app/pulse_runner.py\x00', 'pulse'),
                ('/usr/local/bin/xray\x00run\x00-c\x00/config.json\x00', 'xray'),
                ('/usr/sbin/nginx\x00-g\x00daemon off;\x00', 'other'),
        ):
            files = {'/proc/7/status': STATUS, '/proc/7/cmdline': cmdline}
            with mock.patch.object(memory_report, '_read', _fake_read(files)):
                self.assertEqual(memory_report.process_memory(7)['role'], expected, cmdline)

    def test_a_command_line_is_never_returned_verbatim(self):
        # argv can carry a token; only the executable basename may leave the module.
        files = {'/proc/9/status': STATUS,
                 '/proc/9/cmdline': '/usr/bin/python\x00--api-token\x00SECRET123\x00'}
        with mock.patch.object(memory_report, '_read', _fake_read(files)):
            row = memory_report.process_memory(9)
        self.assertEqual(row['command'], 'python')
        self.assertNotIn('SECRET123', repr(row))


class EveAggregationTests(unittest.TestCase):
    def test_total_pss_sums_pss_not_rss(self):
        rows = {
            '100': {'available': True, 'pid': 100, 'role': 'web',
                    'rss_bytes': 500, 'pss_bytes': 300, 'private_bytes': 200, 'threads': 3,
                    'uptime_seconds': 10, 'peak_rss_bytes': 600},
            '101': {'available': True, 'pid': 101, 'role': 'background',
                    'rss_bytes': 900, 'pss_bytes': 700, 'private_bytes': 500, 'threads': 9,
                    'uptime_seconds': 99, 'peak_rss_bytes': 1000},
            '102': {'available': True, 'pid': 102, 'role': 'xray',
                    'rss_bytes': 100, 'pss_bytes': 80, 'private_bytes': 60, 'threads': 4,
                    'uptime_seconds': 5, 'peak_rss_bytes': 120},
        }
        with mock.patch.object(memory_report, 'process_memory',
                               side_effect=lambda pid: rows[str(pid)]), \
                mock.patch.object(memory_report.os, 'listdir', return_value=['100', '101', '102', 'x']), \
                mock.patch.object(memory_report.os.path, 'isdir', return_value=True):
            report = memory_report.eve_processes()
        # Shared pages counted once: 300 + 700, and Xray reported separately.
        self.assertEqual(report['eve_pss_bytes'], 1000)
        self.assertEqual(report['eve_pss_with_xray_bytes'], 1080)
        self.assertEqual(report['roles']['background']['threads'], 9)
        self.assertEqual(report['roles']['xray']['processes'], 1)
        self.assertIn('double-count', report['note'])


class SnapshotFootprintTests(unittest.TestCase):
    def _snapshot(self):
        return {
            'last_update': 't',
            'servers_status': [{'server_id': 1}, {'server_id': 2}],
            'inbounds': [
                {'server_id': 1, 'id': 1, 'clients': [
                    {'id': 'uuid-a', 'email': 'a@x', 'up': 1, 'up_formatted': '1 B',
                     'raw_client': {'id': 'uuid-a', 'totalGB': 10}},
                    {'id': 'uuid-b', 'email': 'b@x', 'up': 2},
                ]},
                {'server_id': 2, 'id': 2, 'clients': [
                    # The same account on a second inbound: a v3 characteristic, counted
                    # as a duplicate row rather than as a second client.
                    {'id': 'uuid-a', 'email': 'a@x', 'up': 3, 'raw_client': {'id': 'uuid-a'}},
                ]},
            ],
        }

    def test_duplication_and_duplicated_payloads_are_counted(self):
        row = memory_report.snapshot_footprint(self._snapshot())
        self.assertEqual(row['servers'], 2)
        self.assertEqual(row['inbounds'], 2)
        self.assertEqual(row['client_rows'], 3)
        self.assertEqual(row['unique_clients'], 2)
        self.assertEqual(row['duplicate_rows'], 1)
        self.assertEqual(row['duplication_ratio'], 1.5)
        self.assertEqual(row['rows_with_raw_client'], 2)
        self.assertEqual(row['rows_with_formatted_strings'], 1)

    def test_a_client_without_an_id_is_keyed_by_server_and_email(self):
        snapshot = {'inbounds': [{'server_id': 5, 'id': 1, 'clients': [
            {'email': 'x@y'}, {'email': 'x@y'}]}]}
        row = memory_report.snapshot_footprint(snapshot)
        self.assertEqual(row['client_rows'], 2)
        self.assertEqual(row['unique_clients'], 1)

    def test_a_snapshot_that_is_not_there_reports_unavailable(self):
        with mock.patch.dict('sys.modules', {'app': None}):
            row = memory_report.snapshot_footprint()
        self.assertFalse(row['available'])
        self.assertIn('reason', row)


class TrendTests(unittest.TestCase):
    class _FakeRedis:
        def __init__(self):
            self.items = []

        def lpush(self, key, value):
            self.items.insert(0, value)
            return len(self.items)

        def ltrim(self, key, start, stop):
            del self.items[stop + 1:]
            return True

        def expire(self, key, ttl):
            return True

        def lrange(self, key, start, stop):
            return list(self.items)[:stop + 1]

    def setUp(self):
        memory_report._last_sample_at = 0.0

    def test_samples_are_throttled_bounded_and_trimmed(self):
        fake = self._FakeRedis()
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake), \
                mock.patch.object(memory_report, 'sample',
                                  return_value={'at': 1.0, 'eve_pss_bytes': 5}):
            self.assertTrue(memory_report.record_sample(now=100.0))
            # Inside the interval: no second sample, so the ring grows at a fixed rate.
            self.assertFalse(memory_report.record_sample(now=30.0))
            self.assertTrue(memory_report.record_sample(now=200.0))
        self.assertEqual(len(fake.items), 2)

    def test_trend_reports_direction_from_the_ring(self):
        # 275 MB added over an hour is a leak-shaped slope; the threshold is per hour, not
        # per sample, so the sample spacing must be realistic for the assertion to mean
        # anything.
        fake = self._FakeRedis()
        mb = 1024 * 1024
        for at, pss in ((0, 600 * mb), (1800, 700 * mb), (3600, 875 * mb)):
            fake.items.insert(0, '{"at": %d, "eve_pss_bytes": %d}' % (at, pss))
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake):
            row = memory_report.trend(now=3600.0, minutes=60)
        self.assertTrue(row['available'])
        self.assertEqual(row['samples'], 3)
        self.assertEqual(row['peak_bytes'], 875 * mb)
        self.assertEqual(row['delta_bytes'], 275 * mb)
        self.assertEqual(row['trend'], 'growing')

    def test_a_flat_ring_is_stable_and_a_short_window_cannot_fake_a_leak(self):
        mb = 1024 * 1024
        flat = self._FakeRedis()
        for at in (0, 1800, 3600):
            flat.items.insert(0, '{"at": %d, "eve_pss_bytes": %d}' % (at, 900 * mb))
        with mock.patch('panel.core.redis_client.get_redis', return_value=flat):
            row = memory_report.trend(now=3600.0)
        self.assertEqual(row['trend'], 'stable')
        self.assertEqual(row['delta_bytes'], 0)

        # A few hundred bytes across a few seconds is noise, not growth.
        noise = self._FakeRedis()
        for at, pss in ((100, 1000), (102, 1200), (104, 1400)):
            noise.items.insert(0, '{"at": %d, "eve_pss_bytes": %d}' % (at, pss))
        with mock.patch('panel.core.redis_client.get_redis', return_value=noise):
            self.assertEqual(memory_report.trend(now=104.0)['trend'], 'stable')

    def test_an_empty_ring_reports_no_samples(self):
        with mock.patch('panel.core.redis_client.get_redis', return_value=self._FakeRedis()):
            row = memory_report.trend(now=1.0)
        self.assertTrue(row['available'])
        self.assertEqual(row['samples'], 0)
        self.assertIn('no samples yet', row['note'])


class ReportContractTests(unittest.TestCase):
    def test_the_report_carries_no_credentials_or_customer_data(self):
        # The overview payload is rendered in a browser and logged by proxies: it must only
        # ever contain counts, sizes, pids and roles.
        forbidden = ('email', 'token', 'password', 'secret', 'api_key', 'host=', 'uuid@')
        payload = repr({
            'host': memory_report.host_memory(),
            'snapshot': memory_report.snapshot_footprint({'inbounds': [
                {'server_id': 1, 'id': 1, 'clients': [
                    {'id': 'uuid-a', 'email': 'customer@example.invalid',
                     'raw_client': {'password': 'x'}}]}]}),
            'caches': memory_report.cache_footprint(),
        }).lower()
        for word in forbidden:
            self.assertNotIn(word, payload)
        # ...while still reporting the counts that matter.
        self.assertIn('client_rows', payload)
        self.assertIn('duplication_ratio', payload)

    def test_health_notes_name_the_actual_condition(self):
        payload = {
            'host': {'available': True, 'total_bytes': 1000, 'available_bytes': 50,
                     'available_pct': 5.0, 'swap_used_bytes': 10},
            'eve': {'eve_pss_bytes': 900},
            'snapshot': {'duplication_ratio': 1.5},
        }
        health = memory_report._health(payload)
        self.assertEqual(health['state'], 'warning')
        joined = ' '.join(health['notes'])
        self.assertIn('10%', joined)
        self.assertIn('swap', joined)
        self.assertIn('duplicate', joined)

    def test_an_unknown_host_is_not_reported_as_healthy(self):
        health = memory_report._health({'host': {'available': False, 'reason': 'no /proc'}})
        self.assertEqual(health['state'], 'unknown')
        self.assertIn('no /proc', health['notes'][0])


class DeepAnalysisTests(unittest.TestCase):
    def test_the_deep_sample_returns_files_and_sizes_only(self):
        import tracemalloc
        tracemalloc.start()
        try:
            blob = [bytearray(1024) for _ in range(64)]  # noqa: F841 - the allocation is the point
            row = memory_report.analyze_python_memory(limit=5, timeout_seconds=5.0)
        finally:
            tracemalloc.stop()
        self.assertTrue(row['available'])
        self.assertLessEqual(len(row['entries']), 5)
        self.assertGreater(row['current_bytes'], 0)
        for entry in row['entries']:
            self.assertEqual(sorted(entry), ['blocks', 'file', 'line', 'size_bytes'])
            self.assertNotIn('/', entry['file'])   # basename only: no local paths


if __name__ == '__main__':
    unittest.main()
