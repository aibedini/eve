"""Scale benchmark: the mutation path must be O(1) in the number of panels (phase 12).

Every number is measured on the real code path at 10 / 50 / 100 panels (each with
``--clients-per-server`` cached clients):

* ``mutation_p95_ms`` / ``mutation_cpu_ms`` -- ``patch_cached_client`` (the write-through
  commit) and its CPU time: must not grow with the panel count.
* ``delta_bytes`` vs ``full_inbounds_bytes`` -- the delta the browser receives after a
  mutation (``snapshot_delta.build_sync`` + ``select_inbounds``) against the whole
  snapshot: the delta is one panel's block, the full snapshot is everything.
* ``cache_read_full_ms`` / ``cache_read_delta_ms`` (+ payload bytes) -- the real
  ``GET /api/refresh?mode=cache`` route, with and without ``since``.
* ``redis_ops_per_mutation`` -- commands issued to Redis during one mutation (counting
  fake client), by command name.
* ``outbound_http_calls_per_cache_read`` -- the cache read must make no panel call.
* ``xui_requests_per_minute_*`` -- polls per minute simulated with the real per-server
  policy (``panel/core/refresh_policy.py``) on a virtual clock: a watched panel every
  ``EVE_SERVER_POLL_ACTIVE_SECONDS``, an unwatched one every
  ``EVE_SERVER_POLL_IDLE_SECONDS``, capped by ``EVE_SERVER_POLL_WATCH_LIMIT``.
* ``python_peak_kb`` / ``process_peak_rss_kb`` -- memory to hold the snapshot (this one
  is expected to grow; it is reported, not bounded).

Exit code is non-zero when the O(1) relations below break.

Usage:
    python scripts/benchmark_mutation_scale.py --quick
    python scripts/benchmark_mutation_scale.py --json docs/performance/mutation-scale.json
"""
import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

SCALES = (10, 50, 100)
CLIENTS_PER_SERVER = 25
WATCH_LIMIT = 20

# The panel count must not show up in the mutation cost, and the delta must stay the
# size of one panel's block while the full snapshot grows with the install.
MUTATION_SCALE_TOLERANCE = 2.0
DELTA_SCALE_TOLERANCE = 2.0
FULL_GROWTH_MIN = 4.0
# A cache read may not touch a panel, ever (that is the whole point of phase 7).
MAX_OUTBOUND_HTTP_PER_CACHE_READ = 0


def p95(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1))
    return ordered[index]


class CountingRedis:
    """Records every Redis command the mutation path issues."""

    def __init__(self):
        self.calls = []

    def _record(self, name):
        self.calls.append(name)

    def _value_for(self, name):
        if name == 'get':
            return None
        if name in ('set', 'exists', 'expire', 'delete', 'publish', 'incr'):
            return 1 if name in ('exists', 'expire', 'delete', 'publish', 'incr') else True
        if name == 'eval':
            return 1
        return None

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        def command(*args, **kwargs):
            self._record(name)
            return self._value_for(name)

        return command

    def pipeline(self, *args, **kwargs):
        self._record('pipeline')
        outer = self

        class _Pipe:
            def __getattr__(self, name):
                if name.startswith('_'):
                    raise AttributeError(name)

                def command(*args, **kwargs):
                    outer._record(name)
                    return None

                return command

            def execute(self):
                outer._record('execute')
                return [1]

        return _Pipe()

    def counts(self):
        rows = {}
        for name in self.calls:
            rows[name] = rows.get(name, 0) + 1
        return rows

    def total(self):
        return len(self.calls)


