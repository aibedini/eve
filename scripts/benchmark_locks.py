"""Reproducible measurement of the refresh lock scoping (phase 14).

Runs a stubbed panel fan-out in one thread while a reader keeps acquiring
GLOBAL_REFRESH_LOCK, and reports how long the reader was blocked. Before phase 14
the fetch callers held the snapshot lock for the whole fan-out; now they hold it
only for the short in-memory commits.

Usage:
    python scripts/benchmark_locks.py --quick
    python scripts/benchmark_locks.py --json docs/performance/refresh-lock.json
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def run(*, servers=6, fetch_delay=0.2, rounds=1):
    os.environ['FLASK_ENV'] = 'development'
    os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
    os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
    db_path = os.path.join(tempfile.gettempdir(), 'eve-bench-locks-%d.db' % os.getpid())
    if os.path.exists(db_path):
        os.remove(db_path)
    os.environ['DATABASE_URL'] = 'sqlite:///' + db_path.replace(os.sep, '/')

    import app as app_module
    from app import Server, app, db
    from panel.core.redis_client import GLOBAL_REFRESH_LOCK
    from panel.jobs import schedulers
    from panel.jobs import refresh as refresh_jobs

    with app.app_context():
        db.create_all()
        refresh_jobs.REFRESH_BACKOFF.clear()
        Server.query.delete()
        for index in range(servers):
            db.session.add(Server(id=9300 + index, name='lock-%d' % index,
                                  host='https://lock.invalid', username='u',
                                  password='p', panel_type='auto', enabled=True))
        db.session.commit()

    def slow_fetch(server_dict):
        time.sleep(fetch_delay)
        return (server_dict['id'], [], None, None, None, None, 'auto')

    holds = []

    class TimingLock:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._started = time.perf_counter()
            return self._inner.__enter__()

        def __exit__(self, *args):
            holds.append((time.perf_counter() - self._started) * 1000.0)
            return self._inner.__exit__(*args)

        def acquire(self, *args, **kwargs):
            return self._inner.acquire(*args, **kwargs)

        def release(self):
            return self._inner.release()

    waits = []
    worst_fetch = 0.0
    for _ in range(rounds):
        stop = threading.Event()
        waits.clear()
        holds.clear()
        reader = threading.Thread(target=_reader, args=(stop, waits), daemon=True)
        reader.start()
        with app.app_context():
            with mock.patch.object(app_module, 'fetch_worker', slow_fetch), \
                 mock.patch.object(schedulers, 'GLOBAL_REFRESH_LOCK', TimingLock(GLOBAL_REFRESH_LOCK)):
                started = time.perf_counter()
                schedulers.fetch_and_update_global_data(force=True)
                worst_fetch = max(worst_fetch, (time.perf_counter() - started) * 1000.0)
        stop.set()
        reader.join(timeout=2)
    return {
        'servers': servers,
        'fetch_delay_seconds': fetch_delay,
        'rounds': rounds,
        'fetch_ms': round(worst_fetch, 1),
        'reader_worst_block_ms': round(max(waits) if waits else 0.0, 3),
        'reader_mean_block_ms': round(statistics.fmean(waits) if waits else 0.0, 4),
        'reader_samples': len(waits),
        'lock_sections': len(holds),
        'lock_section_max_ms': round(max(holds) if holds else 0.0, 3),
        'lock_section_total_ms': round(sum(holds), 3),
    }


def _reader(stop, waits):
    from panel.core.redis_client import GLOBAL_REFRESH_LOCK
    while not stop.is_set():
        started = time.perf_counter()
        with GLOBAL_REFRESH_LOCK:
            pass
        waits.append((time.perf_counter() - started) * 1000.0)
        time.sleep(0.003)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Refresh lock measurement')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--json', default=None)
    parser.add_argument('--servers', type=int, default=None)
    parser.add_argument('--fetch-delay', dest='fetch_delay', type=float, default=None)
    args = parser.parse_args(argv)
    result = run(servers=args.servers or (2 if args.quick else 6),
                 fetch_delay=args.fetch_delay or (0.05 if args.quick else 0.2))
    print('servers=%d fetch_delay=%.2fs fetch=%.1f ms' % (
        result['servers'], result['fetch_delay_seconds'], result['fetch_ms']))
    print('reader worst block=%.3f ms (mean %.4f, samples %d)' % (
        result['reader_worst_block_ms'], result['reader_mean_block_ms'],
        result['reader_samples']))
    print('lock sections=%d max=%.3f ms total=%.3f ms' % (
        result['lock_sections'], result['lock_section_max_ms'],
        result['lock_section_total_ms']))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
