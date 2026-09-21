"""One-shot allocator diagnostic for the long-lived background process.

Why this exists: the background process holds far more memory than an isolated process that
builds the same state (app + job modules + ORM + the hydrated snapshot + the mutation
indexes), while the fetch lifecycle returns to its own baseline at every checkpoint - so the
excess is not one retained fetch result, and the container audit found no code-level retainer
large enough to explain it. The remaining question is whether those pages hold *live objects*
or were simply never returned to the kernel by the allocator (glibc high-water after hours of
building and dropping large graphs on a threaded process).

That question cannot be answered from outside the process, and it cannot be answered with
``tracemalloc`` either: tracing only attributes allocations made *while it is tracing*, so a
snapshot taken now is blind to objects built hours ago. The decisive, cheap experiment is
therefore: measure, collect, measure, trim, measure.

    baseline -> gc.collect() -> measure -> malloc_trim(0) -> measure at 0s / 1s / 30s

Rules this module follows:

* **Explicit and one-shot.** Nothing runs unless an operator asks for a probe; when it runs,
  it runs exactly once and publishes one bounded result. It is never scheduled, never
  periodic, and never traces continuously.
* **Cross-process by Redis.** The request is written by whichever process serves the HTTP
  call; the diagnostic is executed by the background process (its watcher consumes the
  request), because that is the process under investigation.
* **Counters and byte sizes only.** No ``repr()``, no object graph walk, no environment dump,
  no credentials, no customer data. The only environment value read is ``MALLOC_ARENA_MAX``,
  reported as set/unset plus its number.
* **It does not optimise anything.** ``gc.collect()`` and ``malloc_trim(0)`` are called once
  per explicit probe, for measurement - never added to a runtime path, never periodic. Cf.
  docs/performance/MEMORY.md, which refuses both as fixes.
"""
from __future__ import annotations

import ctypes
import gc
import json
import os
import secrets
import time

MiB = 1024 * 1024
#: A change smaller than this is not treated as the explanation for a multi-hundred-MiB gap.
MATERIAL_BYTES = 64 * MiB

PROC = '/proc'
REQUEST_KEY = 'eve:memory:alloc_probe:request'
RESULT_KEY = 'eve:memory:alloc_probe:result'
REQUEST_TTL_SECONDS = 300
RESULT_TTL_SECONDS = 900
WATCH_INTERVAL_SECONDS = 5.0
TRIM_SETTLE_DELAYS = (0.0, 1.0, 30.0)
ARENA_MAX_ENV = 'MALLOC_ARENA_MAX'

SMAPS_FIELDS = ('Pss', 'Pss_Anon', 'Pss_File', 'Pss_Shmem', 'Private_Clean',
                'Private_Dirty', 'Rss')

_libc = None
_libc_checked = False


def _read(path):
    try:
        with open(path, 'r', encoding='ascii', errors='replace') as handle:
            return handle.read()
    except OSError:
        return None


def _kb(value):
    try:
        return int(str(value).strip().split()[0]) * 1024
    except (TypeError, ValueError, IndexError):
        return None


def proc_memory() -> dict:
    """This process's memory from /proc (PSS and friends), or available=False."""
    out = {'available': False}
    rollup = _read(PROC + '/self/smaps_rollup')
    if rollup:
        for line in rollup.splitlines():
            name, _, rest = line.partition(':')
            key = name.strip()
            if key in SMAPS_FIELDS:
                out[key.lower() + '_bytes'] = _kb(rest)
        out['available'] = 'pss_bytes' in out
    status = _read(PROC + '/self/status')
    if status:
        for line in status.splitlines():
            name, _, rest = line.partition(':')
            key = name.strip()
            if key == 'VmRSS':
                out['rss_bytes'] = _kb(rest)
            elif key == 'VmSwap':
                out['swap_bytes'] = _kb(rest)
            elif key == 'Threads':
                try:
                    out['threads'] = int(rest.strip())
                except ValueError:
                    pass
    private = (out.get('private_clean_bytes') or 0) + (out.get('private_dirty_bytes') or 0)
    out['uss_bytes'] = private or None
    return out


