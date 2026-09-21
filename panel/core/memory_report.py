"""Memory attribution: where the host's RAM went, and where Eve's RAM went.

The question this answers is "3.45 GB of 3.78 GB is used - by what?", and it answers it
without guessing:

* host totals and pressure from ``/proc/meminfo`` and ``/proc/pressure/memory``;
* per-process RSS / **PSS** / USS (private), threads, peak and uptime from
  ``/proc/<pid>/smaps_rollup`` and ``/proc/<pid>/status``;
* Eve's own processes grouped by ROLE (web, background, telegram bot, telegram egress,
  pulse, managed xray) so "Eve is using N GB" is a sum of PSS, not of double-counted RSS;
* the host services that are not Eve (Redis, PostgreSQL, nginx) and the unclassified
  remainder, so the used RAM is reconciled against the processes on the box instead of
  stopping at "it was not Eve";
* what the in-process snapshot actually holds (servers, inbounds, client rows, unique
  clients, duplicated rows, rows still carrying ``raw_client``, formatted-string rows) -
  the internal duplication the architecture pays for;
* the compressed snapshot size in Redis, and the sizes of the caches that live outside
  the snapshot;
* a bounded trend so "stable at 950 MB" can be told apart from "growing without bound".

Rules this module follows:

* **PSS is the aggregate metric.** Summing RSS across processes counts shared pages (the
  Python interpreter, libc, loaded wheels) once per process, which turns a normal
  multi-process install into a fake memory problem.
* **Page cache is not application memory.** It is reported separately, and ``used`` is
  computed as ``total - available`` so reclaimable cache is not presented as pressure.
* **No credentials, no commands, no environment values, no customer data.** Only counts,
  byte sizes, pids, roles and ages cross this boundary; a unit test asserts it.
* **Nothing here walks the whole Python object graph.** Counts are O(servers +
  inbounds + clients) integer work; a real deep-size sample is a separate, explicit,
  admin-only action (``analyze_python_memory``) with a timeout.
* Linux-first with a clean fallback: on a platform without ``/proc`` every section says
  ``available: False`` with a reason instead of inventing numbers.
"""
from __future__ import annotations

import json
import os
import threading
import time

PAGE = 4096
PROC = '/proc'
CLOCK_TICKS = 100

#: Role markers found on Eve's process command lines. Matching is on a substring of
#: ``/proc/<pid>/cmdline``, which is the one thing every install has in common (Docker
#: and systemd both launch these same entry points).
ROLE_MARKERS = (
    ('telegram-bot', 'telegram_bot_worker.py'),
    ('telegram-egress', 'telegram_egress_worker.py'),
    ('pulse', 'pulse_runner.py'),
    ('pulse', 'pulse_agent.py'),
    ('background', 'background_worker.py'),
    ('web', 'app:app'),
    ('web', 'gunicorn'),
)
XRAY_MARKERS = ('/xray', 'xray-linux', 'xray run')

#: Host services that run on the same box but are NOT Eve. They are reported separately
#: and excluded from Eve's total, because folding PostgreSQL into "Eve is using 2 GB"
#: would be the exact attribution error this module exists to avoid.
SERVICE_MARKERS = (
    ('redis', 'redis-server'),
    ('postgres', 'postgres'),
    ('nginx', 'nginx'),
)
SERVICE_ROLES = tuple(role for role, _marker in SERVICE_MARKERS)
#: Roles that belong to the host rather than to Eve, so they are outside ``eve_pss_bytes``.
#: Managed Xray stays listed under ``roles`` (Eve spawns it) but is still reported outside
#: the Eve total, exactly as before. ``other`` is listed here as well so that an
#: unclassified process can never be attributed to Eve through the ``include_other`` path.
NON_EVE_ROLES = ('xray', 'other') + SERVICE_ROLES

#: A sample is compact and lives in Redis, trimmed to a hard cap, so the history itself
#: can never become the memory problem.
SAMPLE_KEY = 'eve:memory:samples'
SAMPLE_INTERVAL_SECONDS = 60.0
SAMPLE_MAX = 1440          # 24 h at one sample a minute
SAMPLE_KEEP_MINUTES = 60   # the window the overview renders by default
#: A "sustained growth" reading needs a long enough window and a process old enough that
#: warm-up is over; below these, growth is only an observation, never a conclusion.
SUSTAINED_WINDOW_MINUTES = 360
SUSTAINED_UPTIME_SECONDS = 6 * 3600

#: What a caller may ask the trend for. The ring holds one sample a minute up to
#: ``SAMPLE_MAX``, so a longer request cannot be answered - and answering it with a short
#: history would be worse than refusing, which is why the clamp is to the ring itself.
TREND_WINDOW_MIN = 5
TREND_WINDOW_MAX = int(SAMPLE_MAX * SAMPLE_INTERVAL_SECONDS / 60)   # 1440 minutes = 24 h


def clamp_trend_minutes(value, *, default=SAMPLE_KEEP_MINUTES) -> int:
    """A requested trend window, clamped to what the ring can actually answer.

    The payload echoes the window it used (``trend.window_minutes``), so a caller that asks
    for a day and gets an hour is told so rather than handed a mislabelled series.
    """
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(TREND_WINDOW_MIN, min(TREND_WINDOW_MAX, minutes))

_lock = threading.Lock()
_last_sample_at = 0.0
#: The last version *this process* recorded a copy for, per role: the throttle is keyed by
#: role because the record is, so a process that reports under two roles still records both.
_last_copy_versions = {}

#: One small record per process that has adopted the shared snapshot, so a single endpoint
#: call can answer "how many full copies exist" instead of one call per role. The roles are
#: the fixed set the installer's units write (setup.sh, docker-compose.yml) plus the
#: single-process ``combined`` case, so reading them is one GET each - never a keyspace
#: SCAN, which would grow with everything else Redis holds.
COPY_KEY_PREFIX = 'eve:memory:copy:'
COPY_ROLES = ('web', 'background', 'telegram-bot', 'telegram-egress', 'pulse', 'combined')
COPY_TTL_SECONDS = 600


