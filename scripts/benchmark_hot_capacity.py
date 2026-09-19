"""HOT capacity: how many panels can actually be polled every cadence?

Two different questions, which the wall-clock benchmark in
`scripts/benchmark_per_server_scheduling.py` deliberately answers only the first of:

  A) INSTALL SIZE SCALING -- 1 HOT panel among N-1 IDLE panels. Does the HOT panel's
     cadence depend on the size of the install? (It must not.)
  B) HOT CONCURRENCY CAPACITY -- M panels HOT at the same time. When does the worker
     pool stop being able to serve them at their cadence, and how does it degrade?

This script answers (B), and it does it **without sleeping**: the worker pool is
simulated in virtual time, while every scheduling decision comes from the REAL policy
(`refresh_policy.scheduler_plan`, `server_due`, `note_fetch_started`,
`note_server_result`). That is the same seam the scheduler loop uses, so what is
measured is the product's own arithmetic; only the clock and the executor are modelled.

Model (deliberately small, and stated so the numbers can be argued with):
  * time advances to the next event (a completion, or a due time);
  * at each step the free workers ask the policy for the most urgent due panels;
  * a dispatched read completes `read_seconds` later;
  * the loop's own wake-up granularity is charged as `--tick-ms`.

Reported per configuration: HOT start-to-start p50/p95, queue delay p95 (how late a due
panel actually started), missed deadlines (a start more than 1.5x the cadence after the
previous one), saturation events, and the idle/WARM feed rate.

Usage:
    .venv\\Scripts\\python.exe scripts\\benchmark_hot_capacity.py
    .venv\\Scripts\\python.exe scripts\\benchmark_hot_capacity.py --json hot-capacity.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')

from panel.core import refresh_policy  # noqa: E402

HOT_BASE = 700000
IDLE_BASE = 800000


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(pct / 100.0 * (len(ordered) - 1) + 0.5)))
    return round(ordered[index], 3)


def simulate(*, hot_count, idle_count, read_seconds, cadence=2.0, workers=5,
             horizon=180.0, tick_seconds=0.05, warm_seconds=10.0, idle_seconds=45.0,
             jitter_seconds=10.0, idle_changes=True):
    """Run the policy's scheduler in virtual time; return the measured behaviour.

    ``idle_changes`` models a BUSY install: every idle poll reports new traffic. That is
    the case that decides whether a busy install promotes itself into the warm band, so it
    is measured rather than assumed.
    """
    env = {
        'EVE_SERVER_POLL_ACTIVE_SECONDS': str(int(cadence)) if cadence >= 1 else '1',
        'EVE_SERVER_POLL_WARM_SECONDS': str(int(warm_seconds)),
        'EVE_SERVER_POLL_IDLE_SECONDS': str(int(idle_seconds)),
        'EVE_SERVER_POLL_IDLE_JITTER_SECONDS': str(jitter_seconds),
        'EVE_SERVER_POLL_ACTIVE_TTL_SECONDS': '120',
    }
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    refresh_policy.reset_state()
    try:
        hot_ids = [HOT_BASE + index for index in range(hot_count)]
        idle_ids = [IDLE_BASE + index for index in range(idle_count)]
        rows = [{'id': sid} for sid in hot_ids + idle_ids]

        # The HOT panels are declared watched, exactly as a dashboard would declare them.
        # Without this the whole simulation is an IDLE install wearing HOT labels - which
        # is the mistake this harness exists to make impossible.
        for sid in hot_ids:
            refresh_policy.note_server_activity(sid, now=-5.0, share=False)
            refresh_policy.note_server_result(sid, True, now=-5.0)

        # Start at t=0 with nothing scheduled: every panel is due on the first tick, which
        # is the cold start (the worst case for a capacity question).
        now = 0.0
        inflight = {}          # sid -> completion time
        starts = {}            # sid -> [start times]
        queue_delays = []
        idle_starts = 0
        hot_starts = 0
        starts_by_mode = {'hot': 0, 'warm': 0, 'idle': 0, 'backoff': 0}
        saturation_ticks = 0
        ticks = 0
        last_renew = 0.0
        # A real dashboard renews its watch marks continuously (every poll, and every SSE
        # tick). Without that the HOT window (EVE_SERVER_POLL_ACTIVE_TTL_SECONDS, 120 s)
        # expires mid-run and the panel correctly falls to WARM -- which would look like a
        # scheduling failure in a measurement that forgot to model the dashboard.
        renew_every = 30.0

        while now < horizon:
            ticks += 1
            if hot_ids and (now - last_renew) >= renew_every:
                last_renew = now
                for sid in hot_ids:
                    refresh_policy.note_server_activity(sid, now=now, share=False)
            # Completions first: a finished read is what frees a worker.
            finished = [sid for sid, when in inflight.items() if when <= now]
            for sid in finished:
                inflight.pop(sid)
                refresh_policy.note_server_result(
                    sid, True, now=now, duration_ms=int(read_seconds * 1000),
                    # A busy install: an idle panel's counters really do move between two
                    # of its own polls. The WARM band must not interpret that as "keep me
                    # at 10 s forever", so this flag is what makes the feed rate below an
                    # honest answer for a busy install rather than for a frozen one.
                    changed=bool(idle_changes))
            free = max(0, workers - len(inflight))
            if free:
                plan = refresh_policy.scheduler_plan(rows, now=now, limit=free)
                due_total = len(refresh_policy.scheduler_plan(rows, now=now))
                if due_total > free:
                    saturation_ticks += 1
                for sid in plan:
                    state = refresh_policy._servers.get(sid) or {}
                    due_at = float(state.get('next_due') or now)
                    delay = max(0.0, now - due_at)
                    queue_delays.append(round(delay * 1000.0, 3))
                    mode = refresh_policy.server_mode(sid, now=now)
                    starts_by_mode[mode] = starts_by_mode.get(mode, 0) + 1
                    refresh_policy.note_fetch_started(sid, now=now, queued_at=now)
                    starts.setdefault(sid, []).append(now)
                    if sid in hot_ids:
                        hot_starts += 1
                    else:
                        idle_starts += 1
                    inflight[sid] = now + read_seconds
            # Advance virtual time to the next interesting moment: a completion, the next
            # due panel, or one scheduler tick - whichever comes first.
            candidates = [when for when in inflight.values()]
            due_in = refresh_policy.next_server_due_in(now=now)
            if due_in is not None:
                candidates.append(now + due_in)
            candidates.append(now + tick_seconds)
            nxt = min(candidates)
            now = nxt if nxt > now else now + tick_seconds

        def cadence_stats(ids):
            gaps = []
            for sid in ids:
                times = starts.get(sid) or []
                gaps.extend(round((b - a) * 1000.0, 3)
                            for a, b in zip(times, times[1:]))
            return gaps

        hot_gaps = cadence_stats(hot_ids)
        target_ms = cadence * 1000.0
        missed = [gap for gap in hot_gaps if gap > target_ms * 1.5]
        return {
            'hot_servers': hot_count,
            'idle_servers': idle_count,
            'read_ms': int(read_seconds * 1000),
            'workers': workers,
            'cadence_ms': target_ms,
            'horizon_s': horizon,
            'hot_polls': hot_starts,
            'idle_polls': idle_starts,
            'hot_start_to_start_p50_ms': _percentile(hot_gaps, 50),
            'hot_start_to_start_p95_ms': _percentile(hot_gaps, 95),
            'hot_max_gap_ms': round(max(hot_gaps), 3) if hot_gaps else None,
            'queue_delay_p95_ms': _percentile(queue_delays, 95),
            'queue_delay_max_ms': round(max(queue_delays), 3) if queue_delays else None,
            'missed_deadlines': len(missed),
            'deadline_miss_pct': (round(100.0 * len(missed) / len(hot_gaps), 2)
                                  if hot_gaps else None),
            'saturation_ticks': saturation_ticks,
            'ticks': ticks,
            'idle_feeds_per_minute': round(idle_starts * 60.0 / horizon, 2),
            'feeds_per_minute_total': round((hot_starts + idle_starts) * 60.0 / horizon, 2),
            'starts_by_mode': starts_by_mode,
            'feeds_per_minute_by_mode': {
                mode: round(count * 60.0 / horizon, 2)
                for mode, count in starts_by_mode.items()},
            'final_modes': {
                mode: sum(1 for sid in hot_ids + idle_ids
                          if refresh_policy.server_mode(sid, now=now) == mode)
                for mode in ('hot', 'warm', 'idle', 'backoff')},
        }
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        refresh_policy.reset_state()


def theoretical_capacity(workers, cadence, read_seconds):
    """workers * cadence / read -- the rate the pool can sustain, in HOT panels."""
    if read_seconds <= 0:
        return float('inf')
    return workers * cadence / read_seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', default=None)
    parser.add_argument('--horizon', type=float, default=180.0)
    parser.add_argument('--workers', type=int, default=5)
    parser.add_argument('--cadence', type=float, default=2.0)
    args = parser.parse_args()

    started = time.perf_counter()
    rows = []
    print('Theoretical HOT capacity = workers * cadence / read  (workers=%d, cadence=%.1fs)'
          % (args.workers, args.cadence))
    for read_ms in (100, 300, 1000):
        print('  read=%4dms -> %5.1f HOT panels'
              % (read_ms, theoretical_capacity(args.workers, args.cadence, read_ms / 1000.0)))
    print()

    print('B) HOT concurrency capacity: M panels HOT at once, 1 idle neighbour, read=300ms')
    print('   %-6s %-9s %-9s %-9s %-9s %-9s %-9s' % (
        'hot', 'p50', 'p95', 'qdelay95', 'missed', 'miss%', 'sat.ticks'))
    for hot_count in (1, 10, 20, 30, 40):
        row = simulate(hot_count=hot_count, idle_count=1, read_seconds=0.3,
                       cadence=args.cadence, workers=args.workers, horizon=args.horizon)
        rows.append(row)
        print('   %-6d %-9s %-9s %-9s %-9s %-9s %-9s' % (
            row['hot_servers'], row['hot_start_to_start_p50_ms'],
            row['hot_start_to_start_p95_ms'], row['queue_delay_p95_ms'],
            row['missed_deadlines'], row['deadline_miss_pct'], row['saturation_ticks']))

    print()
    print('B2) Same, read=1000ms (the pool can only serve ~10 of these)')
    for hot_count in (5, 10, 15):
        row = simulate(hot_count=hot_count, idle_count=1, read_seconds=1.0,
                       cadence=args.cadence, workers=args.workers, horizon=args.horizon)
        rows.append(row)
        print('   %-6d %-9s %-9s %-9s %-9s %-9s %-9s' % (
            row['hot_servers'], row['hot_start_to_start_p50_ms'],
            row['hot_start_to_start_p95_ms'], row['queue_delay_p95_ms'],
            row['missed_deadlines'], row['deadline_miss_pct'], row['saturation_ticks']))

    print()
    print('A) Install size scaling: 1 HOT among N-1 IDLE, read=300ms, IDLE=45s')
    for idle_count in (0, 9, 49, 99):
        row = simulate(hot_count=1, idle_count=idle_count, read_seconds=0.3,
                       cadence=args.cadence, workers=args.workers, horizon=args.horizon)
        row['label'] = 'install-size'
        rows.append(row)
        print('   idle=%-4d hot p50=%-8s p95=%-8s qdelay95=%-8s idle feeds/min=%-7s'
              % (idle_count, row['hot_start_to_start_p50_ms'],
                 row['hot_start_to_start_p95_ms'], row['queue_delay_p95_ms'],
                 row['idle_feeds_per_minute']))

    print()
    print('C) Idle feed rate and the WARM question (100 panels, one HOT, read=300ms, IDLE=45s)')
    print('   %-14s %-14s %-16s %s' % ('install', 'idle feeds/min', 'by mode', 'final modes'))
    for label, changes in (('calm', False), ('busy (traffic moves)', True)):
        row = simulate(hot_count=1, idle_count=99, read_seconds=0.3,
                       cadence=args.cadence, workers=args.workers, horizon=300.0,
                       idle_seconds=45, idle_changes=changes)
        row['label'] = 'idle-load-%s' % label
        rows.append(row)
        expected = 99 * 60.0 / (45 + 5.0)   # 45 s band + half the 10 s jitter span
        print('   %-14s %-14s %-16s %s   (model ~%.1f/min)'
              % (label, row['idle_feeds_per_minute'],
                 row['feeds_per_minute_by_mode'], row['final_modes'], expected))

    elapsed = time.perf_counter() - started
    print('\nvirtual-time harness wall clock: %.2fs' % elapsed)
    payload = {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'workers': args.workers,
        'cadence_s': args.cadence,
        'horizon_s': args.horizon,
        'model': ('virtual time; worker pool simulated; every scheduling decision from '
                  'panel/core/refresh_policy.py'),
        'theoretical_hot_capacity': {
            str(read): round(theoretical_capacity(args.workers, args.cadence, read / 1000.0), 2)
            for read in (100, 300, 1000)},
        'rows': rows,
        'wall_clock_s': round(elapsed, 3),
    }
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print('wrote %s' % args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
