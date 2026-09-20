"""Memory attribution: host totals, per-role PSS, snapshot duplication, bounded trend."""
import json
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
                # Host services are classified so they can be attributed to the host
                # rather than to Eve; they were 'other' before, which made the host's
                # 3.45/3.78 GB question unanswerable.
                ('/usr/sbin/nginx\x00-g\x00daemon off;\x00', 'nginx'),
                ('/usr/bin/redis-server\x00*:6379\x00', 'redis'),
                ('/usr/lib/postgresql/16/bin/postgres\x00-D\x00/var/lib/pg\x00', 'postgres'),
                ('/usr/sbin/sshd\x00-D\x00', 'other'),
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

    def _grouped(self, rows):
        with mock.patch.object(memory_report, 'process_memory',
                               side_effect=lambda pid: rows[str(pid)]), \
                mock.patch.object(memory_report.os, 'listdir',
                                  return_value=[key for key in rows] + ['x']), \
                mock.patch.object(memory_report.os.path, 'isdir', return_value=True):
            return memory_report.eve_processes()

    def test_host_services_are_attributed_separately_and_never_to_eve(self):
        report = self._grouped({
            '100': {'available': True, 'pid': 100, 'role': 'web',
                    'rss_bytes': 500, 'pss_bytes': 300, 'private_bytes': 200, 'threads': 3,
                    'uptime_seconds': 10, 'peak_rss_bytes': 600},
            '101': {'available': True, 'pid': 101, 'role': 'redis',
                    'rss_bytes': 100, 'pss_bytes': 74, 'private_bytes': 70, 'threads': 6,
                    'uptime_seconds': 99, 'peak_rss_bytes': 120},
            '102': {'available': True, 'pid': 102, 'role': 'postgres',
                    'rss_bytes': 200, 'pss_bytes': 182, 'private_bytes': 180, 'threads': 8,
                    'uptime_seconds': 500, 'peak_rss_bytes': 240},
            '103': {'available': True, 'pid': 103, 'role': 'other',
                    'rss_bytes': 90, 'pss_bytes': 60, 'private_bytes': 55, 'threads': 2,
                    'uptime_seconds': 5, 'peak_rss_bytes': 95},
        })
        # Eve is Eve: Redis and PostgreSQL are on the host, not in the Eve figure.
        self.assertEqual(report['eve_pss_bytes'], 300)
        self.assertEqual(report['service_pss_bytes'], 256)
        self.assertEqual(report['services']['redis']['pss_bytes'], 74)
        self.assertEqual(report['services']['postgres']['threads'], 8)
        self.assertNotIn('redis', report['roles'])
        # Unclassified processes stay out of the tables but their PSS is still summed,
        # so the host can be reconciled without them being listed.
        self.assertEqual(report['other_processes'], 1)
        self.assertEqual(report['other_pss_bytes'], 60)
        self.assertNotIn('other', report['roles'])

    def test_unclassified_processes_are_ranked_and_limited(self):
        rows = {
            '100': {'available': True, 'pid': 100, 'role': 'web', 'command': 'gunicorn',
                    'pss_bytes': 900, 'rss_bytes': 900, 'threads': 1},
            '101': {'available': True, 'pid': 101, 'role': 'other', 'command': 'java',
                    'pss_bytes': 300, 'rss_bytes': 400, 'threads': 20},
            '102': {'available': True, 'pid': 102, 'role': 'other', 'command': 'dockerd',
                    'pss_bytes': 500, 'rss_bytes': 600, 'threads': 30},
        }
        with mock.patch.object(memory_report, 'process_memory',
                               side_effect=lambda pid: rows[str(pid)]), \
                mock.patch.object(memory_report.os, 'listdir',
                                  return_value=['100', '101', '102', 'x']), \
                mock.patch.object(memory_report.os.path, 'isdir', return_value=True):
            out = memory_report.unclassified_processes(limit=1)
            full = memory_report.unclassified_processes(limit=10)
        self.assertEqual([row['command'] for row in full['processes']],
                         ['dockerd', 'java'])
        self.assertEqual(out['count'], 2)
        self.assertTrue(out['truncated'])
        self.assertEqual(out['processes'][0]['pid'], 102)
        self.assertFalse(full['truncated'])

    def test_the_host_reconciles_down_to_a_residual_instead_of_hiding_it(self):
        host = {'total_bytes': 1000, 'free_bytes': 100, 'cache_bytes': 200,
                'used_bytes': 700, 'available_bytes': 300}
        eve = {'eve_pss_bytes': 300, 'eve_pss_with_xray_bytes': 320,
               'service_pss_bytes': 100, 'other_pss_bytes': 50,
               'roles': {'xray': {'pss_bytes': 20}}}
        row = memory_report.accounting(host, eve)
        # Xray is already inside eve_pss_with_xray, so it is not summed a second time.
        self.assertEqual(row['process_pss_bytes'], 470)
        self.assertEqual(row['residual_bytes'], 230)
        self.assertEqual(row['used_bytes'], 700)

    def test_accounting_without_a_host_total_is_unavailable_not_zero(self):
        row = memory_report.accounting({'available': False}, {})
        self.assertFalse(row['available'])
        self.assertIn('reason', row)

    def test_accounting_says_so_when_free_and_cache_are_unknown(self):
        row = memory_report.accounting({'total_bytes': 1000}, {})
        self.assertTrue(row['available'])
        self.assertIsNone(row['residual_bytes'])

    def test_an_unavailable_proc_still_returns_the_full_key_set(self):
        # Two consumers read this payload (the Overview and the on-host collector), so a
        # shape that changes with the platform would break one of them silently.
        with mock.patch.object(memory_report.os.path, 'isdir', return_value=False):
            report = memory_report.eve_processes()
        self.assertFalse(report['available'])
        for key in ('roles', 'services', 'other_processes', 'other_rss_bytes',
                    'other_pss_bytes', 'other_private_bytes', 'other_threads',
                    'eve_pss_bytes', 'eve_pss_with_xray_bytes', 'service_pss_bytes'):
            self.assertIn(key, report, key)
        # "Not measured" is None, never 0: a zero would read as "there are none".
        self.assertIsNone(report['eve_pss_bytes'])
        self.assertIsNone(report['other_processes'])


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