def _read(path):
    try:
        with open(path, 'rb') as handle:
            return handle.read().decode('utf-8', 'replace')
    except OSError:
        return None


def _kb(value):
    try:
        return int(str(value).strip().split()[0]) * 1024
    except (TypeError, ValueError, IndexError):
        return None


def _meminfo():
    text = _read(PROC + '/meminfo')
    if text is None:
        return None
    fields = {}
    for line in text.splitlines():
        if ':' not in line:
            continue
        key, _, rest = line.partition(':')
        fields[key.strip()] = rest.strip()
    return fields


def host_memory(now=None) -> dict:
    """Host memory, with cache counted as reclaimable instead of as usage."""
    moment = time.time() if now is None else float(now)
    info = _meminfo()
    if not info:
        return {'available': False,
                'reason': 'no /proc/meminfo (not a Linux host, or /proc is not mounted)'}
    total = _kb(info.get('MemTotal'))
    available = _kb(info.get('MemAvailable'))
    free = _kb(info.get('MemFree'))
    cache = sum(value for value in (
        _kb(info.get('Cached')), _kb(info.get('SReclaimable')), _kb(info.get('Buffers')))
        if value)
    swap_total = _kb(info.get('SwapTotal')) or 0
    swap_free = _kb(info.get('SwapFree')) or 0
    used = None if (total is None or available is None) else max(0, total - available)
    pressure = _memory_pressure()
    available_pct = (round(100.0 * available / total, 1)
                     if total and available is not None else None)
    if available_pct is None:
        health = 'unknown'
    elif available_pct < 10 or (pressure.get('some_avg10') or 0) >= 25:
        health = 'critical'
    elif available_pct < 20 or (pressure.get('some_avg10') or 0) >= 10:
        health = 'warning'
    else:
        health = 'ok'
    return {
        'available': True,
        'total_bytes': total,
        'available_bytes': available,
        'free_bytes': free,
        'used_bytes': used,
        'cache_bytes': cache,
        'swap_total_bytes': swap_total,
        'swap_used_bytes': max(0, swap_total - swap_free),
        'available_pct': available_pct,
        'pressure': pressure,
        'health': health,
        'sampled_at': moment,
        'note': ('used = total - MemAvailable, so reclaimable page cache is not reported '
                 'as application pressure; cache_bytes is shown separately'),
    }


def _memory_pressure() -> dict:
    text = _read(PROC + '/pressure/memory')
    if not text:
        return {'available': False}
    out = {'available': True, 'some_avg10': None, 'some_avg60': None, 'full_avg10': None}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        values = {}
        for token in parts[1:]:
            key, _, value = token.partition('=')
            values[key] = value
        if kind == 'some':
            out['some_avg10'] = float(values.get('avg10', 0) or 0)
            out['some_avg60'] = float(values.get('avg60', 0) or 0)
        elif kind == 'full':
            out['full_avg10'] = float(values.get('avg10', 0) or 0)
    return out


