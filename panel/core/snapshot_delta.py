"""Delta sync for the shared dashboard snapshot.

Every /api/refresh poll used to serialize and transfer the whole snapshot
(measured 8.2 MB / ~540 ms at 18k clients, and a reseller paid ~2 s for the
deepcopy + filter; see docs/performance/BASELINE.md). This module keeps a bounded
revision history so a client that already holds revision N can be answered with
only the inbounds that changed after N, or with a tiny "unchanged" envelope when
nothing changed.

How a revision is detected:

* writers bump GLOBAL_SERVER_DATA['last_update'] on every mutation (the fetcher,
  the optimistic client patch, the Redis snapshot loader), so a changed
  last_update marks the snapshot dirty;
* mark_dirty() lets a writer say so explicitly;
* fingerprints are only recomputed for a dirty snapshot - an unchanged poll is an
  in-memory comparison and costs nothing.

Fingerprints are hashed per inbound over a canonical traversal, so any field
change is caught. The revision counter and the history can be shared through
Redis (best effort): with several gunicorn workers every worker then reports the
same revision and a client can move between workers. Without Redis each worker
keeps its own counter and a revision it does not know falls back to a full
snapshot (safe, just no delta).
"""
import hashlib
import json
import threading
import time
from collections import OrderedDict

MAX_HISTORY = 30          # revisions kept for delta computation
MAX_DELTA_KEYS = 4000     # fall back to full above this many changed inbounds
REDIS_REVISION_KEY = 'eve:snapshot_delta:revision'
REDIS_HISTORY_KEY = 'eve:snapshot_delta:history'
REDIS_HISTORY_TTL = 3600

_lock = threading.RLock()


def _init_state():
    return {
        'dirty': True,
        'dirty_servers': set(),
        'revision': 0,
        'local_revision': 0,
        'last_update': None,
        'meta': None,
        'fingerprints': {},
        'history': OrderedDict(),
    }


_state = _init_state()


def reset_state():
    """Drop all state (tests, or after a deliberate snapshot replacement)."""
    with _lock:
        _state.clear()
        _state.update(_init_state())


def mark_dirty(server_ids=None):
    """Declare that GLOBAL_SERVER_DATA was mutated in place.

    Passing the ids of the servers that were replaced lets sync() re-fingerprint
    only those blocks instead of the whole snapshot (the full pass over 18k
    clients costs ~0.6 s, a hinted pass a few milliseconds).
    """
    with _lock:
        _state['dirty'] = True
        if server_ids is None:
            _state['dirty_servers'] = set()
        else:
            _state.setdefault('dirty_servers', set()).update(
                int(server_id) for server_id in server_ids
                if server_id is not None
            )


def snapshot_key(inbound):
    """Stable identity of one inbound row: (server_id, inbound_id)."""
    if not isinstance(inbound, dict):
        return (None, None)
    return (inbound.get('server_id'), inbound.get('id'))


def _pair(key):
    return [key[0], key[1]]


def _unpair(pair):
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        return (pair[0], pair[1])
    return (None, None)