class SnapshotCopyTests(unittest.TestCase):
    """The per-process copy records: what makes "how many copies exist" answerable."""

    class _FakeRedis:
        def __init__(self):
            self.store = {}
            self.sets = []

        def set(self, key, value, ex=None):
            self.store[key] = value
            self.sets.append((key, ex))
            return True

        def get(self, key):
            return self.store.get(key)

    def setUp(self):
        memory_report._last_copy_versions.clear()

    @staticmethod
    def _inbounds(rows_per_inbound):
        return [{'server_id': index + 1, 'id': index,
                 'clients': [{'id': 'uuid-%d-%d' % (index, n), 'email': 'c%d@x' % n}
                             for n in range(count)]}
                for index, count in enumerate(rows_per_inbound)]

    def _record(self, fake, inbounds, version, role='web', now=1000.0):
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake), \
                mock.patch.dict('os.environ', {'EVE_PROCESS_ROLE': role}):
            return memory_report.record_snapshot_copy(
                inbounds=inbounds, servers=[{'server_id': 1}], version=version, now=now)

    def test_a_record_carries_the_rows_it_holds_and_gets_a_ttl(self):
        fake = self._FakeRedis()
        self.assertTrue(self._record(fake, self._inbounds([3, 2]), 'v1'))
        key, ttl = fake.sets[0]
        self.assertEqual(key, memory_report.COPY_KEY_PREFIX + 'web')
        self.assertEqual(ttl, memory_report.COPY_TTL_SECONDS)
        record = json.loads(fake.store[key])
        self.assertEqual(record['client_rows'], 5)
        self.assertEqual(record['unique_clients'], 5)
        self.assertEqual(record['inbounds'], 2)
        self.assertEqual(record['servers'], 1)
        self.assertEqual(record['version'], 'v1')

    def test_the_same_version_is_not_written_twice(self):
        # This is what keeps a forced reload off the Redis op counts and off the hot path.
        fake = self._FakeRedis()
        self.assertTrue(self._record(fake, self._inbounds([1]), 'v1'))
        self.assertFalse(self._record(fake, self._inbounds([1]), 'v1'))
        self.assertEqual(len(fake.sets), 1)
        self.assertTrue(self._record(fake, self._inbounds([1]), 'v2'))
        self.assertEqual(len(fake.sets), 2)

    def test_a_role_that_stopped_expires_instead_of_counting_as_a_copy(self):
        fake = self._FakeRedis()
        self._record(fake, self._inbounds([4]), 'v1', role='web', now=1000.0)
        self._record(fake, self._inbounds([4]), 'v1', role='background', now=1000.0)
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake):
            live = memory_report.snapshot_copies(now=1000.0 + memory_report.COPY_TTL_SECONDS - 1)
            stale = memory_report.snapshot_copies(now=1000.0 + memory_report.COPY_TTL_SECONDS + 1)
        self.assertEqual(live['copies'], 2)
        self.assertEqual(live['largest_client_rows'], 4)
        self.assertEqual(live['client_rows_summed'], 8)   # rows per copy, not distinct rows
        self.assertEqual(live['versions'], ['v1'])
        self.assertEqual(live['roles']['web']['age_seconds'], 599.0)

    def test_expired_records_are_reported_as_expired_not_counted(self):
        fake = self._FakeRedis()
        self._record(fake, self._inbounds([4]), 'v1', role='web', now=1000.0)
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake):
            row = memory_report.snapshot_copies(now=1000.0 + memory_report.COPY_TTL_SECONDS + 1)
        self.assertEqual(row['copies'], 0)
        self.assertEqual(row['expired_roles'], ['web'])
        self.assertEqual(row['client_rows_summed'], 0)

    def test_no_redis_and_a_corrupt_record_are_handled(self):
        with mock.patch('panel.core.redis_client.get_redis', return_value=None):
            self.assertFalse(memory_report.snapshot_copies()['available'])
        fake = self._FakeRedis()
        fake.store[memory_report.COPY_KEY_PREFIX + 'web'] = 'not json'
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake):
            row = memory_report.snapshot_copies(now=1000.0)
        self.assertTrue(row['available'])
        self.assertEqual(row['copies'], 0)

    def test_a_redis_without_set_cannot_break_the_recorder(self):
        # A test double or a proxy may not implement set(); the recorder must absorb it.
        class _NoSet:
            def get(self, key):
                return None

        with mock.patch('panel.core.redis_client.get_redis', return_value=_NoSet()), \
                mock.patch.dict('os.environ', {'EVE_PROCESS_ROLE': 'web'}):
            self.assertFalse(memory_report.record_snapshot_copy(
                inbounds=self._inbounds([1]), version='v1'))


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

    def test_trend_carries_a_bounded_series_so_a_chart_needs_no_invention(self):
        mb = 1024 * 1024
        fake = self._FakeRedis()
        for at in range(0, 300, 60):
            fake.items.insert(0, '{"at": %d, "eve_pss_bytes": %d}' % (at, (500 + at) * mb))
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake):
            row = memory_report.trend(now=240.0, minutes=60)
        self.assertEqual(row['max_samples'], memory_report.SAMPLE_MAX)
        self.assertTrue(row['series'])
        self.assertEqual(sorted(row['series'][0]), ['at', 'bytes'])
        # Only the window, and only the newest points: the payload stays small while the
        # ring itself may hold a day.
        self.assertTrue(all(240 - point['at'] <= 60 * 60 for point in row['series']))
        self.assertEqual(row['series'][-1]['bytes'], (500 + 240) * mb)

    def test_a_series_is_capped_but_the_statistics_use_every_sample(self):
        mb = 1024 * 1024
        fake = self._FakeRedis()
        for at in range(0, 120):
            fake.items.insert(0, '{"at": %d, "eve_pss_bytes": %d}' % (at, (100 + at) * mb))
        with mock.patch('panel.core.redis_client.get_redis', return_value=fake):
            row = memory_report.trend(now=119.0, minutes=60, series_points=10)
        self.assertEqual(len(row['series']), 10)
        self.assertEqual(row['samples'], 120)      # every sample still counted
        self.assertEqual(row['peak_bytes'], 219 * mb)

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