def _build_world(refresh_jobs, servers, clients_per_server):
    """A warm snapshot of ``servers`` panels, each with one inbound and N clients."""
    from app import GLOBAL_SERVER_DATA
    inbounds = []
    for index in range(servers):
        server_id = 8000 + index
        inbounds.append({
            'server_id': server_id,
            'id': 1,
            'remark': 'scale-%d' % server_id,
            'clients': [],
            'client_count': 0,
            'active_count': 0,
        })
    GLOBAL_SERVER_DATA.clear()
    GLOBAL_SERVER_DATA.update({
        'last_update': 'bench',
        'is_updating': False,
        'stats': {},
        'servers_status': [
            {'server_id': 8000 + index, 'success': True, 'reachable': True, 'stats': {}}
            for index in range(servers)
        ],
        'inbounds': inbounds,
    })
    for index in range(servers):
        server_id = 8000 + index
        for client_index in range(clients_per_server):
            refresh_jobs.add_cached_client(
                server_id, [1],
                {'email': 'scale-%d-%d@bench' % (server_id, client_index),
                 'id': 'uuid-%d-%d' % (server_id, client_index),
                 'enable': True, 'totalGB': 0, 'expiryTime': 0},
                publish=False)
    return GLOBAL_SERVER_DATA


def _measure_mutation(refresh_jobs, servers, clients_per_server, iterations):
    """p95 and CPU time of the write-through commit for one client of one panel."""
    from app import GLOBAL_SERVER_DATA
    target_server = 8000 + servers - 1        # the last panel: no early-exit luck
    target_email = 'scale-%d-0@bench' % target_server
    latencies, cpu = [], []
    for index in range(iterations):
        cpu_started = time.process_time()
        started = time.perf_counter()
        result = refresh_jobs.patch_cached_client(
            target_server, target_email,
            up=1024 * (index + 1), down=2048 * (index + 1),
            operation='renew')
        elapsed = (time.perf_counter() - started) * 1000.0
        cpu.append((time.process_time() - cpu_started) * 1000.0)
        if not result.changed:
            raise RuntimeError('the mutation did not patch the cache at %d panels' % servers)
        latencies.append(elapsed)
    _ = GLOBAL_SERVER_DATA
    return latencies, cpu


def _measure_delta_and_full(refresh_jobs, servers, clients_per_server):
    """Delta payload after one mutation vs the whole snapshot."""
    from app import GLOBAL_SERVER_DATA
    from panel.core import snapshot_delta

    revision_before = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)
    # A mutation the browser has not seen yet: the delta must carry one block.
    refresh_jobs.patch_cached_client(
        8000 + servers - 1, 'scale-%d-0@bench' % (8000 + servers - 1),
        up=999, operation='renew')
    sync_info = snapshot_delta.build_sync(GLOBAL_SERVER_DATA, revision_before)
    delta_inbounds = snapshot_delta.select_inbounds(GLOBAL_SERVER_DATA, sync_info.get('changed') or [])
    delta_bytes = len(json.dumps(
        {'sync': {key: value for key, value in sync_info.items() if key != 'changed'},
         'inbounds': delta_inbounds}, default=str, ensure_ascii=False))
    full_bytes = len(json.dumps(GLOBAL_SERVER_DATA['inbounds'], default=str,
                                ensure_ascii=False))
    return sync_info.get('mode'), delta_bytes, full_bytes


def _cache_reader():
    """A logged-in test client for the real /api/refresh route."""
    from app import Admin, app, db
    admin = Admin.query.filter_by(username='scale-bench').first()
    if admin is None:
        admin = Admin(username='scale-bench', role='superadmin', is_superadmin=True,
                      enabled=True)
        admin.set_password('CorrectHorseBattery1!')
        db.session.add(admin)
        db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess.clear()
        sess['admin_id'] = admin.id
        sess['role'] = admin.role
        sess['is_superadmin'] = bool(admin.is_superadmin)
    return client


def _measure_cache_read(client, since, iterations):
    """(p95 ms, bytes) for /api/refresh?mode=cache, optionally with ?since=."""
    url = '/api/refresh?mode=cache'
    if since is not None:
        url += '&since=%d' % since
    for _ in range(2):
        client.get(url)
    samples, sizes = [], []
    for _ in range(iterations):
        started = time.perf_counter()
        response = client.get(url)
        samples.append((time.perf_counter() - started) * 1000.0)
        if response.status_code not in (200, 202):
            raise RuntimeError('cache read failed with %s' % response.status_code)
        sizes.append(len(response.data))
    return p95(samples), max(sizes)