def _load_libc():
    global _libc, _libc_checked
    if _libc_checked:
        return _libc
    _libc_checked = True
    try:
        _libc = ctypes.CDLL(None, use_errno=False)
    except Exception:
        _libc = None
    return _libc


class _Mallinfo2(ctypes.Structure):
    """glibc's struct_mallinfo2 (same field order as struct_mallinfo, 64-bit sizes)."""
    _fields_ = [(name, ctypes.c_size_t) for name in (
        'arena', 'ordblks', 'smblks', 'hblks', 'hblkhd', 'usmblks', 'fsmblks',
        'uordblks', 'fordblks', 'keepcost')]


#: The fields the report carries, in the order an operator reads them.
MALLINFO_FIELDS = ('arena', 'ordblks', 'hblkhd', 'uordblks', 'fordblks', 'keepcost')


def _mallinfo2_from(struct) -> dict:
    """Parse an mallinfo2 result into plain integers. Pure: tests pass a stand-in."""
    out = {'available': True}
    for name in MALLINFO_FIELDS:
        try:
            out[name] = int(getattr(struct, name))
        except (AttributeError, TypeError, ValueError):
            out[name] = None
    return out


def mallinfo2() -> dict:
    """glibc allocator statistics, or available=False with a reason (never a guess)."""
    libc = _load_libc()
    if libc is None:
        return {'available': False, 'reason': 'no libc handle (not glibc, or ctypes unavailable)'}
    func = getattr(libc, 'mallinfo2', None)
    if func is None:
        return {'available': False, 'reason': 'this libc does not export mallinfo2'}
    try:
        func.restype = _Mallinfo2
        func.argtypes = []
        return _mallinfo2_from(func())
    except Exception as exc:
        return {'available': False, 'reason': 'mallinfo2 failed: %s' % type(exc).__name__}


def malloc_trim() -> dict:
    """Release free heap back to the OS once (glibc), or available=False with a reason."""
    libc = _load_libc()
    if libc is None:
        return {'available': False, 'reason': 'no libc handle (not glibc, or ctypes unavailable)'}
    func = getattr(libc, 'malloc_trim', None)
    if func is None:
        return {'available': False, 'reason': 'this libc does not export malloc_trim'}
    try:
        func.restype = ctypes.c_int
        func.argtypes = [ctypes.c_size_t]
        released = int(func(0))
        return {'available': True, 'trimmed': bool(released)}
    except Exception as exc:
        return {'available': False, 'reason': 'malloc_trim failed: %s' % type(exc).__name__}


def collect_once() -> dict:
    """One explicit gc.collect() with its counters. Never called outside a probe."""
    collected = gc.collect()
    try:
        counts = list(gc.get_count())
    except Exception:
        counts = None
    try:
        stats = [{'collections': int(row.get('collections', 0)),
                  'collected': int(row.get('collected', 0)),
                  'uncollectable': int(row.get('uncollectable', 0))}
                 for row in gc.get_stats()]
    except Exception:
        stats = None
    return {'collected': int(collected), 'counts': counts, 'stats': stats}


def arena_max() -> dict:
    """MALLOC_ARENA_MAX as set/unset plus its value - never the rest of the environment."""
    raw = os.environ.get(ARENA_MAX_ENV)
    if raw is None or str(raw).strip() == '':
        return {'set': False, 'value': None}
    try:
        return {'set': True, 'value': int(str(raw).strip())}
    except ValueError:
        return {'set': True, 'value': None, 'note': 'set to a non-numeric value'}


