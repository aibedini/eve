"""On-demand system network rates, shared between viewers and worker processes."""
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from panel.adapters.xui import fetch_server_status, get_xui_session
from panel.core.redis_client import get_redis
from panel.extensions import db
from panel.models import Server

POLL_SECONDS = 5
FRESH_SECONDS = 15
_cache = {}
_inflight = set()
_scheduled = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='server-network')


def _read_server(server_id, app):
    with app.app_context():
        server = db.session.get(Server, server_id)
        if not server or not server.enabled or server.hidden:
            return None, None, 'unavailable'
        panel_session, error = get_xui_session(server)
        if error or panel_session is None:
            return None, None, 'connection'
        status, error, _ = fetch_server_status(panel_session, server.host, server.panel_type)
        if error or not status:
            return None, None, 'connection'
        up, down = status.get('network_up_bps'), status.get('network_down_bps')
        return up, down, None if up is not None and down is not None else 'unsupported'


def _sample(server_id, app):
    now = time.time()
    key = f'eve:network-rate:{server_id}'
    redis = get_redis()
    cached = None
    if redis is not None:
        try:
            raw = redis.get(key)
            cached = json.loads(raw) if raw else None
        except Exception:
            redis = None
    with _lock:
        if cached is None:
            cached = _cache.get(server_id)
        if cached and 0 <= now - cached['checked_at'] < POLL_SECONDS:
            return dict(cached)
        if server_id in _inflight:
            return dict(cached) if cached else {'server_id': server_id, 'error': 'pending'}
        _inflight.add(server_id)
    token = uuid.uuid4().hex
    claimed = False
    try:
        if redis is not None:
            try:
                claimed = bool(redis.set(key + ':lock', token, nx=True, ex=120))
                if not claimed:
                    return dict(cached) if cached else {'server_id': server_id, 'error': 'pending'}
            except Exception:
                redis = None
        try:
            up, down, error = _read_server(server_id, app)
        except Exception:
            up, down, error = None, None, 'connection'
        finished = time.time()
        sample = {
            'server_id': server_id, 'checked_at': finished,
            'sampled_at': finished if error is None else (cached or {}).get('sampled_at'),
            'upload_bps': up if error is None else (cached or {}).get('upload_bps'),
            'download_bps': down if error is None else (cached or {}).get('download_bps'),
            'error': error,
        }
        with _lock:
            for sid in list(_cache):
                if finished - _cache[sid]['checked_at'] > 60:
                    del _cache[sid]
            if len(_cache) >= 1024:
                _cache.pop(next(iter(_cache)))
            _cache[server_id] = sample
        if redis is not None:
            try:
                redis.set(key, json.dumps(sample), ex=60)
            except Exception:
                pass
        return dict(sample)
    finally:
        if claimed and redis is not None:
            try:
                redis.eval("if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end", 1, key + ':lock', token)
            except Exception:
                pass  # The lease expires even if Redis disconnects.
        with _lock:
            _inflight.discard(server_id)


def collect_network(server_ids, app):
    ids = list(dict.fromkeys(server_ids))
    now = time.time()
    shared = {}
    redis = get_redis()
    if redis is not None and ids:
        try:
            for sid, raw in zip(ids, redis.mget([f'eve:network-rate:{sid}' for sid in ids])):
                if raw:
                    shared[sid] = json.loads(raw)
        except Exception:
            shared = {}
    with _lock:
        rows = [dict(shared.get(sid) or _cache.get(sid) or
                     {'server_id': sid, 'error': 'pending'}) for sid in ids]
        for sid, job in list(_scheduled.items()):
            if job.done():
                del _scheduled[sid]
        # Reads never wait for an unreachable panel. Oldest samples go first.
        for row in sorted(rows, key=lambda row: row.get('checked_at', 0)):
            sid = row['server_id']
            if len(_scheduled) >= 8:
                break
            if sid not in _scheduled and not 0 <= now - row.get('checked_at', 0) < POLL_SECONDS:
                _scheduled[sid] = _pool.submit(_sample, sid, app)
    for row in rows:
        stamp = row.get('sampled_at')
        row['age_seconds'] = max(0, now - stamp) if stamp is not None else None
        row['fresh'] = bool(not row.get('error') and stamp is not None
                            and 0 <= now - stamp <= FRESH_SECONDS)
    fresh = [row for row in rows if row['fresh']]
    return {
        'servers': rows,
        'totals': {
            'upload_bps': sum(row['upload_bps'] for row in fresh) if fresh else None,
            'download_bps': sum(row['download_bps'] for row in fresh) if fresh else None,
            'reporting': len(fresh), 'expected': len(ids),
        },
        'poll_seconds': POLL_SECONDS,
    }