def _measure_redis_ops(refresh_jobs, servers):
    """Commands a single mutation issues, with the Redis path active."""
    from panel.core import client_events, redis_client, refresh_policy, snapshot_delta
    fake = CountingRedis()
    patches = [
        mock.patch.object(redis_client, 'get_redis', return_value=fake),
        mock.patch.object(refresh_jobs, 'get_redis', return_value=fake),
        mock.patch.object(client_events, 'get_redis', return_value=fake),
        mock.patch.object(snapshot_delta, '_redis', return_value=fake),
        mock.patch.object(refresh_policy, '_redis', return_value=fake),
    ]
    for patch in patches:
        patch.start()
    try:
        # Warm-up mutation: the first call also fills the per-process caches.
        refresh_jobs.patch_cached_client(
            8000 + servers - 1, 'scale-%d-0@bench' % (8000 + servers - 1), up=1)
        fake.calls.clear()
        refresh_jobs.patch_cached_client(
            8000 + servers - 1, 'scale-%d-0@bench' % (8000 + servers - 1), up=2)
        return fake.counts()
    finally:
        for patch in reversed(patches):
            patch.stop()


def _simulate_polls(servers, watched, seconds=60.0, step=0.25):
    """Polls one simulated minute would issue, using the real per-server policy."""
    from panel.core import refresh_policy
    refresh_policy.reset_server_state()
    t0 = 1_000_000.0
    watched = min(servers, watched)
    for index in range(servers):
        server_id = 8000 + index
        if index < watched:
            # The browser renews the declaration on every poll; model it as a live mark.
            refresh_policy.note_server_activity(server_id, now=t0, ttl=seconds)
        refresh_policy.note_server_result(server_id, True, now=t0)
    polls = 0
    moment = t0
    while moment < t0 + seconds:
        moment += step
        for index in range(servers):
            server_id = 8000 + index
            if index < watched:
                refresh_policy.note_server_activity(server_id, now=moment, ttl=seconds)
            if refresh_policy.server_due(server_id, now=moment):
                polls += 1
                refresh_policy.note_server_result(server_id, True, now=moment)
    refresh_policy.reset_server_state()
    return polls


def _outbound_http_calls(action):
    """Run ``action`` and count outbound HTTP requests it made."""
    import requests
    sent = []
    original = requests.sessions.Session.request

    def counting(self, method, url, *args, **kwargs):
        sent.append((method, url))
        raise RuntimeError('stopped an outbound request: %s %s' % (method, url))

    requests.sessions.Session.request = counting
    try:
        action()
    except RuntimeError:
        # The request was counted; the caller sees the count and reports it.
        pass
    finally:
        requests.sessions.Session.request = original
    return len(sent)


def _peak_rss_kb():
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        value = float(getattr(usage, 'ru_maxrss', 0) or 0)
        # Linux/macOS report KB/bytes respectively; normalise the common case.
        if sys.platform == 'darwin':
            return round(value / 1024.0, 1)
        if sys.platform.startswith('win'):
            return round(value / 1024.0, 1)
        return round(value, 1)
    except Exception:
        return None


