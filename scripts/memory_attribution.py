"""Attribute the host's RAM to the processes and services that are holding it.

This answers the question a bare number cannot: "3.45 GB of 3.78 GB is used - by what?".
It prints the host totals, Eve's processes by role, the host services that are not Eve
(Redis, PostgreSQL, nginx), the unclassified remainder, the compressed snapshot in Redis
and the bounded trend - and then reconciles them, residual included, instead of stopping
at "it was not Eve".

It is read-only and safe to run on a live install:

* no HTTP, no authentication, no session cookie, no database write;
* it never imports the application. Importing ``app`` runs the app's import-time side
  effects (including migrations) inside whatever process asked, which a read-only
  collector must not do - so everything comes from ``/proc``, Redis and the modules' own
  caches, via ``panel.core.memory_report.host_report()``;
* the Redis sections still need the service's environment (``REDIS_URL``). Run it as the
  service user with that environment, for example
  ``systemctl show -p Environment eve-manager-background`` or a sourced EnvironmentFile.

The in-process snapshot (client rows, duplication, ``raw_client``) is deliberately NOT
printed here. A process that does not run the app holds no snapshot, so reporting zeros
would imply an empty fleet. Read that half from the role that actually holds one:
``GET /api/system/memory`` inside the web/background/bot process, or Settings -> Overview.

Usage::

    python scripts/memory_attribution.py
    python scripts/memory_attribution.py --json > memory-before.json
    python scripts/memory_attribution.py --top 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from panel.core import memory_report  # noqa: E402  (never imports app; see the docstring)

GB = 1024 ** 3
MB = 1024 ** 2
KB = 1024

ROLE_ORDER = ('web', 'background', 'telegram-bot', 'telegram-egress', 'pulse', 'xray')
SERVICE_ORDER = ('redis', 'postgres', 'nginx')


def human_bytes(value) -> str:
    """Bytes as a short human string. ``None`` stays ``?`` rather than becoming ``0 B``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '?'
    if number < 0:
        return '-' + human_bytes(-number)
    for unit, scale in (('GB', GB), ('MB', MB), ('KB', KB)):
        if number >= scale:
            return '%.2f %s' % (number / scale, unit)
    return '%d B' % int(number)


def ordered(present, order):
    """Known keys in the canonical order first, then anything unknown, sorted."""
    keys = list(present or [])
    known = [key for key in order if key in keys]
    return known + sorted(key for key in keys if key not in order)


def _pid_cell(pids, shown=4):
    pids = list(pids or [])
    head = ','.join(str(pid) for pid in pids[:shown])
    if len(pids) > shown:
        head += ',+%d' % (len(pids) - shown)
    return head or '-'


def collect(*, top=5, now=None) -> dict:
    """The full payload: the host-wide report plus the largest unclassified processes."""
    data = memory_report.host_report(now=now)
    data['unclassified'] = memory_report.unclassified_processes(limit=top)
    data['collector'] = {
        'top': max(1, int(top)),
        'script': os.path.basename(__file__),
        'note': ('the in-process snapshot is not included: this process holds none. Read '
                 'snapshot.* from /api/system/memory inside the role that holds one.'),
    }
    return data


def _lines_processes(title, buckets, order):
    buckets = buckets or {}
    if not buckets:
        return ['  (none detected)']
    out = ['  %-16s %6s %10s %10s %10s %8s %10s  %s'
           % (title, 'PROCS', 'RSS', 'PSS', 'USS', 'THREADS', 'PEAK', 'PIDS')]
    for key in ordered(buckets, order):
        bucket = buckets[key] or {}
        out.append('  %-16s %6s %10s %10s %10s %8s %10s  %s'
                   % (key,
                      bucket.get('processes', '?'),
                      human_bytes(bucket.get('rss_bytes')),
                      human_bytes(bucket.get('pss_bytes')),
                      human_bytes(bucket.get('private_bytes')),
                      bucket.get('threads', '?'),
                      human_bytes(bucket.get('peak_rss_bytes')),
                      _pid_cell(bucket.get('pids'))))
    return out


