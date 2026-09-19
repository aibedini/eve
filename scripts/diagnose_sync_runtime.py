"""One-shot runtime diagnosis for the per-server sync pipeline.

Why this exists: the dashboard can now say "Sync issue" about a panel that is reachable
and watched, and the fields that explain it live in the FETCHER process (its scheduler
state) and in Redis (the published report the web process reads). Neither is visible from
a browser, and the two are easy to confuse:

    Active          = the panel answered a status probe        (connectivity)
    Sync issue      = the DATA on screen is not verified fresh (freshness)
    Sync issue + HOT + "Next poll: 0s" + no "Panel sync" row
                    = a scheduling report that stopped updating, or a fetcher that has
                      never completed a successful read of that panel

Run it ON the machine that runs the panel (the process that owns the fetch loop):

    .venv/Scripts/python.exe scripts/diagnose_sync_runtime.py --server-id 12
    .venv/Scripts/python.exe scripts/diagnose_sync_runtime.py --json sync-diag.json

It reads only. Nothing here mutates the schedule, the snapshot or the panels.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')

LOG_GLOBS = (
    os.path.join(REPO_ROOT, 'instance', '*.log'),
    os.path.join(REPO_ROOT, 'logs', '*.log'),
    os.path.join(REPO_ROOT, '*.log'),
    '/var/log/eve*.log',
    '/var/log/eve/*.log',
)


def _threads_of_interest():
    interesting = []
    for thread in threading.enumerate():
        name = thread.name or ''
        if name.startswith('eve-') or 'fetch' in name.lower() or 'worker' in name.lower():
            interesting.append({'name': name, 'alive': thread.is_alive(),
                                'daemon': bool(thread.daemon)})
    return interesting


def _redis_state():
    try:
        from panel.core import redis_client
        client = redis_client.get_redis()
        if client is None:
            return {'enabled': False, 'reachable': False,
                    'url': getattr(redis_client, 'REDIS_URL', None)}
        try:
            client.ping()
            reachable = True
            error = None
        except Exception as exc:
            reachable, error = False, str(exc)[:200]
        return {'enabled': True, 'reachable': reachable,
                'url': getattr(redis_client, 'REDIS_URL', None), 'error': error}
    except Exception as exc:
        return {'enabled': False, 'reachable': False, 'error': str(exc)[:200]}


def _log_tail_for(server_id, limit=40):
    """Recent sync lines that name this server, from whatever log files exist locally."""
    if server_id is None:
        return []
    needle = 'server_id=%s' % server_id
    lines = []
    for pattern in LOG_GLOBS:
        for path in glob.glob(pattern):
            try:
                with open(path, encoding='utf-8', errors='replace') as handle:
                    chunk = handle.readlines()[-40000:]
            except OSError:
                continue
            for line in chunk:
                if needle in line and ('eve.sync' in line or 'sync.' in line
                                       or 'fetch' in line or 'scheduler' in line):
                    lines.append({'file': path, 'line': line.rstrip()[:400]})
    return lines[-limit:]


def collect(server_id=None, *, log_tail=True):
    from panel.core import refresh_policy
    from panel.jobs import schedulers

    now = time.time()
    report = {
        'collected_at': now,
        'process': {
            'pid': os.getpid(),
            'role': (os.environ.get('EVE_PROCESS_ROLE') or 'combined').strip().lower(),
            'threads': _threads_of_interest(),
            'workers': schedulers.worker_inventory(),
        },
        'redis': _redis_state(),
        'wake_listener': refresh_policy.wake_listener_active(),
        'scheduler': schedulers.scheduler_metrics(),
        'policy_summary': refresh_policy.sync_summary(now=now),
        'shared_backend': refresh_policy.server_watch_marks(now=now).get('shared_backend'),
    }
    # What the FETCHER knows (this process's memory) and what a WEB process would see
    # (the published reports). A mismatch between the two is the "report not published"
    # failure path; presence in neither is "no fetcher is running".
    local_ids = sorted(refresh_policy.server_states(now=now).keys(), key=int)
    shared = refresh_policy.shared_server_sync(now=now, force=True)
    report['servers'] = {
        'tracked_locally': local_ids,
        'published_rows': sorted(shared.keys(), key=int),
    }
    if server_id is not None:
        report['server'] = refresh_policy.server_sync_state(server_id, now=now)
        report['server_expected_by_web'] = refresh_policy.server_sync_report(
            [server_id], now=now).get(str(server_id))
    else:
        report['all_local_rows'] = refresh_policy.server_sync_report(now=now)
    if log_tail:
        report['log_tail'] = _log_tail_for(server_id)
    return report


def _verdict(report):
    """The one line an operator needs: is the fetcher alive and completing reads?"""
    metrics = report.get('scheduler') or {}
    workers = report.get('process', {}).get('workers') or {}
    fetcher = (workers.get('workers') or {}).get('data_fetcher') or {}
    dispatched = int(metrics.get('dispatched') or 0)
    completed = int(metrics.get('completed') or 0)
    errors = int(metrics.get('errors') or 0)
    tick_errors = int(metrics.get('tick_errors') or 0)
    notes = []
    if fetcher.get('state') != 'running':
        notes.append('the data_fetcher worker is NOT running in this process '
                     '(state=%s); check PROCESS_ROLE and the singleton lock, and run this '
                     'on the process that owns the fetch loop' % fetcher.get('state'))
    if tick_errors:
        notes.append('%d scheduler tick(s) failed (consecutive=%s): the loop is backing '
                     'off, so nothing is being fetched'
                     % (tick_errors, metrics.get('consecutive_tick_errors')))
    if dispatched == 0:
        notes.append('the scheduler has dispatched nothing: it either never started '
                     '(a blocked bootstrap) or found no due panel')
    if completed > 0 and errors >= completed:
        notes.append('every read is failing: this is a panel/network/auth problem, not a '
                     'scheduling one (see last_error)')
    if report.get('redis', {}).get('enabled') and not report['redis'].get('reachable'):
        notes.append('Redis is configured but unreachable: watch marks and reports cannot '
                     'cross processes, so the dashboard cannot know about the fetcher')
    return notes or ['no obvious scheduling fault: compare the per-server fields above']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-id', type=int, default=None,
                        help='the panel the dashboard is complaining about')
    parser.add_argument('--json', default=None, help='write the raw report here')
    parser.add_argument('--no-logs', action='store_true', help='skip log correlation')
    args = parser.parse_args()

    from app import app  # deferred: an application context is what the policy needs

    with app.app_context():
        report = collect(args.server_id, log_tail=not args.no_logs)
    report['verdict'] = _verdict(report)

    print('== process ==')
    print('  pid=%s role=%s' % (report['process']['pid'], report['process']['role']))
    for row in report['process']['threads']:
        print('  thread %-24s alive=%s daemon=%s' % (row['name'], row['alive'], row['daemon']))
    for name, info in (report['process']['workers'].get('workers') or {}).items():
        print('  worker %-24s state=%-8s singleton=%s' % (
            name, info.get('state'), info.get('singleton')))
    print('  singletons_owned=%s' % report['process']['workers'].get('singletons_owned'))
    print('== redis ==')
    print('  %s' % report['redis'])
    print('  wake_listener=%s shared_backend=%s' % (report['wake_listener'],
                                                    report['shared_backend']))
    print('== scheduler ==')
    for key in ('workers', 'dispatched', 'completed', 'errors', 'capacity_rejections',
                'saturation_events', 'max_inflight', 'queue_delay_ms_max', 'wake_consumed',
                'tick_errors', 'consecutive_tick_errors'):
        print('  %-24s %s' % (key, report['scheduler'].get(key)))
    print('== coverage ==')
    print('  tracked locally : %d %s' % (len(report['servers']['tracked_locally']),
                                         report['servers']['tracked_locally'][:20]))
    print('  published rows  : %d %s' % (len(report['servers']['published_rows']),
                                         report['servers']['published_rows'][:20]))
    if 'server' in report:
        print('== server %s ==' % args.server_id)
        for key in sorted((report['server'] or {}).keys()):
            print('  %-26s %s' % (key, report['server'][key]))
        print('== as the web process sees it ==')
        for key in sorted((report['server_expected_by_web'] or {}).keys()):
            print('  %-26s %s' % (key, report['server_expected_by_web'][key]))
    if report.get('log_tail'):
        print('== log tail (sync lines for this server) ==')
        for row in report['log_tail'][-20:]:
            print('  %s' % row['line'])
    print('== verdict ==')
    for note in report['verdict']:
        print('  - %s' % note)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, sort_keys=True, default=str)
        print('wrote %s' % args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