def process_memory(pid) -> dict:
    """RSS / PSS / USS for one process, from smaps_rollup with a status fallback."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {'available': False, 'reason': 'bad pid'}
    status = _read('%s/%d/status' % (PROC, pid))
    if status is None:
        return {'available': False, 'pid': pid, 'reason': 'process is gone'}
    row = {'available': True, 'pid': pid}
    for line in status.splitlines():
        key, _, rest = line.partition(':')
        key = key.strip()
        if key == 'VmRSS':
            row['rss_bytes'] = _kb(rest)
        elif key == 'VmHWM':
            row['peak_rss_bytes'] = _kb(rest)
        elif key == 'VmSwap':
            row['swap_bytes'] = _kb(rest)
        elif key == 'Threads':
            try:
                row['threads'] = int(rest.strip())
            except ValueError:
                pass
    rollup = _read('%s/%d/smaps_rollup' % (PROC, pid))
    fields = {}
    if rollup:
        for line in rollup.splitlines():
            key, _, rest = line.partition(':')
            if rest.strip():
                fields[key.strip()] = _kb(rest)
    if fields:
        row['pss_available'] = True
        row['pss_bytes'] = fields.get('Pss')
        row['pss_anon_bytes'] = fields.get('Pss_Anon')
        row['pss_file_bytes'] = fields.get('Pss_File')
        row['pss_shmem_bytes'] = fields.get('Pss_Shmem')
        private = (fields.get('Private_Clean') or 0) + (fields.get('Private_Dirty') or 0)
        # USS is the process's own memory: private pages plus anonymous memory it owns.
        # Reported as "private" because that is what the kernel gives us here.
        row['private_bytes'] = private or fields.get('Anonymous')
    else:
        # PSS is a proportional share of shared pages and cannot be derived from RSS: RSS
        # counts every shared page once per process, so a sum of RSS is not a sum of PSS.
        # The reading is therefore *unavailable*, and the RSS value is kept under its own
        # name. Substituting RSS for PSS here is what made PostgreSQL's 24 backends read as
        # 1.6 GB of "PSS" (against a real ~220 MB) and drove the host residual to -1.4 GB.
        row['pss_available'] = False
        row['pss_bytes'] = None
        row['pss_is_rss_fallback'] = True
        row['approximate_rss_bytes'] = row.get('rss_bytes')
        row['pss_reason'] = 'smaps_rollup unavailable (permission or kernel); PSS is unknown'
    row['role'] = _role_for(pid)
    row['command'] = _command_for(pid)
    row['uptime_seconds'] = _uptime_for(pid)
    return row


def _cmdline(pid) -> str:
    raw = _read('%s/%d/cmdline' % (PROC, pid))
    if raw is None:
        return ''
    return raw.replace('\x00', ' ').strip()


def _role_for(pid) -> str:
    line = _cmdline(pid).lower()
    if not line:
        return 'other'
    for role, marker in ROLE_MARKERS:
        if marker in line:
            return role
    for marker in XRAY_MARKERS:
        if marker in line:
            return 'xray'
    for role, marker in SERVICE_MARKERS:
        if marker in line:
            return role
    return 'other'


def _command_for(pid) -> str:
    """The executable basename only: an argv can carry a token or a credential."""
    line = _cmdline(pid)
    if not line:
        return ''
    first = line.split(' ')[0]
    return os.path.basename(first)[:64]


def _uptime_for(pid):
    stat = _read('%s/%d/stat' % (PROC, pid))
    if not stat:
        return None
    try:
        tail = stat.rsplit(')', 1)[1].split()
        start_ticks = int(tail[19])
    except (IndexError, ValueError):
        return None
    btime = None
    for line in (_read(PROC + '/stat') or '').splitlines():
        if line.startswith('btime'):
            try:
                btime = int(line.split()[1])
            except (IndexError, ValueError):
                btime = None
    if btime is None:
        return None
    return max(0.0, round(time.time() - (btime + start_ticks / float(CLOCK_TICKS)), 1))


def _has_real_pss(row) -> bool:
    """True when a process row carries a PSS that was really read.

    A row that declares ``pss_available`` False is never a PSS, whatever else it carries.
    A row that does not declare it is treated as measured when it carries a value: the only
    producer is :func:`process_memory`, which always declares, and this keeps a hand-built
    row (a test fixture, a future adapter) from turning an RSS into a PSS.
    """
    if row.get('pss_available') is False:
        return False
    return row.get('pss_bytes') is not None


def eve_processes(*, include_other=False) -> dict:
    """Eve's processes grouped by role, aggregated on PSS.

    Host services (Redis, PostgreSQL, nginx) are collected under ``services`` rather than
    folded into ``roles``: they are not Eve, so counting them in Eve's total would make
    the headline number a lie. Managed Xray stays in ``roles`` because Eve spawns it, and
    remains outside the Eve total as before. Unclassified processes are only counted by
    default; their PSS is still summed into ``other_pss_bytes`` so the host can be
    reconciled without listing them (see :func:`unclassified_processes`).
    """
    if not os.path.isdir(PROC):
        # The same keys as the measured branch, with None instead of invented zeros: two
        # consumers (Settings -> Overview and scripts/memory_attribution.py) read this
        # payload, and a shape that changes with the platform makes both of them lie in a
        # different way. A count of None says "not measured", a count of 0 says "none".
        return {'available': False, 'reason': 'no /proc', 'roles': {}, 'services': {},
                'other_processes': None, 'other_rss_bytes': None, 'other_pss_bytes': None,
                'other_pss_complete': False, 'other_pss_unavailable_processes': None,
                'other_private_bytes': None, 'other_threads': None,
                'eve_pss_bytes': None, 'eve_pss_complete': False,
                'eve_pss_with_xray_bytes': None, 'eve_pss_with_xray_complete': False,
                'service_pss_bytes': None, 'service_pss_complete': False}
    roles = {}
    services = {}
    other = {'processes': 0, 'rss_bytes': 0, 'pss_bytes': 0, 'private_bytes': 0,
             'threads': 0, 'pss_available_processes': 0, 'pss_unavailable_processes': 0,
             'pss_complete': True}
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        row = process_memory(name)
        if not row.get('available'):
            continue
        role = row.get('role') or 'other'
        if role == 'other' and not include_other:
            other['processes'] += 1
            for key in ('rss_bytes', 'private_bytes'):
                other[key] += int(row.get(key) or 0)
            other['threads'] += int(row.get('threads') or 0)
            if _has_real_pss(row):
                other['pss_bytes'] += int(row.get('pss_bytes') or 0)
                other['pss_available_processes'] += 1
            else:
                other['pss_unavailable_processes'] += 1
                other['pss_complete'] = False
            continue
        bucket = (services if role in SERVICE_ROLES else roles).setdefault(role, {
            'processes': 0, 'rss_bytes': 0, 'pss_bytes': 0, 'private_bytes': 0,
            'threads': 0, 'pids': [], 'max_uptime_seconds': 0.0,
            'peak_rss_bytes': 0, 'pss_approximated': False,
            'pss_available_processes': 0, 'pss_unavailable_processes': 0,
            'pss_complete': True,
        })
        bucket['processes'] += 1
        bucket['pids'].append(row['pid'])
        for key in ('rss_bytes', 'private_bytes', 'peak_rss_bytes'):
            bucket[key] += int(row.get(key) or 0)
        bucket['threads'] += int(row.get('threads') or 0)
        bucket['max_uptime_seconds'] = max(bucket['max_uptime_seconds'],
                                           float(row.get('uptime_seconds') or 0))
        if _has_real_pss(row):
            bucket['pss_bytes'] += int(row.get('pss_bytes') or 0)
            bucket['pss_available_processes'] += 1
        else:
            # Deliberately neither zero nor RSS. The bucket's pss_bytes stays a partial sum
            # of what was readable, and pss_complete says so, so nothing downstream can add
            # an RSS to a PSS and call the result a reconciliation.
            bucket['pss_approximated'] = True
            bucket['pss_unavailable_processes'] += 1
            bucket['pss_complete'] = False
    eve_pss = sum(bucket['pss_bytes'] for role, bucket in roles.items()
                  if role not in NON_EVE_ROLES)
    eve_complete = all(bucket.get('pss_complete', False)
                       for role, bucket in roles.items() if role not in NON_EVE_ROLES)
    xray = roles.get('xray') or {}
    eve_pss_with_xray = eve_pss + int(xray.get('pss_bytes') or 0)
    return {
        'available': True,
        'roles': roles,
        'services': services,
        'other_processes': other['processes'],
        'other_rss_bytes': other['rss_bytes'],
        'other_pss_bytes': other['pss_bytes'],
        'other_pss_complete': other['pss_complete'],
        'other_pss_unavailable_processes': other['pss_unavailable_processes'],
        'other_private_bytes': other['private_bytes'],
        'other_threads': other['threads'],
        'eve_pss_bytes': eve_pss,
        'eve_pss_complete': eve_complete,
        'eve_pss_with_xray_bytes': eve_pss_with_xray,
        'eve_pss_with_xray_complete': eve_complete and bool(xray.get('pss_complete', False)),
        'service_pss_bytes': sum(bucket['pss_bytes'] for bucket in services.values()),
        'service_pss_complete': all(bucket.get('pss_complete', False)
                                    for bucket in services.values()),
        'note': ('EVE total is a sum of PSS, which counts shared pages once across the '
                 'processes that map them; summing RSS would double-count the interpreter '
                 'and the loaded libraries. Host services and unclassified processes are '
                 'reported separately and are not part of the EVE figure. A process whose '
                 'smaps_rollup could not be read contributes nothing to these sums and '
                 'marks its group incomplete (see pss_complete).'),
    }


def unclassified_processes(*, limit=10) -> dict:
    """The largest processes that matched no Eve role and no known host service.

    This is the "who else is on the box" list, so a row carries the executable basename
    only - never argv, which is where a token or a connection string would sit - and no
    command-line arguments, environment or customer data.
    """
    if not os.path.isdir(PROC):
        return {'available': False, 'reason': 'no /proc', 'processes': []}
    rows = []
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        row = process_memory(name)
        if not row.get('available') or (row.get('role') or 'other') != 'other':
            continue
        rows.append({'pid': row['pid'], 'command': row.get('command') or '',
                     'rss_bytes': row.get('rss_bytes'), 'pss_bytes': row.get('pss_bytes'),
                     'private_bytes': row.get('private_bytes'),
                     'threads': row.get('threads')})
    rows.sort(key=lambda item: -int(item.get('pss_bytes') or 0))
    limit = max(1, int(limit))
    return {'available': True, 'processes': rows[:limit], 'count': len(rows),
            'truncated': len(rows) > limit,
            'note': 'command is the executable basename; arguments are deliberately not read'}


def accounting(host, eve) -> dict:
    """Reconcile total RAM against the PSS attributed to processes, residual included.

    ``residual_bytes`` is reported rather than absorbed: kernel, slab, page tables and
    driver memory are real, belong to no process, and a breakdown that quietly buried them
    would be claiming an accuracy it does not have.

    A residual is only computed when every additive part is a real PSS and the host numbers
    are present. If any group's PSS is incomplete (a process whose ``smaps_rollup`` could
    not be read), the reconciliation is marked incomplete and the residual is None: mixing
    an RSS into a PSS sum is how a host ends up reporting an impossible -1.4 GB residual.
    """
    total = host.get('total_bytes')
    if not total:
        # The same keys as the measured branch, with None for "not measured", for the same
        # reason as eve_processes(): two consumers read this payload.
        return {'available': False, 'reason': 'host total unknown', 'total_bytes': None,
                'free_bytes': None, 'cache_bytes': None, 'process_pss_bytes': None,
                'eve_pss_bytes': None, 'xray_pss_bytes': None, 'service_pss_bytes': None,
                'other_pss_bytes': None, 'residual_bytes': None, 'used_bytes': None,
                'available_bytes': None, 'complete': False, 'unreconciled': True,
                'incomplete_groups': ['host']}
    free = host.get('free_bytes')
    cache = host.get('cache_bytes')
    process_pss = sum(int(eve.get(key) or 0) for key in
                      ('eve_pss_with_xray_bytes', 'service_pss_bytes', 'other_pss_bytes'))
    incomplete_groups = []
    if not eve.get('eve_pss_with_xray_complete'):
        incomplete_groups.append('eve')
    if not eve.get('service_pss_complete'):
        incomplete_groups.append('host services')
    if not eve.get('other_pss_complete'):
        incomplete_groups.append('unclassified processes')
    if free is None or cache is None:
        incomplete_groups.append('host availability')
    complete = not incomplete_groups
    return {
        'available': True,
        'total_bytes': total,
        'free_bytes': free,
        'cache_bytes': cache,
        'process_pss_bytes': process_pss,
        'eve_pss_bytes': int(eve.get('eve_pss_bytes') or 0),
        'xray_pss_bytes': int((eve.get('roles') or {}).get('xray', {}).get('pss_bytes') or 0),
        'service_pss_bytes': int(eve.get('service_pss_bytes') or 0),
        'other_pss_bytes': int(eve.get('other_pss_bytes') or 0),
        'residual_bytes': (total - free - cache - process_pss) if complete else None,
        'complete': complete,
        'unreconciled': not complete,
        'incomplete_groups': incomplete_groups,
        'reason': (None if complete else
                   'PSS is unavailable for: %s. The residual is not computed, because a '
                   'partial PSS sum cannot be reconciled against total RAM.'
                   % ', '.join(incomplete_groups)),
        'used_bytes': host.get('used_bytes'),
        'available_bytes': host.get('available_bytes'),
        'note': ('free + page cache + the PSS of every process = total; the residual is '
                 'kernel, slab, page tables and driver memory, which belongs to no '
                 'process. used (total - MemAvailable) is the smaller number shown as '
                 'usage, because the page cache is reclaimable.'),
    }


def snapshot_footprint(snapshot=None) -> dict:
    """What the in-process snapshot holds, without deep-walking the object graph.

    Counting is O(inbounds + clients) integer work and allocates nothing that outlives
    the call, so it is safe to run when the overview is opened. It deliberately does NOT
    measure the true retained size of nested objects: that is what the admin-only deep
    analysis is for.

    The vocabulary is schema v2's. A **membership** is one client appearing on one inbound;
    a **canonical entity** is the single retained client dict those memberships share, which
    is what ``hydrate_server_block()`` hands to every membership that has no override.
    Calling memberships "duplicate client rows" would describe the pre-v2 shape, where each
    mirrored inbound really did hold its own copy. Distinct dicts are counted by ``id()``
    while scanning and no reference is kept.
    """
    if snapshot is None:
        try:
            from app import GLOBAL_SERVER_DATA  # deferred: app-level state
            snapshot = GLOBAL_SERVER_DATA
        except Exception as exc:
            return {'available': False, 'reason': 'snapshot unavailable: %s' % str(exc)[:120]}
    inbounds = snapshot.get('inbounds') or []
    memberships = 0
    raw_memberships = 0
    formatted_memberships = 0
    canonical = {}          # client key -> id() of the canonical dict (ids only)
    client_objects = set()  # id() only: no references are retained
    raw_objects = set()
    shared_memberships = 0
    overrides = 0
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            continue
        for client in (inbound.get('clients') or []):
            if not isinstance(client, dict):
                continue
            memberships += 1
            uid = client.get('id') or client.get('uuid')
            if not uid:
                uid = '%s|%s' % (inbound.get('server_id'), client.get('email'))
            key = str(uid)
            identity = id(client)
            first = canonical.get(key)
            if first is None:
                canonical[key] = identity
                shared_memberships += 1
            elif first == identity:
                shared_memberships += 1
            else:
                # The membership carries its own dict because it differs from the shared
                # entity: a per-inbound override.
                overrides += 1
            client_objects.add(identity)
            raw = client.get('raw_client')
            if isinstance(raw, dict):
                raw_memberships += 1
                raw_objects.add(id(raw))
            if any(name.endswith('_formatted') for name in client.keys()):
                formatted_memberships += 1
    entities = len(canonical)
    ratio = round(memberships / entities, 2) if entities else None
    return {
        'available': True,
        'servers': len(snapshot.get('servers_status') or []),
        'inbounds': len(inbounds),
        # Schema-v2 vocabulary.
        'canonical_client_entities': entities,
        'client_memberships': memberships,
        'membership_ratio': ratio,
        'shared_memberships': shared_memberships,
        'membership_overrides': overrides,
        'distinct_client_object_count': len(client_objects),
        'distinct_raw_client_object_count': len(raw_objects),
        'memberships_with_raw_client': raw_memberships,
        'memberships_with_formatted_strings': formatted_memberships,
        # Pre-v2 aliases, kept so existing consumers and tests keep working.
        'client_rows': memberships,
        'unique_clients': entities,
        'duplicate_rows': max(0, memberships - entities),
        'duplication_ratio': ratio,
        'rows_with_raw_client': raw_memberships,
        'rows_with_formatted_strings': formatted_memberships,
        'last_update': snapshot.get('last_update'),
        'note': ('client_memberships counts a client appearing on an inbound; '
                 'canonical_client_entities counts the distinct client dicts those '
                 'memberships share. membership_overrides counts memberships whose dict is '
                 'not the shared entity object, so distinct_client_object_count is the '
                 'number of client dicts actually retained - not the membership count.'),
    }


def _copy_key(role) -> str:
    return COPY_KEY_PREFIX + str(role)


def record_snapshot_copy(*, inbounds, servers=(), version=None, now=None) -> bool:
    """Publish this process's snapshot footprint, once per snapshot version.

    This is what lets one endpoint call answer "how many full copies exist" instead of one
    call per role: a process records what it holds when it adopts a version, and
    :func:`snapshot_copies` reads the records back.

    Bounded and version-throttled: one small key with a TTL, written only when this process
    adopts a version it has not recorded yet, so a forced reload of an unchanged version
    issues no command at all. It never raises - a footprint record must not be able to break
    a snapshot load, and a Redis double that does not implement ``set`` must stay harmless.
    """
    marker = '' if version is None else str(version)
    role = (os.environ.get('EVE_PROCESS_ROLE') or 'combined').strip().lower()
    if marker and _last_copy_versions.get(role) == marker:
        return False
    inbounds = [inbound for inbound in (inbounds or []) if isinstance(inbound, dict)]
    rows = 0
    unique = set()
    for inbound in inbounds:
        for client in (inbound.get('clients') or []):
            if not isinstance(client, dict):
                continue
            rows += 1
            unique.add(str(client.get('id') or client.get('uuid') or client.get('email')))
    payload = {
        'role': role,
        'pid': os.getpid(),
        'at': round(time.time() if now is None else float(now), 1),
        'client_rows': rows,
        'unique_clients': len(unique),
        'inbounds': len(inbounds),
        'servers': len(servers or []),
        'version': marker,
    }
    try:
        from panel.core import redis_client
        client = redis_client.get_redis()
        if client is None:
            return False
        client.set(_copy_key(role), json.dumps(payload, separators=(',', ':')),
                   ex=COPY_TTL_SECONDS)
        _last_copy_versions[role] = marker
        return True
    except Exception:
        return False


def snapshot_copies(now=None) -> dict:
    """Every process that published a snapshot footprint, and how many copies that is.

    One GET per known role, never a keyspace SCAN: the roles are the fixed set the units
    write, and a scan would grow with everything else Redis holds - the hidden cost this
    module exists to avoid. A record older than its TTL is reported as expired rather than
    counted, so a process that was stopped stops being a copy.
    """
    moment = time.time() if now is None else float(now)
    try:
        from panel.core import redis_client
        import json
        client = redis_client.get_redis()
    except Exception as exc:
        return {'available': False, 'reason': str(exc)[:120]}
    if client is None:
        return {'available': False, 'reason': 'no Redis configured'}
    found = {}
    expired = []
    try:
        for role in COPY_ROLES:
            raw = client.get(_copy_key(role))
            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', 'replace')
            try:
                record = json.loads(raw)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            age = moment - float(record.get('at') or 0)
            if age > COPY_TTL_SECONDS:
                expired.append(role)
                continue
            record['age_seconds'] = round(age, 1)
            found[role] = record
    except Exception as exc:
        return {'available': False, 'reason': str(exc)[:120]}
    rows = [int(record.get('client_rows') or 0) for record in found.values()]
    versions = sorted({str(record.get('version')) for record in found.values()
                       if record.get('version')})
    return {
        'available': True,
        'copies': len(found),
        'roles': found,
        'expired_roles': sorted(expired),
        'largest_client_rows': max(rows, default=0),
        'client_rows_summed': sum(rows),
        'versions': versions,
        'ttl_seconds': COPY_TTL_SECONDS,
        'note': ('one record per process that adopted the shared snapshot, written when it '
                 'adopted a new version and expiring with the TTL. More than one copy with '
                 'comparable rows is suspects A/B/C; client_rows_summed counts rows per '
                 'copy, so the same fleet held twice sums to twice the fleet.'),
    }


def cache_footprint() -> dict:
    """Sizes of the caches that live outside the snapshot. Counts only."""
    out = {'available': True, 'caches': {}}
    try:
        from panel.adapters import xui
        sessions = getattr(xui, 'XUI_SESSION_CACHE', None)
        if isinstance(sessions, dict):
            out['caches']['xui_sessions'] = {'entries': len(sessions)}
        caps = getattr(xui, 'XUI_CAPABILITY_CACHE', None)
        if isinstance(caps, dict):
            out['caches']['xui_capabilities'] = {'entries': len(caps)}
    except Exception:
        pass
    try:
        from panel.services import subscription_cache
        metrics = subscription_cache.metrics()
        out['caches']['subscription'] = {key: value for key, value in metrics.items()
                                         if isinstance(value, (int, float))}
    except Exception:
        pass
    try:
        from panel.jobs import refresh as refresh_jobs
        jobs = getattr(refresh_jobs, 'REFRESH_JOBS', None)
        if isinstance(jobs, dict):
            out['caches']['refresh_jobs'] = {'entries': len(jobs)}
        bulk = getattr(refresh_jobs, 'BULK_JOBS', None)
        if isinstance(bulk, dict):
            out['caches']['bulk_jobs'] = {'entries': len(bulk)}
    except Exception:
        pass
    try:
        from panel.services import ownership
        for name in ('_OWNERSHIP_CACHE', '_CACHE'):
            store = getattr(ownership, name, None)
            if isinstance(store, dict):
                out['caches']['ownership'] = {'entries': len(store)}
                break
    except Exception:
        pass
    return out


def redis_snapshot_bytes() -> dict:
    """Compressed size of the published snapshot: O(servers), never a keyspace scan.

    The manifest already lists the server ids that have a published block, so the size is
    one STRLEN per server - which is what makes this cheap enough to show on an overview
    page. A SCAN over the whole keyspace would grow with everything else Redis holds, and
    this module exists to stop exactly that kind of hidden cost.
    """
    try:
        from panel.core import redis_client
        import json
        client = redis_client.get_redis()
    except Exception as exc:
        return {'available': False, 'reason': str(exc)[:120]}
    if client is None:
        return {'available': False, 'reason': 'no Redis configured'}
    prefix = getattr(redis_client, 'REDIS_SERVER_SNAPSHOT_PREFIX', 'eve:server_data:')
    manifest_key = getattr(redis_client, 'REDIS_SNAPSHOT_MANIFEST_KEY', None)
    try:
        raw_manifest = client.get(manifest_key) if manifest_key else None
        manifest = {}
        if raw_manifest:
            try:
                manifest = redis_client._decode_snapshot(raw_manifest)
            except Exception:
                manifest = {}
        server_ids = list((manifest.get('server_versions') or {}).keys())
        total = 0
        keys = 0
        largest = 0
        for sid in server_ids:
            try:
                size = int(client.strlen(prefix + str(int(sid))) or 0)
            except Exception:
                continue
            total += size
            keys += 1
            largest = max(largest, size)
        manifest_bytes = 0
        if manifest_key:
            try:
                manifest_bytes = int(client.strlen(manifest_key) or 0)
            except Exception:
                manifest_bytes = 0
        version = manifest.get('last_update')
        return {
            'available': True,
            'server_keys': keys,
            'servers_in_manifest': len(server_ids),
            'server_blocks_bytes': total,
            'largest_block_bytes': largest,
            'manifest_bytes': manifest_bytes,
            'total_bytes': total + manifest_bytes,
            'last_update': version,
            'note': ('the compressed size of what is published to Redis; the live Python '
                     'object is larger, which is what snapshot.* describes'),
        }
    except Exception as exc:
        return {'available': False, 'reason': str(exc)[:120]}


def _snapshot_sizes() -> dict:
    """Two cheap sums for the trend sample: no set, no per-client allocation."""
    try:
        from app import GLOBAL_SERVER_DATA  # deferred: app-level state
        inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    except Exception:
        return {'client_rows': None, 'inbounds': None}
    return {'client_rows': sum(len(row.get('clients') or []) for row in inbounds
                               if isinstance(row, dict)),
            'inbounds': len(inbounds)}


def sample(now=None, *, process_scan=True) -> dict:
    """One compact trend sample: host availability plus Eve's total PSS.

    Also records the two cheap aggregates (client rows, inbounds) and the oldest role
    uptime, so a later reading can tell growth with a stable client count on an old process
    from a process that is merely still warming up. These are sums over the inbound list,
    not the counting pass in snapshot_footprint(): a sample every minute must not allocate a
    50k-entry set, which is the kind of churn this module exists to measure.
    """
    moment = time.time() if now is None else float(now)
    host = host_memory(now=moment)
    eves = eve_processes() if process_scan else {'available': False, 'roles': {}}
    by_role = {role: int(bucket.get('pss_bytes') or 0)
               for role, bucket in (eves.get('roles') or {}).items()}
    uptimes = [float(bucket.get('max_uptime_seconds') or 0)
               for bucket in (eves.get('roles') or {}).values()]
    sizes = _snapshot_sizes()
    return {
        'at': round(moment, 1),
        'available_bytes': host.get('available_bytes'),
        'cache_bytes': host.get('cache_bytes'),
        'swap_used_bytes': host.get('swap_used_bytes'),
        'eve_pss_bytes': eves.get('eve_pss_bytes'),
        'eve_pss_complete': eves.get('eve_pss_complete'),
        'roles': by_role,
        'client_rows': sizes['client_rows'],
        'inbounds': sizes['inbounds'],
        'uptime_seconds': (max(uptimes) if uptimes else None),
    }


def record_sample(now=None, *, force=False) -> bool:
    """Append one sample to the bounded Redis ring (throttled to one a minute).

    Bounded by construction: ``LPUSH`` + ``LTRIM`` to ``SAMPLE_MAX``. A trend that grows
    with uptime would itself be a memory leak, which is the thing this module is supposed
    to be able to rule out.
    """
    global _last_sample_at
    moment = time.time() if now is None else float(now)
    with _lock:
        if not force and (moment - _last_sample_at) < SAMPLE_INTERVAL_SECONDS:
            return False
        _last_sample_at = moment
    try:
        from panel.core import redis_client
        import json
        client = redis_client.get_redis()
        if client is None:
            return False
        client.lpush(SAMPLE_KEY, json.dumps(sample(now=moment), separators=(',', ':')))
        client.ltrim(SAMPLE_KEY, 0, SAMPLE_MAX - 1)
        client.expire(SAMPLE_KEY, 86400 * 3)
        return True
    except Exception:
        return False


def _trend_unavailable(reason, minutes) -> dict:
    """The trend's keys with None for "not measured", so the shape does not vary.

    Three consumers read this (the Overview, the collector and the route contract), and a
    branch that omits keys makes each of them guard differently for the same condition.
    """
    return {'available': False, 'reason': reason, 'samples': None,
            'window_minutes': minutes, 'max_samples': SAMPLE_MAX, 'current_bytes': None,
            'peak_bytes': None, 'window_start_bytes': None, 'delta_bytes': None,
            'per_hour_bytes': None, 'trend': None, 'series': [], 'uptime_seconds': None,
            'client_rows_start': None, 'client_rows_end': None, 'counts_stable': None,
            'pss_continuous': None, 'incomplete_pss_samples': None}


def trend(now=None, *, minutes=SAMPLE_KEEP_MINUTES, series_points=120) -> dict:
    """Current / peak / delta over the window, plus a bounded series to draw.

    ``series`` exists so a chart can be drawn from measurements instead of an
    interpolation: an operator asking "is this growing?" is looking at a shape, and the
    only honest way to draw one is to hand over the samples that were taken. It is capped
    at ``series_points`` (the newest ones) so the payload stays small even though the ring
    holds a day.
    """
    moment = time.time() if now is None else float(now)
    try:
        from panel.core import redis_client
        import json
        client = redis_client.get_redis()
        if client is None:
            return _trend_unavailable('no Redis configured', minutes)
        raw = client.lrange(SAMPLE_KEY, 0, SAMPLE_MAX - 1)
    except Exception as exc:
        return _trend_unavailable(str(exc)[:120], minutes)
    samples = []
    for item in raw or []:
        if isinstance(item, bytes):
            item = item.decode('utf-8', 'replace')
        try:
            samples.append(json.loads(item))
        except Exception:
            continue
    samples.sort(key=lambda row: row.get('at') or 0)
    window = [row for row in samples if (moment - float(row.get('at') or 0)) <= minutes * 60]
    marks = [int(row['eve_pss_bytes']) for row in window if row.get('eve_pss_bytes')]
    if not marks:
        return {'available': True, 'samples': 0, 'window_minutes': minutes,
                'max_samples': SAMPLE_MAX, 'series': [],
                'uptime_seconds': None, 'client_rows_start': None, 'client_rows_end': None,
                'counts_stable': None, 'pss_continuous': None, 'incomplete_pss_samples': 0,
                'note': 'no samples yet in this window'}
    current = marks[-1]
    peak = max(marks)
    delta = current - marks[0]
    span = max(1.0, (float(window[-1]['at']) - float(window[0]['at'])) / 60.0)
    per_hour = delta / span * 60.0
    if per_hour > 20 * 1024 * 1024:
        direction = 'growing'
    elif per_hour < -20 * 1024 * 1024:
        direction = 'shrinking'
    else:
        direction = 'stable'
    points = [row for row in window if row.get('eve_pss_bytes')]
    series = [{'at': round(float(row['at']), 1), 'bytes': int(row['eve_pss_bytes'])}
              for row in points[-max(2, int(series_points)):]]
    # Context for the slope, so a growth reading is not read as a leak on its own: how many
    # samples could not read every process PSS (a discontinuous series), whether the client
    # count moved, and how old the process is.
    incomplete = [row for row in window if row.get('eve_pss_complete') is False]
    with_counts = [row for row in window if row.get('client_rows') is not None]
    counts_stable = None
    client_rows_start = client_rows_end = None
    if len(with_counts) >= 2:
        client_rows_start = int(with_counts[0]['client_rows'])
        client_rows_end = int(with_counts[-1]['client_rows'])
        counts_stable = abs(client_rows_end - client_rows_start) <= max(
            1, int(0.02 * max(1, client_rows_start)))
    return {
        'available': True,
        'samples': len(marks),
        'window_minutes': minutes,
        'max_samples': SAMPLE_MAX,
        'current_bytes': current,
        'peak_bytes': peak,
        'window_start_bytes': marks[0],
        'delta_bytes': delta,
        'per_hour_bytes': int(per_hour),
        'trend': direction,
        'series': series,
        'uptime_seconds': (window[-1].get('uptime_seconds') if window else None),
        'client_rows_start': client_rows_start,
        'client_rows_end': client_rows_end,
        'counts_stable': counts_stable,
        'pss_continuous': not incomplete,
        'incomplete_pss_samples': len(incomplete),
        'note': ('a steady high value after a full dashboard load is retained snapshot; a '
                 'value that climbs while the client count is flat is growth that needs a '
                 'longer window before it can be called anything more than that'),
    }


def host_report(*, now=None, trend_minutes=SAMPLE_KEEP_MINUTES) -> dict:
    """The host-wide attribution: safe to call from a process that is not the app.

    Everything here is read from ``/proc``, Redis and the modules' own caches, so it never
    imports the application. ``report()`` adds the in-process snapshot on top - a snapshot
    only means something inside a worker that actually holds one, and importing the app to
    discover that would run the app's import-time side effects (including migrations) in
    whatever process asked, which is not something a read-only collector may do.
    """
    moment = time.time() if now is None else float(now)
    host = host_memory(now=moment)
    eves = eve_processes()
    payload = {
        'available': host.get('available', False),
        'sampled_at': moment,
        'pid': os.getpid(),
        'process_role': (os.environ.get('EVE_PROCESS_ROLE') or 'combined').strip().lower(),
        'host': host,
        'eve': eves,
        'accounting': accounting(host, eves),
        'redis_snapshot': redis_snapshot_bytes(),
        'snapshot_copies': snapshot_copies(now=moment),
        'caches': cache_footprint(),
        'trend': trend(now=moment, minutes=trend_minutes),
    }
    try:
        from panel.core import memory_probe
        payload['background_fetch'] = memory_probe.report()
    except Exception as exc:
        payload['background_fetch'] = {
            'available': False,
            'reason': str(exc)[:120],
            'samples': [],
        }
    payload['health'] = _health(payload)
    return payload


def report(*, now=None, trend_minutes=SAMPLE_KEEP_MINUTES) -> dict:
    """The whole attribution in one payload for Settings -> Overview."""
    payload = host_report(now=now, trend_minutes=trend_minutes)
    payload['snapshot'] = snapshot_footprint()
    # Recomputed with the snapshot present: this is where the duplication note comes from.
    payload['health'] = _health(payload)
    return payload


def _health(payload) -> dict:
    host = payload.get('host') or {}
    notes = []
    if not host.get('available'):
        return {'state': 'unknown', 'notes': [host.get('reason') or 'host memory unknown']}
    available_pct = host.get('available_pct')
    if available_pct is not None and available_pct < 10:
        notes.append('less than 10% of RAM is available')
    elif available_pct is not None and available_pct < 20:
        notes.append('less than 20% of RAM is available')
    if (host.get('swap_used_bytes') or 0) > 0:
        notes.append('swap is in use, which means the host has already been under pressure')
    eves = payload.get('eve') or {}
    eve_pss = eves.get('eve_pss_bytes')
    total = host.get('total_bytes')
    # A partial PSS sum must not be compared against total RAM: if a process in Eve's own
    # groups could not be read, the comparison would be made on a number that is missing a
    # part of Eve.
    if eve_pss and total and eve_pss > 0.6 * total and eves.get('eve_pss_complete') is not False:
        notes.append('Eve alone accounts for most of the host memory')
    # The membership ratio is deliberately NOT a health note: one client on several inbounds
    # is what v3 looks like, not a condition. The numbers live in the snapshot group, worded
    # in schema-v2 terms.
    #
    # Growth is an observation, not a diagnosis. A one-hour slope cannot tell warm-up or
    # allocator high-water from a leak - the background probe measured exactly that shape (a
    # restart rises for hours and then plateaus). So the note names its window and says what
    # would settle it, and the stronger wording needs all of: a long enough window, a process
    # past warm-up, a stable client count and a continuous series.
    trend = payload.get('trend') or {}
    if trend.get('trend') == 'growing':
        if trend.get('pss_continuous') is False:
            notes.append('EVE PSS trends up in the selected window, but %s sample(s) in it '
                         'could not read every process PSS, so the slope is not comparable'
                         % trend.get('incomplete_pss_samples'))
        else:
            notes.append('EVE PSS is growing in the selected %s-minute window; longer-lived '
                         'samples are needed to distinguish warm-up or allocator high-water '
                         'from sustained growth'
                         % (trend.get('window_minutes') or '?'))
            if ((trend.get('window_minutes') or 0) >= SUSTAINED_WINDOW_MINUTES
                    and (trend.get('uptime_seconds') or 0) >= SUSTAINED_UPTIME_SECONDS
                    and trend.get('counts_stable') is True):
                notes.append('EVE PSS grew across a %s-hour window with a stable client '
                             'count and a process older than %s h'
                             % (round((trend.get('window_minutes') or 0) / 60),
                                round(SUSTAINED_UPTIME_SECONDS / 3600)))
    return {'state': 'ok' if not notes else 'warning', 'notes': notes}


def analyze_python_memory(*, limit=40, timeout_seconds=2.0) -> dict:
    """Admin-only deep sample: the largest Python allocations, bounded and PII-free.

    ``tracemalloc`` is started here and stopped in the same call, so it never runs
    continuously; the sample is capped by ``limit`` and the caller is told when the
    timeout cut it short. Only file names and sizes leave this function - never a repr,
    which is where a token or a customer email would hide.
    """
    import tracemalloc
    started = time.monotonic()
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start(10)
    try:
        snapshot = tracemalloc.take_snapshot()
        stats = snapshot.statistics('lineno')[:max(1, int(limit))]
        entries = []
        for stat in stats:
            frame = stat.traceback[0]
            entries.append({
                'file': os.path.basename(frame.filename),
                'line': frame.lineno,
                'size_bytes': int(stat.size),
                'blocks': int(stat.count),
            })
        current, peak = tracemalloc.get_traced_memory()
        return {
            'available': True,
            'entries': entries,
            'current_bytes': int(current),
            'peak_bytes': int(peak),
            'elapsed_ms': int((time.monotonic() - started) * 1000),
            'truncated': (time.monotonic() - started) > timeout_seconds,
            'note': ('file names and sizes only: a Python repr can carry a credential or a '
                     'customer identifier, so none is returned'),
        }
    finally:
        if not was_tracing:
            tracemalloc.stop()