def cache_counts() -> dict:
    """Entry counts only: ORM identity map, the module caches and the in-flight table."""
    out = {}
    try:
        from panel.core import memory_report
        caches = (memory_report.cache_footprint() or {}).get('caches') or {}
        out['xui_sessions'] = (caches.get('xui_sessions') or {}).get('entries')
        out['xui_capabilities'] = (caches.get('xui_capabilities') or {}).get('entries')
        out['refresh_jobs'] = (caches.get('refresh_jobs') or {}).get('entries')
        out['bulk_jobs'] = (caches.get('bulk_jobs') or {}).get('entries')
        out['ownership_cache'] = (caches.get('ownership') or {}).get('entries')
        out['subscription_cache'] = caches.get('subscription')
    except Exception as exc:
        out['cache_error'] = type(exc).__name__
    try:
        from app import app, db  # deferred: app-level objects
        with app.app_context():
            out['db_identity_map'] = len(list(db.session.identity_map))
    except Exception as exc:
        out['db_identity_map'] = None
        out['db_identity_map_error'] = type(exc).__name__
    try:
        from panel.core import panel_limits
        out['in_flight_fetches'] = len(getattr(panel_limits, '_flights', {}) or {})
    except Exception:
        out['in_flight_fetches'] = None
    # The scheduler keeps its own `inflight` dict as a loop local; it is not reachable from
    # here, so it is reported as unavailable instead of being guessed at.
    out['scheduler_inflight'] = None
    out['scheduler_inflight_note'] = 'the scheduler keeps inflight as a loop local'
    return out


def classify(*, baseline_pss, after_gc_pss, after_trim_pss,
             material_bytes=MATERIAL_BYTES) -> str:
    """Which of the three explanations the measured deltas support.

    Pure and explicit: GC_RECLAIM (unreachable/cyclic objects), GLIBC_FRAGMENTATION (the
    allocator never returned the pages), or LIVE_RETAINED (live objects still hold it).
    """
    if None in (baseline_pss, after_gc_pss):
        return 'UNKNOWN'
    gc_freed = int(baseline_pss) - int(after_gc_pss)
    if gc_freed >= material_bytes:
        return 'GC_RECLAIM'
    if after_trim_pss is None:
        return 'UNKNOWN'
    if (int(after_gc_pss) - int(after_trim_pss)) >= material_bytes:
        return 'GLIBC_FRAGMENTATION'
    return 'LIVE_RETAINED'


def run(probe_id, *, sleep=time.sleep, trim_delays=TRIM_SETTLE_DELAYS,
        now=time.time) -> dict:
    """The staged experiment. Sleeps only between the trim settle samples."""
    started = now()
    baseline = proc_memory()
    result = {
        'probe_id': probe_id,
        'pid': os.getpid(),
        'process_role': (os.environ.get('EVE_PROCESS_ROLE') or 'combined').strip().lower(),
        'started_at': round(started, 3),
        'malloc_arena_max': arena_max(),
        'baseline': {'memory': baseline, 'caches': cache_counts()},
        'glibc_before_trim': mallinfo2(),
    }
    gc_result = collect_once()
    after_gc = proc_memory()
    result['after_gc'] = {
        'collected': gc_result['collected'],
        'gc_counts': gc_result['counts'],
        'gc_stats': gc_result['stats'],
        'memory': after_gc,
        'delta_pss_bytes': (None if None in (baseline.get('pss_bytes'),
                                             after_gc.get('pss_bytes'))
                            else after_gc['pss_bytes'] - baseline['pss_bytes']),
    }
    trim = malloc_trim()
    samples = []
    if trim.get('available'):
        previous = 0.0
        for delay in trim_delays:
            if delay > previous:
                sleep(delay - previous)
            previous = float(delay)
            samples.append({'after_seconds': float(delay), 'memory': proc_memory()})
    result['trim'] = {
        'outcome': trim,
        'samples': samples,
        'mallinfo2_after': mallinfo2() if trim.get('available') else None,
    }
    after_trim_pss = samples[-1]['memory'].get('pss_bytes') if samples else None
    result['classification'] = classify(
        baseline_pss=baseline.get('pss_bytes'),
        after_gc_pss=after_gc.get('pss_bytes'),
        after_trim_pss=after_trim_pss)
    result['finished_at'] = round(now(), 3)
    result['material_change_bytes'] = MATERIAL_BYTES
    result['note'] = ('one-shot diagnostic: gc.collect() and malloc_trim(0) ran once each, '
                      'for measurement only. Nothing here is applied to a runtime path.')
    return result


# --------------------------------------------------------------------------- #
# Cross-process protocol (the request travels in Redis, the background runs it)
# --------------------------------------------------------------------------- #

def _client():
    from panel.core import redis_client
    return redis_client.get_redis()


