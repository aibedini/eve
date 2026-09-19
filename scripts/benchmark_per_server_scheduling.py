"""Per-server scheduling benchmark: does a HOT panel wait for the install?

The claim under test is architectural, not micro-optimisational:

    a HOT panel's poll cadence must not be a function of how many OTHER panels
    exist, nor of how long reading them takes.

Two dispatch shapes are measured against the SAME simulated panel latency and the same
policy (`panel/core/refresh_policy.py`):

* ``cycle`` -- the shape this repository used until the per-server scheduler landed:
  collect the due panels, hand the batch to a bounded pool, WAIT FOR THE BATCH, sleep,
  repeat. It is driven through the real entry point
  (``schedulers._fetch_and_update_global_data_inner(periodic=True)``) against a real
  (temporary, sqlite) database and a stubbed panel read, so the "before" number is the
  product's own code path and not a re-implementation of it.
* ``per-server`` -- ``schedulers.run_per_server_scheduler()``: one dispatch decision per
  free worker, one server per decision, rescheduled from its own completion. Its panel
  read is injected because the loop is designed to be drivable that way (the tests use
  the same seam); the latency injected is exactly the latency the cycle path sleeps for.

Reported per run, for the HOT panel:

* ``hot_start_to_start_p50/p95_ms`` -- the number the SLA is about
* ``hot_queue_delay_p95_ms`` -- how late a due HOT panel started against its schedule
  (this is what worker saturation looks like, and it is reported instead of hidden)
* ``idle_fetches_per_minute`` -- what the rest of the install costs
* ``wake_to_dispatch_ms`` -- from a watch nudge to the HOT read starting
* ``cpu_ms_per_fetch`` -- process CPU spent per panel read
* ``max_inflight`` / ``saturation_events`` -- capacity evidence

Usage (from the repository root, with the project venv):

    .venv\\Scripts\\python.exe scripts\\benchmark_per_server_scheduling.py --json out.json
    .venv\\Scripts\\python.exe scripts\\benchmark_per_server_scheduling.py --quick
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ.setdefault('FLASK_ENV', 'development')
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')

import app as app_module  # noqa: E402
from app import Server, app, db  # noqa: E402
from panel.core import panel_limits, refresh_policy  # noqa: E402
from panel.jobs import schedulers  # noqa: E402

HOT_SERVER_ID = 900001


class PanelSim:
    """A fake panel read with a fixed latency, recording when each read started."""

    def __init__(self, latency_ms, hot_id=HOT_SERVER_ID):
        self.latency = max(0.0, latency_ms / 1000.0)
        self.hot_id = hot_id
        self.starts = {}
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0

    def __call__(self, sid):
        sid = int(sid if not isinstance(sid, dict) else sid.get('id'))
        with self.lock:
            self.starts.setdefault(sid, []).append(time.monotonic())
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            time.sleep(self.latency)
        finally:
            with self.lock:
                self.inflight -= 1
        return {'server_id': sid, 'changed': False, 'block': [], 'stats': {}}

    def hot_starts(self):
        with self.lock:
            return list(self.starts.get(self.hot_id, []))


def _gaps_ms(starts):
    return [(b - a) * 1000.0 for a, b in zip(starts, starts[1:])]


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    # Round half UP: Python's round() is banker's rounding, which turns the median of
    # two samples into the lower one and quietly biases a p50 report.
    index = int(pct / 100.0 * (len(ordered) - 1) + 0.5)
    index = min(len(ordered) - 1, max(0, index))
    return round(ordered[index], 3)


def _cpu_ms():
    """Process CPU time in ms.

    ``time.process_time()`` is the process's own user+system time, so it excludes time
    spent sleeping on the simulated panel latency. On Windows its resolution is the
    system tick (~15.6 ms), which is why the report carries the raw total as well: a
    per-fetch number below one tick is not a measurement of anything.
    """
    return time.process_time() * 1000.0


def _seed_servers(count):
    """Enabled panel rows: the cycle path reads them from the database itself."""
    Server.query.delete()
    db.session.commit()
    for index in range(count):
        sid = HOT_SERVER_ID + index
        db.session.add(Server(id=sid, name='bench-%d' % sid, host='https://bench.invalid',
                              username='u', password='p', panel_type='auto', enabled=True))
    db.session.commit()


def _reset_policy(servers, hot=True):
    refresh_policy.reset_state()
    now = time.time()
    if hot:
        # The HOT panel is declared watched and already overdue, so its first read is
        # due at t=0 for both shapes.
        refresh_policy.note_server_activity(HOT_SERVER_ID, now=now - 30)
        refresh_policy.note_server_result(HOT_SERVER_ID, True, now=now - 30)
    # Idle panels start due as well: a cold start is the worst case for a HOT panel and
    # the one that exposes a batch barrier.
    return now


def run_cycle(app_ctx, sim, servers, seconds, workers):
    """The previous dispatch shape, through the product's own entry point."""
    original_fetch_worker = app_module.fetch_worker

    def fake_fetch_worker(server_dict):
        # The cycle path expects the fetch result tuple it has always expected:
        # (sid, inbounds, online_index, status_payload, status_error, error, type).
        sid = sim(int(server_dict['id']))
        return (sid, [], None, {'xui_version': '3.0'}, None, None, 'auto')

    app_module.fetch_worker = fake_fetch_worker
    fetches = 0
    cpu0 = _cpu_ms()
    started = time.monotonic()
    try:
        with app_ctx:
            while time.monotonic() - started < seconds:
                before = {sid: len(v) for sid, v in sim.starts.items()}
                schedulers._fetch_and_update_global_data_inner(force=False, periodic=True)
                after = {sid: len(v) for sid, v in sim.starts.items()}
                fetches += sum(max(0, after.get(sid, 0) - before.get(sid, 0))
                               for sid in after)
                due_in = refresh_policy.next_server_due_in()
                slice_seconds = refresh_policy.max_wake_slice()
                if due_in is not None:
                    slice_seconds = min(slice_seconds, max(0.25, due_in))
                refresh_policy.wait_for_interval(slice_seconds)
    finally:
        app_module.fetch_worker = original_fetch_worker
        refresh_policy.wake()
    return fetches, _cpu_ms() - cpu0


