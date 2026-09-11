"""Reproducible measurement of the database connection pool (phase 16).

Runs the same number of checkouts against a pooled engine (the application
policy) and an unpooled NullPool engine on the same SQLite file, counting the
physical connections opened. Also reports the worker connection demand used for
the max_connections audit. No app or database server needed.

Usage:
    python scripts/benchmark_db_pool.py --quick
    python scripts/benchmark_db_pool.py --json docs/performance/db-pool.json
"""
import argparse
import json
import os
import sys
import tempfile
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _counter(engine):
    from sqlalchemy import event
    hits = {'connects': 0}

    def _on_connect(_dbapi_connection, _record):
        hits['connects'] += 1

    event.listen(engine, 'connect', _on_connect)
    return hits


def measure(iterations=200):
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool
    from panel.core.db_pool import engine_options

    db_path = os.path.join(tempfile.gettempdir(), 'eve-bench-pool-%d.db' % os.getpid())
    if os.path.exists(db_path):
        os.remove(db_path)
    url = 'sqlite:///' + db_path.replace(os.sep, '/')

    options = engine_options(url)
    pooled = create_engine(url, **options)
    unpooled = create_engine(url, poolclass=NullPool)

    pooled_hits = _counter(pooled)
    started = time.perf_counter()
    for _ in range(iterations):
        with pooled.connect() as connection:
            connection.execute(text('SELECT 1'))
    pooled_ms = (time.perf_counter() - started) * 1000.0

    unpooled_hits = _counter(unpooled)
    started = time.perf_counter()
    for _ in range(iterations):
        with unpooled.connect() as connection:
            connection.execute(text('SELECT 1'))
    unpooled_ms = (time.perf_counter() - started) * 1000.0

    demand = []
    for workers in (1, 2, 4, 8):
        demand.append({
            'workers': workers,
            'expected_max_connections': int(workers) * (
                int(options.get('pool_size') or 0) + int(options.get('max_overflow') or 0)),
        })

    pooled.dispose()
    unpooled.dispose()
    try:
        os.remove(db_path)
    except OSError:
        pass
    return {
        'iterations': iterations,
        'pool_size': options.get('pool_size'),
        'max_overflow': options.get('max_overflow'),
        'pool_timeout': options.get('pool_timeout'),
        'pool_recycle': options.get('pool_recycle'),
        'pooled_connects': pooled_hits['connects'],
        'pooled_ms': round(pooled_ms, 1),
        'unpooled_connects': unpooled_hits['connects'],
        'unpooled_ms': round(unpooled_ms, 1),
        'worker_demand': demand,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Database pool measurement')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--json', default=None)
    parser.add_argument('--iterations', type=int, default=None)
    args = parser.parse_args(argv)
    iterations = args.iterations or (20 if args.quick else 200)
    result = measure(iterations)
    print('iterations=%d pool_size=%s max_overflow=%s' % (
        result['iterations'], result['pool_size'], result['max_overflow']))
    print('pooled   : %d physical connections in %.1f ms' % (
        result['pooled_connects'], result['pooled_ms']))
    print('unpooled : %d physical connections in %.1f ms' % (
        result['unpooled_connects'], result['unpooled_ms']))
    for row in result['worker_demand']:
        print('  %d worker(s) -> %d connections max' % (
            row['workers'], row['expected_max_connections']))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