def request_probe(*, now=time.time) -> dict:
    """Ask the background process for one probe. Refused while one is already pending."""
    client = _client()
    if client is None:
        return {'ok': False, 'reason': 'no Redis configured'}
    probe_id = secrets.token_hex(8)
    payload = json.dumps({'probe_id': probe_id, 'requested_at': round(now(), 3)},
                         separators=(',', ':'))
    try:
        claimed = client.set(REQUEST_KEY, payload, nx=True, ex=REQUEST_TTL_SECONDS)
    except Exception as exc:
        return {'ok': False, 'reason': 'redis error: %s' % type(exc).__name__}
    if not claimed:
        return {'ok': False, 'reason': 'a probe is already pending',
                'pending': read_request(now=now)}
    return {'ok': True, 'probe_id': probe_id, 'expires_in_seconds': REQUEST_TTL_SECONDS,
            'watcher_interval_seconds': WATCH_INTERVAL_SECONDS}


def read_request(*, now=time.time) -> dict:
    client = _client()
    if client is None:
        return {'available': False, 'reason': 'no Redis configured', 'pending': None,
                'probe_id': None}
    try:
        raw = client.get(REQUEST_KEY)
    except Exception as exc:
        return {'available': False, 'reason': 'redis error: %s' % type(exc).__name__,
                'pending': None, 'probe_id': None}
    if not raw:
        return {'available': True, 'pending': False}
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    try:
        data = json.loads(raw)
    except Exception:
        return {'available': True, 'pending': True, 'probe_id': None}
    data['pending'] = True
    data['age_seconds'] = round(now() - float(data.get('requested_at') or now()), 1)
    data['available'] = True
    return data


def take_request(*, now=time.time):
    """Consume the pending request (the watcher is a singleton, so GET+DEL is enough)."""
    client = _client()
    if client is None:
        return None
    try:
        raw = client.get(REQUEST_KEY)
        if not raw:
            return None
        client.delete(REQUEST_KEY)
    except Exception:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    try:
        return json.loads(raw)
    except Exception:
        return None


def publish_result(result) -> bool:
    client = _client()
    if client is None:
        return False
    try:
        client.set(RESULT_KEY, json.dumps(result, separators=(',', ':')),
                   ex=RESULT_TTL_SECONDS)
        return True
    except Exception:
        return False


def read_result(*, now=time.time) -> dict:
    """Operator-facing view: pending / done / none, with the bounded TTL reported.

    Every branch returns the same keys (``state``, ``probe_id``, ``result``), with None for
    "not measured": the two consumers are an operator and the integration test, and a payload
    whose shape changes when Redis is missing makes each of them guard differently.
    """
    client = _client()
    if client is None:
        return {'available': False, 'reason': 'no Redis configured', 'state': None,
                'probe_id': None, 'result': None}
    request_state = read_request(now=now)
    try:
        raw = client.get(RESULT_KEY)
    except Exception as exc:
        return {'available': False, 'reason': 'redis error: %s' % type(exc).__name__,
                'state': None, 'probe_id': None, 'result': None}
    result = None
    if raw:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        try:
            result = json.loads(raw)
        except Exception:
            result = None
    if request_state.get('pending'):
        return {'available': True, 'state': 'pending', 'probe_id': request_state.get('probe_id'),
                'age_seconds': request_state.get('age_seconds'),
                'watcher_interval_seconds': WATCH_INTERVAL_SECONDS,
                'note': 'the background process has not consumed the request yet'}
    if result is not None:
        return {'available': True, 'state': 'done', 'probe_id': result.get('probe_id'),
                'result': result, 'result_ttl_seconds': RESULT_TTL_SECONDS}
    return {'available': True, 'state': 'none', 'probe_id': None, 'result': None,
            'note': 'no probe has been requested, or the result expired'}


def watch_once(*, now=time.time) -> bool:
    """Run one requested probe in THIS process. Returns True when it ran one.

    Called by the background watcher; a no-op when nothing was requested, which is what
    keeps the diagnostic explicit rather than automatic.
    """
    request = take_request(now=now)
    if not request:
        return False
    result = run(request.get('probe_id') or 'unknown', now=now)
    publish_result(result)
    return True


def reset_for_tests():
    global _libc, _libc_checked
    _libc = None
    _libc_checked = False