def run_per_server(app_ctx, sim, servers, seconds, workers):
    """The new shape: independent per-server dispatch, no batch barrier."""
    rows = [{'id': HOT_SERVER_ID + index} for index in range(servers)]
    cpu0 = _cpu_ms()
    metrics = schedulers.run_per_server_scheduler(
        fetch_callable=sim, server_rows=rows, duration=seconds, worker_limit=workers)
    return int(metrics.get('completed') or 0), _cpu_ms() - cpu0, metrics


def measure_wake_latency(app_ctx, sim, workers, rounds, latency_ms):
    """Nudge an idle panel and measure until its read starts.

    The panel is idle (a long window away) and the loop is parked; the nudge is the
    real mutation path -- ``note_server_activity`` with ``share=True``, which publishes
    the mark and the wake -- and it makes the panel overdue. So what is measured is
    exactly the wake path: nudge -> loop notices -> dispatch, with nothing else between
    them. This is the in-process half of the cross-process proof; the Redis harness
    (scripts/integration_redis_multiprocess.py) measures the other half.
    """
    samples = []
    for index in range(rounds):
        sid = 950000 + index
        refresh_policy.reset_state()
        refresh_policy.note_server_result(sid, True, now=time.time())
        rows = [{'id': sid}]
        stop = threading.Event()

        def worker():
            schedulers.run_per_server_scheduler(
                fetch_callable=sim, server_rows=rows, duration=3.0,
                stop_event=stop, worker_limit=workers)

        with app_ctx:
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            time.sleep(0.3)  # let the loop settle into its (long) wait
            started = time.monotonic()
            refresh_policy.note_server_activity(sid, now=time.time() - 10)
            refresh_policy.publish_wake([sid], reason='benchmark')
            deadline = started + 2.0
            while time.monotonic() < deadline:
                ours = sim.starts.get(sid) or []
                if ours:
                    samples.append((ours[0] - started) * 1000.0)
                    break
                time.sleep(0.002)
            stop.set()
            thread.join(timeout=4.0)
    return samples


