"""Official latency SLOs for the mutation -> cache -> UI path (phase 11).

Every number below is measured on the real code path, not modelled:

1. ``mutation_cache_commit_ms`` -- ``panel.jobs.refresh.patch_cached_client`` on a warm
   snapshot (the write-through commit an Eve mutation performs after the panel answered).
   Budget: p95 < 100 ms.
2. ``browser_visible_ms`` -- the same mutation plus the canonical client state and the
   JSON body the browser patches its card from, i.e. everything the operator waits for
   over the network. The DOM patch itself is a synchronous single-card update (no
   refetch) that ``tests/test_ui_design_system.py`` guards, so it is not part of the
   network budget. Budget: p95 < 300 ms.
3. ``cache_read_ms`` -- ``GET /api/refresh?mode=cache`` through the real Flask app
   against a warm snapshot: the path that must never touch X-UI. Budget: p95 < 50 ms.
4. ``other_tabs_ms`` -- a ``client.changed`` event recorded by one process until a real
   SSE stream (a reader thread on ``/api/refresh/stream``) observes it. Budget: p95 < 1 s.
5. ``external_xui_ms`` -- an external change in X-UI for a panel the operator is watching
   (or one an Eve mutation just touched): the panel is marked hot and the real periodic
   cycle is polled in small steps until it is fetched again. Budget: p95 < 3 s.

Exit code is non-zero when any p95 misses its budget, so CI can gate on it.

Usage:
    python scripts/benchmark_latency_slo.py --quick
    python scripts/benchmark_latency_slo.py --json docs/performance/latency-slo.json
"""
import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import threading
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

SLOS = {
    'mutation_cache_commit_ms': 100.0,
    'browser_visible_ms': 300.0,
    'cache_read_ms': 50.0,
    'other_tabs_ms': 1000.0,
    'external_xui_ms': 3000.0,
}

BENCH_SERVER_ID = 7401
BENCH_INBOUND_ID = 9101
BENCH_CLIENT = 'bench-0@latency.test'