def run(*, quick=False, clients_per_server=CLIENTS_PER_SERVER):
    os.environ['FLASK_ENV'] = 'development'
    os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
    os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
    db_path = os.path.join(tempfile.gettempdir(), 'eve-bench-scale-%d.db' % os.getpid())
    if os.path.exists(db_path):
        os.remove(db_path)
    os.environ['DATABASE_URL'] = 'sqlite:///' + db_path.replace(os.sep, '/')

    from app import app, db
    from panel.jobs import refresh as refresh_jobs
    from panel.core import refresh_policy

    mutation_iterations = 30 if quick else 80
    cache_iterations = 3 if quick else 8
    scales = (10, 50) if quick else SCALES

    rows = []
    with app.app_context():
        db.create_all()
        client = None
        for servers in scales:
            tracemalloc.start()
            _build_world(refresh_jobs, servers, clients_per_server)
            _, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peak = traced_peak / 1024.0

            latencies, cpu = _measure_mutation(
                refresh_jobs, servers, clients_per_server, mutation_iterations)
            delta_mode, delta_bytes, full_bytes = _measure_delta_and_full(
                refresh_jobs, servers, clients_per_server)

            if client is None:
                client = _cache_reader()
            from panel.core import snapshot_delta
            from app import GLOBAL_SERVER_DATA
            revision = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)
            full_ms, full_payload = _measure_cache_read(client, None, cache_iterations)

            # A fresh mutation, then a delta read from the revision before it.
            refresh_jobs.patch_cached_client(
                8000 + servers - 1, 'scale-%d-0@bench' % (8000 + servers - 1), up=7)
            delta_ms, delta_payload = _measure_cache_read(
                client, revision, cache_iterations)

            redis_rows = _measure_redis_ops(refresh_jobs, servers)
            http_calls = _outbound_http_calls(
                lambda: client.get('/api/refresh?mode=cache'))

            watched = min(WATCH_LIMIT, servers)
            watched_polls = _simulate_polls(servers, watched)
            unwatched_polls = _simulate_polls(servers, 0)

            rows.append({
                'servers': servers,
                'clients': servers * clients_per_server,
                'mutation_p95_ms': round(p95(latencies), 3),
                'mutation_mean_ms': round(statistics.fmean(latencies), 3),
                'mutation_cpu_p95_ms': round(p95(cpu), 3),
                'delta_mode': delta_mode,
                'delta_bytes': delta_bytes,
                'full_inbounds_bytes': full_bytes,
                'cache_read_full_p95_ms': round(full_ms, 3),
                'cache_read_full_bytes': full_payload,
                'cache_read_delta_p95_ms': round(delta_ms, 3),
                'cache_read_delta_bytes': delta_payload,
                'redis_ops_per_mutation': redis_rows,
                'redis_ops_total_per_mutation': sum(redis_rows.values()),
                'outbound_http_calls_per_cache_read': http_calls,
                'xui_requests_per_minute_watched': watched_polls,
                'xui_requests_per_minute_unwatched': unwatched_polls,
                'naive_all_servers_every_2s_per_minute': servers * 30,
                'python_peak_kb': round(peak, 1),
                'process_peak_rss_kb': _peak_rss_kb(),
            })
        refresh_policy.reset_server_state()

    verdicts = _verdicts(rows)
    return {
        'mode': 'quick' if quick else 'full',
        'clients_per_server': clients_per_server,
        'scales': list(scales),
        'watch_limit': WATCH_LIMIT,
        'rows': rows,
        'verdicts': verdicts,
        'passed': all(row['passed'] for row in verdicts.values()),
    }