def _apply_common_env(idle_seconds, active_seconds=2):
    """Compress the idle band so several idle due-events fall inside the window.

    A HOT panel is only delayed by the rest of the install when the rest of the install
    is actually being read. On a real install with a 45 s idle band that happens in
    bursts -- and a ten-second measurement would simply miss them, which is how a
    cycle-oriented loop can look healthy in a benchmark and still starve the panel the
    operator is watching. Compressing the band to a few seconds makes those bursts part
    of every window; it changes how OFTEN the burst happens, not how long a HOT panel
    waits inside it, which is the quantity under test.
    """
    os.environ['EVE_SERVER_POLL_IDLE_SECONDS'] = str(idle_seconds)
    os.environ['EVE_SERVER_POLL_ACTIVE_SECONDS'] = str(active_seconds)
    os.environ['EVE_SERVER_POLL_WARM_SECONDS'] = str(active_seconds)


def one_run(app_ctx, mode, servers, latency_ms, seconds, workers, idle_seconds,
            hot=True):
    sim = PanelSim(latency_ms)
    _seed_servers(servers)
    _reset_policy(servers, hot=hot)
    saved_env = {}
    _apply_common_env(idle_seconds)
    if mode == 'legacy-cycle':
        # The architecture before the per-server policy: one sweep of every due panel,
        # no batch cap and no idle jitter. Reproduced by disabling exactly those two
        # additions, which is what made the sweep whole-install again.
        for name, value in (('EVE_REFRESH_BATCH_SERVERS', '0'),
                            ('EVE_SERVER_POLL_IDLE_JITTER_SECONDS', '0')):
            saved_env[name] = os.environ.get(name)
            os.environ[name] = value
        mode = 'cycle'
    try:
        if mode == 'cycle':
            fetches, cpu_ms = run_cycle(app_ctx, sim, servers, seconds, workers)
            metrics = {'workers': workers, 'max_inflight': sim.max_inflight}
        else:
            fetches, cpu_ms, metrics = run_per_server(app_ctx, sim, servers, seconds, workers)
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    row_mode = 'legacy-cycle' if saved_env else mode
    if mode == 'cycle':
        # The cycle path reschedules from the policy like the loop it emulates; re-mark
        # the HOT panel so the next measured run starts from the same cold state.
        refresh_policy.reset_state()
        refresh_policy.note_server_activity(HOT_SERVER_ID, now=time.time() - 30)
        refresh_policy.note_server_result(HOT_SERVER_ID, True, now=time.time() - 30)
    return _row(row_mode, servers, latency_ms, workers, seconds, sim, fetches, cpu_ms,
                metrics)

