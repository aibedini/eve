"""Simulate the adaptive refresh policy over a day (phase 17).

Compares the previous fixed 30 s cadence with the activity-aware policy for a
typical day: 8 hours with an operator watching, 16 hours idle. Reports how many
panel fan-outs each policy performs. Pure simulation of panel/core/refresh_policy.py
(no app, no database, no panels).

Usage:
    python scripts/benchmark_refresh_policy.py --quick
    python scripts/benchmark_refresh_policy.py --json docs/performance/refresh-policy.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def simulate(*, day_seconds=86400, fixed_interval=30, servers=12,
             active_start=9 * 3600, active_end=17 * 3600, request_every=30):
    from panel.core import refresh_policy
    refresh_policy.reset_state()
    # Keep the simulation hermetic: no Redis lookups from the activity helpers.
    refresh_policy._redis = lambda: None

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    last_fetch = None
    adaptive_cycles = 0
    level_seconds = {'active': 0, 'recent': 0, 'idle': 0}
    for second in range(day_seconds):
        now = base + timedelta(seconds=second)
        stamp = now.timestamp()
        if active_start <= second < active_end and second % request_every == 0:
            refresh_policy.record_activity(throttle_seconds=0, now=stamp)
        level_seconds[refresh_policy.activity_level(now=stamp)] += 1
        age = None if last_fetch is None else (now - last_fetch).total_seconds()
        should, _reason = refresh_policy.should_fetch_now(age, now=stamp)
        if should:
            last_fetch = now
            adaptive_cycles += 1
    fixed_cycles = day_seconds // fixed_interval
    reduction = 0.0 if not fixed_cycles else (fixed_cycles - adaptive_cycles) / fixed_cycles * 100.0
    refresh_policy.reset_state()
    return {
        'day_seconds': day_seconds,
        'fixed_interval_seconds': fixed_interval,
        'servers': servers,
        'fixed_cycles': fixed_cycles,
        'adaptive_cycles': adaptive_cycles,
        'reduction_pct': round(reduction, 1),
        'fixed_fanout_requests': fixed_cycles * servers,
        'adaptive_fanout_requests': adaptive_cycles * servers,
        'seconds_by_level': level_seconds,
        'policy_intervals': {
            'active': refresh_policy.active_interval(),
            'recent': refresh_policy.recent_interval(),
            'idle': refresh_policy.idle_interval(),
            'max_staleness': refresh_policy.max_staleness(),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Adaptive refresh policy simulation')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--json', default=None)
    parser.add_argument('--hours', type=float, default=None)
    args = parser.parse_args(argv)
    day_seconds = int((args.hours or (2 if args.quick else 24)) * 3600)
    result = simulate(day_seconds=day_seconds)
    print('window=%.1f h  fixed every %d s vs adaptive' % (
        day_seconds / 3600.0, result['fixed_interval_seconds']))
    print('fixed cycles=%d (%d panel fetches)' % (
        result['fixed_cycles'], result['fixed_fanout_requests']))
    print('adaptive cycles=%d (%d panel fetches)  reduction=%.1f%%' % (
        result['adaptive_cycles'], result['adaptive_fanout_requests'], result['reduction_pct']))
    print('time by level: %s' % result['seconds_by_level'])
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
