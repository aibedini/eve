"""Memory attribution: where the host's RAM went, and where Eve's RAM went.

The question this answers is "3.45 GB of 3.78 GB is used - by what?", and it answers it
without guessing:

* host totals and pressure from ``/proc/meminfo`` and ``/proc/pressure/memory``;
* per-process RSS / **PSS** / USS (private), threads, peak and uptime from
  ``/proc/<pid>/smaps_rollup`` and ``/proc/<pid>/status``;
* Eve's own processes grouped by ROLE (web, background, telegram bot, telegram egress,
  pulse, managed xray) so "Eve is using N GB" is a sum of PSS, not of double-counted RSS;
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

#: A sample is compact and lives in Redis, trimmed to a hard cap, so the history itself
#: can never become the memory problem.
SAMPLE_KEY = 'eve:memory:samples'
SAMPLE_INTERVAL_SECONDS = 60.0
SAMPLE_MAX = 1440          # 24 h at one sample a minute
SAMPLE_KEEP_MINUTES = 60   # the window the overview renders

_lock = threading.Lock()
_last_sample_at = 0.0


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
        row['pss_bytes'] = fields.get('Pss')
        row['pss_anon_bytes'] = fields.get('Pss_Anon')
        row['pss_file_bytes'] = fields.get('Pss_File')
        row['pss_shmem_bytes'] = fields.get('Pss_Shmem')
        private = (fields.get('Private_Clean') or 0) + (fields.get('Private_Dirty') or 0)
        # USS is the process's own memory: private pages plus anonymous memory it owns.
        # Reported as "private" because that is what the kernel gives us here.
        row['private_bytes'] = private or fields.get('Anonymous')
    else:
        row['pss_bytes'] = row.get('rss_bytes')   # honest fallback: PSS unknown, RSS used
        row['pss_is_rss_fallback'] = True
        row['pss_note'] = 'smaps_rollup unavailable (permission or kernel); PSS approximated by RSS'
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


def eve_processes(*, include_other=False) -> dict:
    """Eve's processes grouped by role, aggregated on PSS."""
    if not os.path.isdir(PROC):
        return {'available': False, 'reason': 'no /proc', 'roles': {}}
    roles = {}
    other = {'processes': 0, 'rss_bytes': 0, 'pss_bytes': 0, 'private_bytes': 0}
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        row = process_memory(name)
        if not row.get('available'):
            continue
        role = row.get('role') or 'other'
        if role == 'other' and not include_other:
            other['processes'] += 1
            continue
        bucket = roles.setdefault(role, {
            'processes': 0, 'rss_bytes': 0, 'pss_bytes': 0, 'private_bytes': 0,
            'threads': 0, 'pids': [], 'max_uptime_seconds': 0.0,
            'peak_rss_bytes': 0, 'pss_approximated': False,
        })
        bucket['processes'] += 1
        bucket['pids'].append(row['pid'])
        for key in ('rss_bytes', 'pss_bytes', 'private_bytes', 'peak_rss_bytes'):
            bucket[key] += int(row.get(key) or 0)
        bucket['threads'] += int(row.get('threads') or 0)
        bucket['max_uptime_seconds'] = max(bucket['max_uptime_seconds'],
                                           float(row.get('uptime_seconds') or 0))
        if row.get('pss_is_rss_fallback'):
            bucket['pss_approximated'] = True
    eve_pss = sum(bucket['pss_bytes'] for role, bucket in roles.items()
                  if role != 'xray')
    eve_pss_with_xray = eve_pss + int((roles.get('xray') or {}).get('pss_bytes') or 0)
    return {
        'available': True,
        'roles': roles,
        'other_processes': other['processes'],
        'eve_pss_bytes': eve_pss,
        'eve_pss_with_xray_bytes': eve_pss_with_xray,
        'note': ('EVE total is a sum of PSS, which counts shared pages once across the '
                 'processes that map them; summing RSS would double-count the interpreter '
                 'and the loaded libraries'),
    }