def _row(mode, servers, latency_ms, workers, seconds, sim, fetches, cpu_ms, metrics):
    hot = _gaps_ms(sim.hot_starts())
    # The first gap is the cold-start catch-up: the panel starts overdue, so the second
    # read happens as soon as the first returns. Steady state is what the cadence claim
    # is about, so the two are reported separately instead of one average that hides it.
    hot_steady = hot[1:]
    idle_starts = [s for sid, starts in sim.starts.items() if int(sid) != HOT_SERVER_ID
                   for s in starts]
    queue_delays = []
    if mode == 'per-server' and hasattr(refresh_policy, 'server_sync_state'):
        for index in range(servers):
            sid = HOT_SERVER_ID + index
            try:
                delay = refresh_policy.server_sync_state(sid).get('scheduler_queue_delay_ms')
            except Exception:
                delay = None
            if isinstance(delay, (int, float)):
                queue_delays.append(float(delay))
    row = {
        'mode': mode,
        'servers': servers,
        'panel_latency_ms': latency_ms,
        'workers': workers,
        'fetches': fetches,
        'fetches_per_minute': round(fetches * 60.0 / seconds, 1),
        'idle_fetches_per_minute': round(len(idle_starts) * 60.0 / seconds, 1),
        'hot_polls': len(sim.hot_starts()),
        'hot_first_gap_ms': round(hot[0], 3) if hot else None,
        'hot_start_to_start_p50_ms': _percentile(hot_steady, 50),
        'hot_start_to_start_p95_ms': _percentile(hot_steady, 95),
        'hot_start_to_start_mean_ms': (round(statistics.fmean(hot_steady), 3)
                                       if hot_steady else None),
        'hot_max_gap_ms': round(max(hot_steady), 3) if hot_steady else None,
        'hot_gaps_ms': [round(gap, 1) for gap in hot],
        'hot_queue_delay_p95_ms': _percentile(queue_delays, 95),
        'max_inflight': int(metrics.get('max_inflight') or sim.max_inflight or 0),
        'saturation_events': int(metrics.get('saturation_events') or 0),
        'worker_saturation_pct': (
            round(100.0 * float(metrics.get('saturation_events') or 0)
                  / max(1, int(metrics.get('dispatched') or 1)), 2)
            if mode == 'per-server' else None),
        'cpu_ms_total': round(cpu_ms, 3),
        'cpu_ms_per_fetch': round(cpu_ms / fetches, 4) if fetches else None,
    }
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--servers', default='1,10,50,100',
                        help='comma-separated install sizes')
    parser.add_argument('--latency-ms', default='100,300,1000',
                        help='comma-separated simulated panel read times')
    parser.add_argument('--seconds', type=float, default=10.0,
                        help='measured window per run')
    parser.add_argument('--workers', type=int, default=int(os.environ.get('EVE_REFRESH_WORKERS', '5')))
    parser.add_argument('--modes', default='legacy-cycle,cycle,per-server',
                        help='dispatch shapes to measure')
    parser.add_argument('--wake-rounds', type=int, default=5)
    parser.add_argument('--idle-seconds', type=float, default=5.0,
                        help='idle band used during the measurement (production: 45)')
    parser.add_argument('--json', default=None, help='write the raw measurements here')
    parser.add_argument('--quick', action='store_true',
                        help='fewer sizes/latencies so a developer can iterate')
    args = parser.parse_args()

    if args.quick:
        args.servers = '1,50'
        args.latency_ms = '300'
        args.seconds = 6.0
        args.wake_rounds = 3

    sizes = [int(x) for x in args.servers.split(',') if x.strip()]
    latencies = [int(x) for x in args.latency_ms.split(',') if x.strip()]
    modes = [m.strip() for m in args.modes.split(',') if m.strip()]

    ctx = app.app_context()
    ctx.push()
    db.create_all()
    rows = []
    wake_samples = []
    try:
        for mode in modes:
            for servers in sizes:
                for latency in latencies:
                    row = one_run(ctx, mode, servers, latency, args.seconds,
                                  args.workers, args.idle_seconds)
                    rows.append(row)
                    print('%-11s servers=%-4d latency=%-5dms hot p50=%-8s p95=%-8s '
                          'idle/min=%-7s max_inflight=%s'
                          % (mode, row['servers'], row['panel_latency_ms'],
                             row['hot_start_to_start_p50_ms'],
                             row['hot_start_to_start_p95_ms'],
                             row['idle_fetches_per_minute'], row['max_inflight']))
        wake_samples = measure_wake_latency(ctx, PanelSim(100), args.workers,
                                            args.wake_rounds, 100)
    finally:
        db.session.remove()
        db.drop_all()
        ctx.pop()

    wake = {
        'rounds': len(wake_samples),
        'p50_ms': _percentile(wake_samples, 50),
        'p95_ms': _percentile(wake_samples, 95),
        'max_ms': round(max(wake_samples), 3) if wake_samples else None,
    }
    print('\nwake -> dispatch: p50=%sms p95=%sms over %d rounds'
          % (wake['p50_ms'], wake['p95_ms'], wake['rounds']))

    payload = {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'workers': args.workers,
        'seconds_per_run': args.seconds,
        'idle_band_seconds': args.idle_seconds,
        'hot_servers': 1,
        'rows': rows,
        'wake': wake,
        'environment': {
            'python': sys.version.split()[0],
            'platform': sys.platform,
            'refresh_workers': panel_limits.refresh_worker_limit(),
            'panel_concurrency': panel_limits.concurrency_limit(),
        },
    }
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        print('wrote %s' % args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
