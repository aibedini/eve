"""Reproducible measurement of the subscription response cache (phase 18).

Simulates a burst of VPN-client subscription polls. Without the cache every poll
performs a panel read; with the cache only the first poll per (server, sub id,
variant) does, and a stampede of misses for the same key is coalesced into one
render. Pure module behaviour: no app, no panels.

Usage:
    python scripts/benchmark_subscription_cache.py --quick
    python scripts/benchmark_subscription_cache.py --json docs/performance/subscription-cache.json
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

QUICK = {'requests': 40, 'keys': 4, 'delay': 0.005, 'stampede': 8}
DEFAULT = {'requests': 400, 'keys': 20, 'delay': 0.02, 'stampede': 20}


def _panel_read(key, delay):
    time.sleep(delay)
    return (('body-for-%s' % key).encode(), 200, {'Content-Type': 'text/plain'})


def measure(requests=400, keys=20, delay=0.02, stampede=20, ttl=300):
    from panel.core import subscription_cache
    # Measurement is meaningless with the cache disabled, and the setting may
    # have been changed by another module in the same process; force it on.
    previous = os.environ.get('EVE_SUBSCRIPTION_CACHE_ENABLED')
    os.environ['EVE_SUBSCRIPTION_CACHE_ENABLED'] = '1'
    try:
        return _measure_locked(requests, keys, delay, stampede, ttl)
    finally:
        if previous is None:
            os.environ.pop('EVE_SUBSCRIPTION_CACHE_ENABLED', None)
        else:
            os.environ['EVE_SUBSCRIPTION_CACHE_ENABLED'] = previous
        subscription_cache.reset()


def _measure_locked(requests, keys, delay, stampede, ttl):
    from panel.core import subscription_cache
    subscription_cache.reset()
    key_list = [subscription_cache.make_key(1, 'sub-%d' % index) for index in range(keys)]

    # Before: every request renders (a live panel read).
    started = time.perf_counter()
    for index in range(requests):
        _panel_read(key_list[index % keys], delay)
    without_ms = (time.perf_counter() - started) * 1000.0

    # After: cache hit, or a single render per key.
    reads = {'count': 0}

    def cached_request(key):
        value = subscription_cache.get(key)
        if value is None:
            subscription_cache.note_miss()
        if value is not None:
            return value
        if subscription_cache.in_flight(key):
            subscription_cache.wait_for_fill(key, timeout=5)
            value = subscription_cache.get(key)
            if value is not None:
                return value
        if subscription_cache.begin(key):
            try:
                value = _panel_read(key, delay)
                reads['count'] += 1
                subscription_cache.set(key, value, ttl=ttl, variant='fast')
                return value
            finally:
                subscription_cache.end(key)
        return _panel_read(key, delay)

    started = time.perf_counter()
    for index in range(requests):
        cached_request(key_list[index % keys])
    with_ms = (time.perf_counter() - started) * 1000.0
    sequential_metrics = subscription_cache.metrics()

    # Stampede: concurrent misses for one key must render once.
    subscription_cache.reset()
    stampede_key = subscription_cache.make_key(2, 'burst')
    renders = {'count': 0}
    lock = threading.Lock()

    def burst():
        if subscription_cache.begin(stampede_key):
            try:
                value = _panel_read(stampede_key, delay)
                with lock:
                    renders['count'] += 1
                subscription_cache.set(stampede_key, value, ttl=ttl, variant='fast')
            finally:
                subscription_cache.end(stampede_key)
            return
        subscription_cache.wait_for_fill(stampede_key, timeout=5)
        subscription_cache.get(stampede_key)

    threads = [threading.Thread(target=burst) for _ in range(max(2, stampede))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    metrics = sequential_metrics
    return {
        'requests': requests,
        'distinct_keys': keys,
        'panel_reads_without_cache': requests,
        'panel_reads_with_cache': reads['count'],
        'without_cache_ms': round(without_ms, 1),
        'with_cache_ms': round(with_ms, 1),
        'speedup': round(without_ms / max(0.001, with_ms), 1),
        'hit_rate': metrics['hit_rate'],
        'hits': metrics['hits'],
        'evictions': metrics['evictions'],
        'stampede_callers': max(2, stampede),
        'stampede_renders': renders['count'],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Subscription cache measurement')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--json', default=None)
    args = parser.parse_args(argv)
    sizes = dict(QUICK if args.quick else DEFAULT)
    result = measure(**sizes)
    print('requests=%d over %d subscription keys' % (
        result['requests'], result['distinct_keys']))
    print('without cache: %d panel reads, %.1f ms' % (
        result['panel_reads_without_cache'], result['without_cache_ms']))
    print('with cache   : %d panel reads, %.1f ms  (%.1fx, hit rate %.0f%%)' % (
        result['panel_reads_with_cache'], result['with_cache_ms'],
        result['speedup'], result['hit_rate'] * 100))
    print('stampede: %d concurrent callers -> %d render(s)' % (
        result['stampede_callers'], result['stampede_renders']))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
