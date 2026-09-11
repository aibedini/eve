"""Reproducible measurement of bounded, coalesced panel access (phase 15).

Compares duplicate concurrent panel fetches with and without single flight, and
measures the process-wide concurrency cap. No app, database or panel needed.

Usage:
    python scripts/benchmark_panel_limits.py --quick
    python scripts/benchmark_panel_limits.py --json docs/performance/panel-limits.json
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


def measure_coalescing(callers, delay):
    from panel.core import panel_limits
    panel_limits.reset_panel_metrics()
    executions = []
    started = threading.Event()

    def work():
        started.set()
        time.sleep(delay)
        executions.append(1)

    def caller():
        with panel_limits.coalesce('duplicate') as slot:
            if slot.leader:
                work()
                slot.result = 'payload'

    threads = [threading.Thread(target=caller) for _ in range(callers)]
    wall_started = time.perf_counter()
    for index, thread in enumerate(threads):
        thread.start()
        if index == 0:
            started.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=30)
    wall_ms = (time.perf_counter() - wall_started) * 1000.0
    metrics = panel_limits.panel_metrics()
    return {
        'coalesced_executions': len(executions),
        'coalesced_wall_ms': round(wall_ms, 1),
        'coalesced_started': metrics['started'],
        'coalesced_followers': metrics['coalesced'],
    }


def measure_uncodalesced(callers, delay):
    executions = []
    started = threading.Event()

    def caller():
        started.set()
        time.sleep(delay)
        executions.append(1)

    threads = [threading.Thread(target=caller) for _ in range(callers)]
    wall_started = time.perf_counter()
    for index, thread in enumerate(threads):
        thread.start()
        if index == 0:
            started.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=30)
    return {
        'unbounded_executions': len(executions),
        'unbounded_wall_ms': round((time.perf_counter() - wall_started) * 1000.0, 1),
    }


def measure_cap(limit, tasks, delay):
    from panel.core import panel_limits
    os.environ['EVE_PANEL_CONCURRENCY'] = str(limit)
    panel_limits.reset_panel_metrics()
    current = {'value': 0, 'max': 0}
    lock = threading.Lock()

    def worker(index):
        with panel_limits.coalesce('cap:%d' % index, wait_seconds=30):
            with lock:
                current['value'] += 1
                current['max'] = max(current['max'], current['value'])
            time.sleep(delay)
            with lock:
                current['value'] -= 1

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(tasks)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    os.environ.pop('EVE_PANEL_CONCURRENCY', None)
    return {
        'cap_limit': limit,
        'cap_tasks': tasks,
        'cap_max_concurrency': current['max'],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Panel concurrency measurement')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--json', default=None)
    parser.add_argument('--callers', type=int, default=None)
    parser.add_argument('--delay', type=float, default=None)
    args = parser.parse_args(argv)
    callers = args.callers or (4 if args.quick else 20)
    delay = args.delay or (0.02 if args.quick else 0.05)
    result = {'callers': callers, 'delay_seconds': delay}
    result.update(measure_uncodalesced(callers, delay))
    result.update(measure_coalescing(callers, delay))
    result.update(measure_cap(2 if args.quick else 4, 8 if args.quick else 16, delay))
    print('callers=%d delay=%.3fs' % (callers, delay))
    print('without single flight: %d panel fetches in %.1f ms' % (
        result['unbounded_executions'], result['unbounded_wall_ms']))
    print('with single flight   : %d panel fetch(es) in %.1f ms (%d followers coalesced)' % (
        result['coalesced_executions'], result['coalesced_wall_ms'],
        result['coalesced_followers']))
    print('concurrency cap %d over %d tasks -> max %d simultaneous' % (
        result['cap_limit'], result['cap_tasks'], result['cap_max_concurrency']))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