def snapshot_footprint(snapshot=None) -> dict:
    """What the in-process snapshot holds, without deep-walking the object graph.

    Counting is O(inbounds + clients) integer work and allocates nothing that outlives
    the call, so it is safe to run when the overview is opened. It deliberately does NOT
    measure the true retained size of nested objects: that is what the admin-only deep
    analysis is for.
    """
    if snapshot is None:
        try:
            from app import GLOBAL_SERVER_DATA  # deferred: app-level state
            snapshot = GLOBAL_SERVER_DATA
        except Exception as exc:
            return {'available': False, 'reason': 'snapshot unavailable: %s' % str(exc)[:120]}
    inbounds = snapshot.get('inbounds') or []
    client_rows = 0
    with_raw_client = 0
    formatted_rows = 0
    unique = set()
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            continue
        for client in (inbound.get('clients') or []):
            if not isinstance(client, dict):
                continue
            client_rows += 1
            if isinstance(client.get('raw_client'), dict):
                with_raw_client += 1
            if any(key.endswith('_formatted') for key in client.keys()):
                formatted_rows += 1
            uid = client.get('id') or client.get('uuid')
            if not uid:
                uid = '%s|%s' % (inbound.get('server_id'), client.get('email'))
            unique.add(str(uid))
    unique_count = len(unique)
    return {
        'available': True,
        'servers': len(snapshot.get('servers_status') or []),
        'inbounds': len(inbounds),
        'client_rows': client_rows,
        'unique_clients': unique_count,
        'duplicate_rows': max(0, client_rows - unique_count),
        'duplication_ratio': (round(client_rows / unique_count, 2) if unique_count else None),
        'rows_with_raw_client': with_raw_client,
        'rows_with_formatted_strings': formatted_rows,
        'last_update': snapshot.get('last_update'),
        'note': ('client_rows counts what the snapshot holds; unique_clients counts '
                 'distinct client ids. The difference is the same account appearing once '
                 'per assigned inbound (a v3 characteristic), and rows_with_raw_client '
                 'counts the config that is retained twice per row'),
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
            if isinstance(raw_manifest, bytes):
                raw_manifest = raw_manifest.decode('utf-8', 'replace')
            try:
                manifest = json.loads(raw_manifest)
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


def sample(now=None, *, process_scan=True) -> dict:
    """One compact trend sample: host availability plus Eve's total PSS."""
    moment = time.time() if now is None else float(now)
    host = host_memory(now=moment)
    eves = eve_processes() if process_scan else {'available': False, 'roles': {}}
    by_role = {role: int(bucket.get('pss_bytes') or 0)
               for role, bucket in (eves.get('roles') or {}).items()}
    return {
        'at': round(moment, 1),
        'available_bytes': host.get('available_bytes'),
        'cache_bytes': host.get('cache_bytes'),
        'swap_used_bytes': host.get('swap_used_bytes'),
        'eve_pss_bytes': eves.get('eve_pss_bytes'),
        'roles': by_role,
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


def trend(now=None, *, minutes=SAMPLE_KEEP_MINUTES) -> dict:
    """Current / peak / delta over the window, from the bounded ring."""
    moment = time.time() if now is None else float(now)
    try:
        from panel.core import redis_client
        import json
        client = redis_client.get_redis()
        if client is None:
            return {'available': False, 'reason': 'no Redis configured'}
        raw = client.lrange(SAMPLE_KEY, 0, SAMPLE_MAX - 1)
    except Exception as exc:
        return {'available': False, 'reason': str(exc)[:120]}
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
    return {
        'available': True,
        'samples': len(marks),
        'window_minutes': minutes,
        'current_bytes': current,
        'peak_bytes': peak,
        'window_start_bytes': marks[0],
        'delta_bytes': delta,
        'per_hour_bytes': int(per_hour),
        'trend': direction,
        'note': ('a steady high value after a full dashboard load is retained snapshot, '
                 'not a leak; a value that climbs while the client count is flat is a leak'),
    }


def report(*, now=None, trend_minutes=SAMPLE_KEEP_MINUTES) -> dict:
    """The whole attribution in one payload for Settings -> Overview."""
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
        'snapshot': snapshot_footprint(),
        'redis_snapshot': redis_snapshot_bytes(),
        'caches': cache_footprint(),
        'trend': trend(now=moment, minutes=trend_minutes),
    }
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
    if eve_pss and total and eve_pss > 0.6 * total:
        notes.append('Eve alone accounts for most of the host memory')
    snapshot = payload.get('snapshot') or {}
    if (snapshot.get('duplication_ratio') or 0) > 1.2:
        notes.append('the snapshot holds %sx duplicate client rows'
                     % snapshot.get('duplication_ratio'))
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
