"""Recent per-client changes, for the SSE fast path and cross-tab sync.

The revision-aware stream already tells a browser "something changed, go fetch the
delta". That is enough to stay correct but not enough to be *fast* on the tab that did
not make the change: it still pays a request to learn which client moved.

This module keeps a small, bounded log of client-level changes (server, client, the
snapshot revision it landed at and the canonical state). The stream replays the entries
newer than a viewer's revision as `client.changed` events, so a second tab can patch the
one card instead of downloading a delta -- and the state travels with the event, so an
unverified write is never adopted (it simply has no state).

Storage mirrors the rest of the project: Redis when configured, so workers share the
log, and an in-process deque otherwise.
"""
from __future__ import annotations

import json
import threading
from collections import deque

from panel.core.redis_client import get_redis

MAX_EVENTS = 200
REDIS_EVENTS_KEY = 'eve:client_events'

_lock = threading.Lock()
_events = deque(maxlen=MAX_EVENTS)


def reset() -> None:
    """Drop the in-process log (tests, and a fresh process)."""
    with _lock:
        _events.clear()


def record(server_id, *, client_id=None, email=None, revision=0, operation=None,
           client_state=None, deleted=False) -> dict:
    """Append one client change. Bounded, never raises, never blocks a caller."""
    event = {
        'server_id': int(server_id) if server_id is not None else None,
        'client_id': client_id,
        'email': email,
        'revision': int(revision or 0),
        'operation': operation,
        'deleted': bool(deleted),
        'client_state': client_state,
    }
    with _lock:
        _events.append(event)
    client = get_redis()
    if client is not None:
        try:
            payload = json.dumps(event, ensure_ascii=False, default=str)
            pipe = client.pipeline()
            pipe.lpush(REDIS_EVENTS_KEY, payload)
            pipe.ltrim(REDIS_EVENTS_KEY, 0, MAX_EVENTS - 1)
            pipe.execute()
        except Exception:
            pass
    return event


def since(revision) -> list:
    """Events with a revision newer than `revision`, oldest first.

    A falsy revision yields nothing: a viewer without a revision bootstraps over HTTP
    and must not receive a replay of the whole log.
    """
    try:
        threshold = int(revision)
    except (TypeError, ValueError):
        return []
    if threshold <= 0:
        return []

    events = []
    client = get_redis()
    if client is not None:
        try:
            raw = client.lrange(REDIS_EVENTS_KEY, 0, MAX_EVENTS - 1)
            for item in raw:
                try:
                    events.append(json.loads(item))
                except Exception:
                    continue
            events.reverse()   # Redis keeps the newest first; callers want oldest first
        except Exception:
            events = []
    if not events:
        with _lock:
            events = list(_events)   # the local deque is already oldest-first

    return [event for event in events if int(event.get('revision') or 0) > threshold]