def _verdicts(rows):
    """The O(1) relations, evaluated on the measured numbers."""
    first, last = rows[0], rows[-1]
    ratio = last['servers'] / float(first['servers'])

    mutation_ratio = (last['mutation_p95_ms'] / first['mutation_p95_ms']
                      if first['mutation_p95_ms'] else 999.0)
    delta_ratio = (last['delta_bytes'] / first['delta_bytes']
                   if first['delta_bytes'] else 999.0)
    full_ratio = (last['full_inbounds_bytes'] / first['full_inbounds_bytes']
                  if first['full_inbounds_bytes'] else 0.0)
    redis_totals = {row['redis_ops_total_per_mutation'] for row in rows}
    worst_http = max(row['outbound_http_calls_per_cache_read'] for row in rows)
    worst_watched = max(row['xui_requests_per_minute_watched'] for row in rows)
    naive = max(row['naive_all_servers_every_2s_per_minute'] for row in rows)

    def watch_bound(row):
        """Watched panels at the active cadence + the rest at the idle cadence."""
        watched = min(row['servers'], WATCH_LIMIT)
        return watched * 30 + (row['servers'] - watched) * 2

    oversized = [row for row in rows if row['servers'] > WATCH_LIMIT]
    polling_bounded = all(row['xui_requests_per_minute_watched'] <= watch_bound(row)
                          for row in rows)
    if oversized:
        # An install bigger than the watch limit must be strictly below "everything
        # every two seconds": that is what the limit is for.
        polling_bounded = polling_bounded and all(
            row['xui_requests_per_minute_watched']
            < row['naive_all_servers_every_2s_per_minute'] for row in oversized)

    return {
        'mutation_is_flat': {
            'p95_ratio_last_over_first': round(mutation_ratio, 3),
            'panels_ratio': round(ratio, 3),
            'tolerance': MUTATION_SCALE_TOLERANCE,
            'passed': mutation_ratio <= MUTATION_SCALE_TOLERANCE,
        },
        'delta_is_one_block': {
            'bytes_ratio_last_over_first': round(delta_ratio, 3),
            'tolerance': DELTA_SCALE_TOLERANCE,
            'passed': delta_ratio <= DELTA_SCALE_TOLERANCE,
        },
        'full_snapshot_grows_with_the_install': {
            'bytes_ratio_last_over_first': round(full_ratio, 3),
            'minimum': FULL_GROWTH_MIN,
            'passed': full_ratio >= FULL_GROWTH_MIN,
        },
        'redis_ops_are_constant': {
            'totals_per_scale': sorted(redis_totals),
            'passed': len(redis_totals) == 1,
        },
        'cache_read_makes_no_panel_call': {
            'worst_outbound_http_calls': worst_http,
            'maximum': MAX_OUTBOUND_HTTP_PER_CACHE_READ,
            'passed': worst_http <= MAX_OUTBOUND_HTTP_PER_CACHE_READ,
        },
        'per_server_polling_is_bounded': {
            'worst_watched_polls_per_minute': worst_watched,
            'naive_all_servers_every_2s': naive,
            'oversized_scales_below_naive': bool(oversized) and all(
                row['xui_requests_per_minute_watched']
                < row['naive_all_servers_every_2s_per_minute'] for row in oversized),
            'passed': polling_bounded,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Mutation scale benchmark (O(1) proof)')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--json', default=None)
    parser.add_argument('--clients-per-server', dest='clients_per_server',
                        type=int, default=CLIENTS_PER_SERVER)
    args = parser.parse_args(argv)
    result = run(quick=args.quick, clients_per_server=args.clients_per_server)
    print('mode=%s scales=%s clients/server=%d' % (
        result['mode'], result['scales'], result['clients_per_server']))
    header = ('panels', 'mutation p95', 'cpu p95', 'delta B', 'full B',
              'read full', 'read delta', 'redis ops', 'xui/min(watched)')
    print('%-7s %12s %9s %9s %10s %10s %11s %10s %18s' % header)
    for row in result['rows']:
        print('%-7d %12.3f %9.3f %9d %10d %10.3f %11.3f %10d %18d' % (
            row['servers'], row['mutation_p95_ms'], row['mutation_cpu_p95_ms'],
            row['delta_bytes'], row['full_inbounds_bytes'],
            row['cache_read_full_p95_ms'], row['cache_read_delta_p95_ms'],
            row['redis_ops_total_per_mutation'], row['xui_requests_per_minute_watched']))
    for name, verdict in result['verdicts'].items():
        print('%-42s %s' % (name, 'OK' if verdict['passed'] else 'BROKEN'))
    print('O(1) relations hold' if result['passed'] else 'SCALING REGRESSION')
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