def p95(values):
    """p95 by nearest-rank: the standard the SLO table quotes."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1))
    return ordered[index]


def _seed_snapshot(app_module, refresh_jobs, clients=200):
    """A warm snapshot with one panel and ``clients`` cached clients."""
    from app import GLOBAL_SERVER_DATA
    inbound = {
        'server_id': BENCH_SERVER_ID,
        'id': BENCH_INBOUND_ID,
        'remark': 'latency bench',
        'clients': [],
        'client_count': 0,
        'active_count': 0,
    }
    GLOBAL_SERVER_DATA.clear()
    GLOBAL_SERVER_DATA.update({
        'last_update': 'bench',
        'is_updating': False,
        'stats': {},
        'servers_status': [{'server_id': BENCH_SERVER_ID, 'success': True,
                            'reachable': True, 'stats': {}}],
        'inbounds': [inbound],
    })
    for index in range(clients):
        refresh_jobs.add_cached_client(
            BENCH_SERVER_ID, [BENCH_INBOUND_ID],
            {'email': 'bench-%d@latency.test' % index, 'id': 'uuid-bench-%d' % index,
             'enable': True, 'totalGB': 0, 'expiryTime': 0},
            publish=False)
    return GLOBAL_SERVER_DATA


def _measure_mutation(refresh_jobs, iterations):
    """(commit_ms, visible_ms): the write-through commit and the response body."""
    from app import GLOBAL_SERVER_DATA
    commits, visible = [], []
    for index in range(iterations):
        total = (10 + index) * 1024 ** 3
        started = time.perf_counter()
        result = refresh_jobs.patch_cached_client(
            BENCH_SERVER_ID, BENCH_CLIENT,
            total_gb_bytes=total,
            operation='renew',
            verified_state=None,
        )
        committed = time.perf_counter()
        payload = json.dumps({'mutation': result.to_payload()}, default=str)
        finished = time.perf_counter()
        if not result.changed or not payload:
            raise RuntimeError('the warm-snapshot mutation did not patch the cache')
        commits.append((committed - started) * 1000.0)
        visible.append((finished - started) * 1000.0)
        # Keep the mutation honest across iterations: the row must exist each time.
        if not GLOBAL_SERVER_DATA.get('inbounds'):
            raise RuntimeError('snapshot lost its inbounds during the benchmark')
    return commits, visible


def _measure_cache_read(iterations):
    """GET /api/refresh?mode=cache against the warm snapshot, through Flask."""
    from app import Admin, app, db
    Admin.query.delete()
    db.session.commit()
    admin = Admin(username='latency-bench', role='superadmin', is_superadmin=True,
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

    # Warm the route (template/JSON caches, pool connections) before measuring.
    for _ in range(3):
        client.get('/api/refresh?mode=cache')

    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        response = client.get('/api/refresh?mode=cache')
        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status_code not in (200, 202):
            raise RuntimeError('cache read failed with %s' % response.status_code)
        samples.append(elapsed)
    return samples


def _measure_other_tabs(samples, tick_seconds=0.05, timeout=10.0):
    """Record a client event, keep the reader running, and time its delivery."""
    from app import Admin, GLOBAL_SERVER_DATA, app
    from panel.core import client_events, snapshot_delta
    from panel.core import refresh_policy

    env = {
        'EVE_SSE_ENABLED': '1',
        'EVE_SSE_MAX_SECONDS': str(int(timeout + 5)),
        'EVE_SSE_TICK_SECONDS': str(tick_seconds),
        'EVE_SSE_HEARTBEAT_SECONDS': '5',
        'EVE_SSE_MAX_STREAMS': '4',
    }
    saved = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        client_events.reset()
        revision = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)
        admin = Admin.query.filter_by(username='latency-bench').first()
        stream_client = app.test_client()
        with stream_client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = admin.id
            sess['role'] = admin.role
            sess['is_superadmin'] = bool(admin.is_superadmin)

        observed = []
        ready = threading.Event()
        error = []

        def reader():
            try:
                response = stream_client.get(
                    '/api/refresh/stream?since=%d' % revision, buffered=False)
                buffer = b''
                hello = False
                for chunk in response.response:
                    buffer += chunk
                    if not hello and b'event: hello' in buffer:
                        hello = True
                        buffer = b''
                        ready.set()
                        continue
                    while b'event: client.changed' in buffer:
                        observed.append(time.perf_counter())
                        buffer = buffer.split(b'event: client.changed', 1)[1]
            except Exception as exc:  # pragma: no cover - surfaced by the assert
                error.append(exc)
                ready.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        if not ready.wait(timeout=timeout):
            raise RuntimeError('the SSE stream never announced itself')

        latencies = []
        for index in range(samples):
            recorded = time.perf_counter()
            client_events.record(
                BENCH_SERVER_ID, client_id='uuid-bench-0',
                email=BENCH_CLIENT, revision=revision + index + 1,
                operation='renew', client_state=None, deleted=False)
            deadline = time.perf_counter() + timeout
            while not observed and time.perf_counter() < deadline:
                time.sleep(0.005)
            if not observed:
                raise RuntimeError('client.changed never reached the stream reader')
            latencies.append((observed.pop(0) - recorded) * 1000.0)
        thread.join(timeout=timeout)
        if error:
            raise error[0]
        _ = refresh_policy  # imported for the stream's own activity bookkeeping
        return latencies
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _measure_external_xui(refresh_jobs, schedulers, samples=3, timeout=10.0):
    """Mark a panel hot and time the real periodic cycle until it polls it again."""
    import app as app_module
    from app import GLOBAL_SERVER_DATA, Server, db
    from panel.core import refresh_policy

    patches = []
    try:
        Server.query.filter(Server.id == BENCH_SERVER_ID).delete(synchronize_session=False)
        db.session.add(Server(id=BENCH_SERVER_ID, name='latency-bench',
                              host='https://latency.invalid', username='u', password='p',
                              panel_type='auto', enabled=True))
        db.session.commit()
        fetched_at = []

        def worker(server_dict):
            fetched_at.append(time.perf_counter())
            return (int(server_dict['id']), [], None, {'xui_version': '3.0'},
                    None, None, 'auto')

        from unittest import mock
        patch = mock.patch.object(app_module, 'fetch_worker', worker)
        patch.start()
        patches.append(patch)

        refresh_policy.reset_server_state()
        # First poll: the panel is unknown, so it is due immediately.
        schedulers._fetch_and_update_global_data_inner(force=False, periodic=True)
        if not fetched_at:
            raise RuntimeError('the first periodic cycle did not poll the bench panel')

        latencies = []
        for _ in range(samples):
            fetched_at.clear()
            # An Eve mutation (or the operator opening the card) marks it hot; the
            # next poll is then due after EVE_SERVER_POLL_ACTIVE_SECONDS.
            marked = time.perf_counter()
            refresh_policy.note_server_activity(BENCH_SERVER_ID)
            deadline = marked + timeout
            while not fetched_at:
                if time.perf_counter() > deadline:
                    raise RuntimeError('the watched panel was never polled again')
                schedulers._fetch_and_update_global_data_inner(
                    force=False, periodic=True)
                if fetched_at:
                    break
                time.sleep(0.05)
            latencies.append((fetched_at[0] - marked) * 1000.0)
        return latencies
    finally:
        for patch in patches:
            patch.stop()
        refresh_policy.reset_server_state()
        GLOBAL_SERVER_DATA['is_updating'] = False


def run(*, quick=False, iterations=None, external_samples=None, clients=200):
    os.environ['FLASK_ENV'] = 'development'
    os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
    os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
    db_path = os.path.join(tempfile.gettempdir(), 'eve-bench-latency-%d.db' % os.getpid())
    if os.path.exists(db_path):
        os.remove(db_path)
    os.environ['DATABASE_URL'] = 'sqlite:///' + db_path.replace(os.sep, '/')

    import app as app_module
    from app import GLOBAL_SERVER_DATA, app, db
    from panel.jobs import refresh as refresh_jobs
    from panel.jobs import schedulers
    from panel.core import refresh_policy

    iterations = iterations or (24 if quick else 120)
    external_samples = external_samples or (2 if quick else 5)

    with app.app_context():
        db.create_all()
        _seed_snapshot(app_module, refresh_jobs, clients=clients)
        commits, visible = _measure_mutation(refresh_jobs, iterations)
        cache_reads = _measure_cache_read(iterations)
        other_tabs = _measure_other_tabs(3 if quick else 8)
        external = _measure_external_xui(refresh_jobs, schedulers, samples=external_samples)

    measured = {
        'mutation_cache_commit_ms': commits,
        'browser_visible_ms': visible,
        'cache_read_ms': cache_reads,
        'other_tabs_ms': other_tabs,
        'external_xui_ms': external,
    }
    slos = {}
    for name, budget in SLOS.items():
        values = measured[name]
        value = p95(values)
        slos[name] = {
            'p95_ms': round(value, 3),
            'budget_ms': budget,
            'passed': value < budget,
            'samples': len(values),
            'mean_ms': round(statistics.fmean(values), 3) if values else 0.0,
            'max_ms': round(max(values), 3) if values else 0.0,
        }
    return {
        'mode': 'quick' if quick else 'full',
        'clients_in_snapshot': clients,
        'mutation_iterations': iterations,
        'slos': slos,
        'passed': all(row['passed'] for row in slos.values()),
        'snapshot_revision': GLOBAL_SERVER_DATA.get('last_update'),
        'refresh_activity_level': refresh_policy.activity_level(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Mutation/cache/UI latency SLOs')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--json', default=None)
    parser.add_argument('--iterations', type=int, default=None)
    parser.add_argument('--clients', type=int, default=200)
    args = parser.parse_args(argv)
    result = run(quick=args.quick, iterations=args.iterations, clients=args.clients)
    print('mode=%s iterations=%d clients=%d' % (
        result['mode'], result['mutation_iterations'], result['clients_in_snapshot']))
    for name, row in result['slos'].items():
        print('%-26s p95=%8.3f ms (mean %7.3f, max %8.3f, n=%d) budget %6.1f ms  %s' % (
            name, row['p95_ms'], row['mean_ms'], row['max_ms'], row['samples'],
            row['budget_ms'], 'OK' if row['passed'] else 'MISSED'))
    print('all SLOs met' if result['passed'] else 'SLO VIOLATION')
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