def _meta_digest(snapshot) -> str:
    """Digest of everything outside the inbound blocks (stats, server status)."""
    if not isinstance(snapshot, dict):
        return ''
    payload = json.dumps({
        'stats': snapshot.get('stats'),
        'servers': snapshot.get('servers_status'),
        'is_updating': bool(snapshot.get('is_updating')),
        'last_update': snapshot.get('last_update'),
    }, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.blake2b(payload.encode('utf-8', 'replace'), digest_size=12).hexdigest()


def fingerprint(inbound) -> str:
    """Canonical digest of one inbound (json is C-accelerated; a hand-rolled
    traversal was measured ~3x slower)."""
    payload = json.dumps(inbound, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.blake2b(payload.encode('utf-8', 'replace'), digest_size=16).hexdigest()


def _redis():
    try:
        from panel.core.redis_client import get_redis
        return get_redis()
    except Exception:
        return None


def _next_revision():
    client = _redis()
    if client is not None:
        try:
            return int(client.incr(REDIS_REVISION_KEY))
        except Exception:
            pass
    _state['local_revision'] = int(_state.get('local_revision') or 0) + 1
    return _state['local_revision']


def _record_history(revision, changed, removed, last_update):
    record = {
        'revision': revision,
        'changed': changed,
        'removed': removed,
        'last_update': last_update,
        'at': time.time(),
    }
    history = _state['history']
    history[revision] = record
    while len(history) > MAX_HISTORY:
        history.popitem(last=False)
    client = _redis()
    if client is not None:
        try:
            client.lpush(REDIS_HISTORY_KEY, json.dumps(record, separators=(',', ':')))
            client.ltrim(REDIS_HISTORY_KEY, 0, MAX_HISTORY - 1)
            client.expire(REDIS_HISTORY_KEY, REDIS_HISTORY_TTL)
        except Exception:
            pass
    return record


def touch(snapshot=None, reason='client_event'):
    """Force a new revision for a change the snapshot data itself cannot fingerprint.

    A mutation can be authoritative without being mergeable into this worker's cache
    (a cache miss on a renamed or newly created client). Nothing in ``inbounds``
    changed here, so ``sync`` would keep the old revision and a viewer's cursor would
    never pass the client-level event recorded for it. Recording an empty history entry
    keeps the delta machinery consistent: a viewer asking from the previous revision
    gets a delta with no blocks (it already patches the client from the event).
    """
    last_update = snapshot.get('last_update') if isinstance(snapshot, dict) else None
    with _lock:
        revision = _next_revision()
        _state['revision'] = revision
        _record_history(revision, [], [], last_update)
        _state['dirty'] = False
        return revision


def _redis_history():
    client = _redis()
    if client is None:
        return []
    try:
        raw = client.lrange(REDIS_HISTORY_KEY, 0, MAX_HISTORY - 1) or []
    except Exception:
        return []
    records = []
    for item in raw:
        try:
            records.append(json.loads(item))
        except Exception:
            continue
    return records


def sync(snapshot, force=False):
    """Bring the revision history up to date; returns revision/changed/removed."""
    with _lock:
        last_update = snapshot.get('last_update') if isinstance(snapshot, dict) else None
        if (not force) and (not _state['dirty']) and last_update == _state['last_update']:
            return {'revision': _state['revision'], 'changed': [], 'removed': []}

        inbounds = (snapshot.get('inbounds') or []) if isinstance(snapshot, dict) else []
        previous = _state['fingerprints']
        hints = set(_state.get('dirty_servers') or ())
        # A hint is only usable when every inbound is attributed to a server.
        if hints and any(
            isinstance(inbound, dict) and inbound.get('server_id') is None
            for inbound in inbounds
        ):
            hints = set()

        if hints:
            current = dict(previous)
            hinted_keys = {}
            changed = []
            for inbound in inbounds:
                if not isinstance(inbound, dict):
                    continue
                key = snapshot_key(inbound)
                if key[0] not in hints:
                    continue
                hinted_keys.setdefault(key[0], set()).add(key)
                digest = fingerprint(inbound)
                if previous.get(key) != digest:
                    changed.append(key)
                current[key] = digest
            removed = []
            for key in list(previous):
                if key[0] in hints and key not in hinted_keys.get(key[0], set()):
                    removed.append(key)
                    current.pop(key, None)
        else:
            current = {}
            for inbound in inbounds:
                if isinstance(inbound, dict):
                    current[snapshot_key(inbound)] = fingerprint(inbound)
            changed = [key for key, digest in current.items() if previous.get(key) != digest]
            removed = [key for key in previous if key not in current]

        meta = _meta_digest(snapshot)
        meta_changed = meta != _state.get('meta')

        _state['fingerprints'] = current
        _state['meta'] = meta
        _state['last_update'] = last_update
        _state['dirty'] = False
        _state['dirty_servers'] = set()

        revision = _state['revision']
        if changed or removed or meta_changed or revision == 0:
            revision = _next_revision()
            _state['revision'] = revision
            _record_history(revision, [_pair(key) for key in changed],
                            [_pair(key) for key in removed], last_update)

        return {
            'revision': revision,
            'changed': [_pair(key) for key in changed],
            'removed': [_pair(key) for key in removed],
        }


def current_revision(snapshot=None, force=False):
    if snapshot is None:
        with _lock:
            return _state['revision']
    return sync(snapshot, force=force)['revision']


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _records_since(since):
    """Return {revision: record} for every revision after since."""
    records = {}
    with _lock:
        for revision, record in _state['history'].items():
            if revision > since:
                records[revision] = record
    expected_start = since + 1
    if not records or min(records) > expected_start:
        for record in _redis_history():
            revision = _to_int(record.get('revision'))
            if revision is not None and revision > since:
                records.setdefault(revision, record)
    return records


def build_sync(snapshot, since, force=False):
    """Decide full / delta / unchanged for a client at revision since."""
    result = sync(snapshot, force=force)
    revision = result['revision']
    last_update = snapshot.get('last_update') if isinstance(snapshot, dict) else None
    base = {'revision': revision, 'last_update': last_update}

    since_int = _to_int(since)
    if since_int is None:
        return dict(base, mode='full', reason='no_revision')
    if since_int == revision:
        return dict(base, mode='unchanged')
    if since_int > revision or (revision - since_int) > MAX_HISTORY * 4:
        return dict(base, mode='full', reason='unknown_revision')

    records = _records_since(since_int)
    if not records:
        return dict(base, mode='full', reason='unknown_revision')
    expected = range(since_int + 1, revision + 1)
    if any(item not in records for item in expected):
        return dict(base, mode='full', reason='history_gap')

    changed = set()
    removed = set()
    for revision_item in expected:
        record = records[revision_item]
        changed.update(_unpair(pair) for pair in record.get('changed') or [])
        removed.update(_unpair(pair) for pair in record.get('removed') or [])
    changed -= removed
    if len(changed) > MAX_DELTA_KEYS:
        return dict(base, mode='full', reason='too_many_changes')

    return dict(
        base,
        mode='delta',
        changed=[_pair(key) for key in sorted(changed, key=str)],
        removed=[_pair(key) for key in sorted(removed, key=str)],
    )


def select_inbounds(snapshot, keys):
    """Return the inbounds named by (server_id, inbound_id) pairs."""
    if not keys or not isinstance(snapshot, dict):
        return []
    wanted = {_unpair(pair) for pair in keys}
    return [
        inbound for inbound in (snapshot.get('inbounds') or [])
        if isinstance(inbound, dict) and snapshot_key(inbound) in wanted
    ]