def render(data) -> str:
    """The human report. Every figure is printed as measured, '-' where it is unknown."""
    host = data.get('host') or {}
    eve = data.get('eve') or {}
    services = eve.get('services') or {}
    acct = data.get('accounting') or {}
    redis = data.get('redis_snapshot') or {}
    trend = data.get('trend') or {}
    other = data.get('unclassified') or {}

    out = []
    out.append('EVE memory attribution')
    out.append('  sampled %s   pid %s   role %s'
               % (time.strftime('%Y-%m-%d %H:%M:%S',
                                time.localtime(data.get('sampled_at') or time.time())),
                  data.get('pid', '-'), data.get('process_role', '-')))
    out.append('=' * 78)

    out.append('HOST MEMORY')
    if host.get('available') is False:
        out.append('  unavailable: %s' % (host.get('reason') or 'unknown'))
    else:
        out.append('  %-22s %s' % ('Total', human_bytes(host.get('total_bytes'))))
        out.append('  %-22s %s  (%s%% of total)'
                   % ('Available', human_bytes(host.get('available_bytes')),
                      host.get('available_pct')))
        out.append('  %-22s %s   (total - MemAvailable)'
                   % ('Used', human_bytes(host.get('used_bytes'))))
        out.append('  %-22s %s   (reclaimable, not application usage)'
                   % ('Page cache', human_bytes(host.get('cache_bytes'))))
        out.append('  %-22s %s' % ('Free', human_bytes(host.get('free_bytes'))))
        out.append('  %-22s %s / %s'
                   % ('Swap used', human_bytes(host.get('swap_used_bytes')),
                      human_bytes(host.get('swap_total_bytes'))))
        pressure = host.get('pressure') or {}
        if pressure.get('available'):
            out.append('  %-22s some avg10 %s, full avg10 %s'
                       % ('PSI pressure', pressure.get('some_avg10'),
                          pressure.get('full_avg10')))
        else:
            out.append('  %-22s not reported by this kernel' % 'PSI pressure')
        out.append('  %-22s %s' % ('Health', host.get('health')))

    out.append('')
    out.append('WHERE THE USED MEMORY IS')
    if eve.get('available') is False:
        out.append('  %-22s unavailable: %s'
                   % ('EVE', eve.get('reason') or 'unknown'))
    else:
        out.append('  %-22s %s   (sum of PSS, not RSS)'
                   % ('EVE', human_bytes(eve.get('eve_pss_bytes'))))
        for key in ordered(eve.get('roles') or {}, ROLE_ORDER):
            bucket = (eve.get('roles') or {}).get(key) or {}
            out.append('    %-20s %s' % (key, human_bytes(bucket.get('pss_bytes'))))
        for key in ordered(services, SERVICE_ORDER):
            bucket = services.get(key) or {}
            out.append('  %-22s %s   (host service, not EVE)'
                       % (key, human_bytes(bucket.get('pss_bytes'))))
        if not services:
            out.append('  %-22s %s' % ('Host services', 'none detected'))
        count = eve.get('other_processes')
        out.append('  %-22s %s   (%s, unclassified)'
                   % ('Other processes', human_bytes(eve.get('other_pss_bytes')),
                      'count not measured' if count is None else '%s processes' % count))
    out.append('  %-22s %s   (reclaimable)' % ('Page cache', human_bytes(host.get('cache_bytes'))))
    out.append('  %-22s %s' % ('Free', human_bytes(host.get('free_bytes'))))
    out.append('  %-22s %s   (kernel, slab, page tables: no process owns it)'
               % ('Residual', human_bytes(acct.get('residual_bytes'))))
    total = host.get('total_bytes')
    if total and None not in (host.get('free_bytes'), host.get('cache_bytes'),
                              acct.get('residual_bytes')):
        out.append('  %s' % ('-' * 60))
        out.append('  %-22s %s of %s'
                   % ('Sum', human_bytes(int(host['free_bytes']) + int(host['cache_bytes'])
                                         + int(acct.get('process_pss_bytes') or 0)
                                         + int(acct['residual_bytes'])),
                      human_bytes(total)))
    else:
        out.append('  %-22s %s' % ('Sum', 'not reconciled (host total unknown)'))

    out.append('')
    out.append('EVE PROCESSES BY ROLE')
    out.extend(_lines_processes('role', eve.get('roles') or {}, ROLE_ORDER))
    out.append('')
    out.append('HOST SERVICES (not EVE)')
    out.extend(_lines_processes('service', services, SERVICE_ORDER))

    out.append('')
    out.append('REDIS (the published snapshot)')
    if redis.get('available') is False:
        out.append('  unavailable: %s' % (redis.get('reason') or 'unknown'))
    else:
        out.append('  %-22s %s across %s server keys (largest %s)'
                   % ('Compressed', human_bytes(redis.get('total_bytes')),
                      redis.get('server_keys'), human_bytes(redis.get('largest_block_bytes'))))
        out.append('  %-22s %s (version %s)'
                   % ('Manifest', human_bytes(redis.get('manifest_bytes')),
                      redis.get('last_update')))

    out.append('')
    out.append('TREND (bounded ring, one sample a minute)')
    if trend.get('available') is False:
        out.append('  unavailable: %s' % (trend.get('reason') or 'unknown'))
    elif not trend.get('samples'):
        out.append('  %s' % (trend.get('note') or 'no samples yet'))
    else:
        out.append('  %-22s %s' % ('Current', human_bytes(trend.get('current_bytes'))))
        out.append('  %-22s %s' % ('Peak', human_bytes(trend.get('peak_bytes'))))
        out.append('  %-22s %s' % ('Change in window', human_bytes(trend.get('delta_bytes'))))
        out.append('  %-22s %s/hour' % ('Per hour', human_bytes(trend.get('per_hour_bytes'))))
        out.append('  %-22s %s' % ('Trend', trend.get('trend')))
        out.append('  %-22s %s of %s in the last %s min'
                   % ('Samples', trend.get('samples'), trend.get('max_samples'),
                      trend.get('window_minutes')))

    out.append('')
    out.append('TOP UNCLASSIFIED PROCESSES (command basename only; arguments are never read)')
    if other.get('available') is False:
        out.append('  unavailable: %s' % (other.get('reason') or 'unknown'))
    elif not other.get('processes'):
        out.append('  (none)')
    else:
        out.append('  %10s %10s %8s %8s  %s' % ('PSS', 'RSS', 'THREADS', 'PID', 'COMMAND'))
        for row in other['processes']:
            out.append('  %10s %10s %8s %8s  %s'
                       % (human_bytes(row.get('pss_bytes')), human_bytes(row.get('rss_bytes')),
                          row.get('threads'), row.get('pid'), row.get('command')))
        if other.get('truncated'):
            out.append('  (%s unclassified in total; raise --top to see more)'
                       % other.get('count'))

    out.append('')
    out.append('SNAPSHOT COPIES (who holds the shared snapshot)')
    copies = data.get('snapshot_copies') or {}
    if copies.get('available') is False:
        out.append('  unavailable: %s' % (copies.get('reason') or 'unknown'))
    elif not copies.get('copies'):
        out.append('  none recorded yet (no process has adopted a new snapshot version,')
        out.append('  or Redis was restarted and the records expired)')
    else:
        out.append('  %-22s %s   (rows summed over copies, not distinct clients)'
                   % ('Copies', copies.get('copies')))
        out.append('  %-22s %s rows' % ('Largest copy',
                                        copies.get('largest_client_rows')))
        for key in sorted(copies.get('roles') or {}):
            record = (copies.get('roles') or {}).get(key) or {}
            out.append('    %-20s %s rows, %s inbounds, pid %s, %ss old'
                       % (key, record.get('client_rows'), record.get('inbounds'),
                          record.get('pid'), record.get('age_seconds')))
        if copies.get('expired_roles'):
            out.append('  expired (stopped processes): %s'
                       % ', '.join(copies['expired_roles']))
        out.append('  Records are written when a process adopts a new version and expire')
        out.append('  after %s s, so a stopped process stops counting as a copy.'
                   % copies.get('ttl_seconds'))

    out.append('')
    out.append('IN-PROCESS SNAPSHOT')
    out.append('  Not readable from this process: it does not run the app, so it holds no')
    out.append('  snapshot and printing zeros would imply an empty fleet. Read snapshot.*')
    out.append('  from the role that holds one: GET /api/system/memory, or Settings ->')
    out.append('  Overview -> Memory.')
    out.append('=' * 78)
    out.append('Read-only: no HTTP, no authentication, no database write, and the app is')
    out.append('never imported (its import-time migrations would run in this process).')
    return '\n'.join(out)


def main():
    parser = argparse.ArgumentParser(
        description='Attribute host RAM to the processes and services holding it.')
    parser.add_argument('--json', action='store_true',
                        help='print the raw payload (same shape as GET /api/system/memory, '
                             'minus the in-process snapshot)')
    parser.add_argument('--top', type=int, default=5,
                        help='how many unclassified processes to list (default 5)')
    args = parser.parse_args()

    data = collect(top=args.top)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    print(render(data))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
