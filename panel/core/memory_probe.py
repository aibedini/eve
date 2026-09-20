"""Bounded, aggregate-only memory checkpoints for the background fetch cycle."""
from collections import deque
import json
import os
import threading
import time
import uuid

CHECKPOINTS = (
    'idle_before_fetch',
    'after_panel_fetch',
    'after_process_inbounds',
    'after_snapshot_commit',
    'after_redis_publish',
    'after_worker_result_release',
    'settled_30s_after_fetch',
)
MAX_SAMPLES = 256
REDIS_KEY = 'eve:memory:background_fetch'
REDIS_TTL_SECONDS = 3600
_ALLOWED_COUNTS = frozenset({
    'servers', 'inbounds', 'client_entities', 'client_memberships',
    'legacy_client_rows', 'raw_client_rows', 'processed_client_rows',
    'inflight_work', 'retained_server_results', 'dirty_servers', 'dirty_inbounds',
})
_samples = deque(maxlen=MAX_SAMPLES)
_lock = threading.Lock()


def _read_rollup(path='/proc/self/smaps_rollup'):
    try:
        with open(path, 'r', encoding='ascii', errors='replace') as handle:
            values = {}
            for line in handle:
                name, _, rest = line.partition(':')
                if name in {'Pss', 'Private_Clean', 'Private_Dirty'}:
                    values[name] = int(rest.strip().split()[0]) * 1024
            return values
    except (OSError, ValueError, IndexError):
        return {}


def _memory():
    values = _read_rollup()
    if values:
        return {
            'pss_bytes': values.get('Pss'),
            'uss_bytes': values.get('Private_Clean', 0) + values.get('Private_Dirty', 0),
            'pss_is_rss_fallback': False,
        }
    try:
        with open('/proc/self/statm', 'r', encoding='ascii') as handle:
            rss = int(handle.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        rss = None
    return {'pss_bytes': rss, 'uss_bytes': None, 'pss_is_rss_fallback': rss is not None}


def begin_cycle():
    return uuid.uuid4().hex[:12]


def record(cycle_id, checkpoint, *, counts=None, now=None):
    if checkpoint not in CHECKPOINTS:
        raise ValueError('unknown memory checkpoint: %s' % checkpoint)
    safe_counts = {}
    for name, value in (counts or {}).items():
        if name in _ALLOWED_COUNTS and isinstance(value, (int, float)):
            safe_counts[name] = int(value)
    row = {
        'cycle_id': str(cycle_id)[:32],
        'checkpoint': checkpoint,
        'at': round(time.time() if now is None else float(now), 3),
        'counts': safe_counts,
    }
    row.update(_memory())
    with _lock:
        _samples.append(row)
    try:
        from panel.core.redis_client import get_redis
        client = get_redis()
        if client is not None:
            client.lpush(REDIS_KEY, json.dumps(row, separators=(',', ':')))
            client.ltrim(REDIS_KEY, 0, MAX_SAMPLES - 1)
            client.expire(REDIS_KEY, REDIS_TTL_SECONDS)
    except Exception:
        pass
    return row


def schedule_settled(cycle_id, *, counts=None, delay=30.0):
    safe_counts = dict(counts or {})

    def settled():
        record(cycle_id, 'settled_30s_after_fetch', counts=safe_counts)

    timer = threading.Timer(delay, settled)
    timer.daemon = True
    timer.start()
    return timer


def report():
    with _lock:
        rows = list(_samples)
    source = 'process'
    try:
        from panel.core.redis_client import get_redis
        client = get_redis()
        if client is not None:
            shared = []
            for raw in client.lrange(REDIS_KEY, 0, MAX_SAMPLES - 1) or []:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8', 'replace')
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if isinstance(item, dict):
                    shared.append(item)
            if shared:
                rows = list(reversed(shared))
                source = 'redis'
    except Exception:
        pass
    return {
        'available': True,
        'max_samples': MAX_SAMPLES,
        'checkpoints': list(CHECKPOINTS),
        'samples': rows,
        'source': source,
        'note': 'aggregate counts only; no client identifiers or payloads are retained',
    }


def reset_for_tests():
    with _lock:
        _samples.clear()
