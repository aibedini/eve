"""Short-TTL cache for public subscription responses (phase 18).

The public /s/<server>/<sub> route used to perform two to four live X-UI panel
round trips on EVERY request. VPN clients poll subscriptions on their own schedule
and thousands of clients can share a handful of subscriptions, so the request path
paid the panel latency every time.

This module caches the rendered response (body, status, headers) per
(server, subscription id, variant) for a short TTL, with:

* a bounded LRU so memory stays flat;
* single-flight bookkeeping (begin/end/wait_for_fill) so a burst of misses for the
  same key triggers one panel read and the others reuse its result;
* per-server invalidation, called by the cached-client write-through helpers that
  change a subscription's credentials;
* counters for diagnostics.

Configuration:

* EVE_SUBSCRIPTION_CACHE_ENABLED (default 1)
* EVE_SUBSCRIPTION_CACHE_TTL_SECONDS (default 30, live view with usage/expiry)
* EVE_SUBSCRIPTION_CONFIG_CACHE_TTL_SECONDS (default 300, config-only fast path)
* EVE_SUBSCRIPTION_CACHE_MAX_ENTRIES (default 2000)
* EVE_SUBSCRIPTION_CACHE_WAIT_SECONDS (default 5, follower stampede wait)
"""
import os
import threading
import time
from collections import OrderedDict

_lock = threading.RLock()
_entries = OrderedDict()
_in_flight = {}
_counters = {
    'hits': 0,
    'misses': 0,
    'stores': 0,
    'evictions': 0,
    'expired': 0,
    'invalidations': 0,
    'stampede_waits': 0,
    'stampede_fills': 0,
}
MAX_KEY_PART = 200


def _env_int(name, default, minimum=0):
    raw = (os.environ.get(name) or '').strip()
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        return default


def enabled() -> bool:
    return (os.environ.get('EVE_SUBSCRIPTION_CACHE_ENABLED') or '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def ttl_seconds(variant: str = 'full') -> int:
    if variant == 'fast':
        return _env_int('EVE_SUBSCRIPTION_CONFIG_CACHE_TTL_SECONDS', 300, minimum=1)
    return _env_int('EVE_SUBSCRIPTION_CACHE_TTL_SECONDS', 30, minimum=1)


def max_entries() -> int:
    return _env_int('EVE_SUBSCRIPTION_CACHE_MAX_ENTRIES', 2000, minimum=1)


def wait_seconds() -> float:
    return float(_env_int('EVE_SUBSCRIPTION_CACHE_WAIT_SECONDS', 5, minimum=0))


def make_key(server_id, sub_id, variant: str = 'full') -> str:
    return '%s:%s:%s' % (int(server_id), str(sub_id)[:MAX_KEY_PART], variant)


def server_prefix(server_id) -> str:
    return '%s:' % int(server_id)


def reset() -> None:
    with _lock:
        _entries.clear()
        _in_flight.clear()
        for name in _counters:
            _counters[name] = 0


def get(key):
    """Return the cached response tuple, or None (expired entries are dropped)."""
    now = time.time()
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _entries.pop(key, None)
            _counters['expired'] += 1
            return None
        _entries.move_to_end(key)
        _counters['hits'] += 1
        return value


def set(key, value, *, ttl=None, variant='full'):
    if not enabled():
        return False
    if ttl is None:
        ttl = ttl_seconds(variant)
    with _lock:
        _entries[key] = (time.time() + ttl, value)
        _entries.move_to_end(key)
        _counters['stores'] += 1
        limit = max_entries()
        while len(_entries) > limit:
            _entries.popitem(last=False)
            _counters['evictions'] += 1
    return True


def invalidate_server(server_id) -> int:
    prefix = server_prefix(server_id)
    with _lock:
        doomed = [key for key in _entries if key.startswith(prefix)]
        for key in doomed:
            _entries.pop(key, None)
        _counters['invalidations'] += len(doomed)
    return len(doomed)


def in_flight(key) -> bool:
    with _lock:
        return key in _in_flight


def begin(key) -> bool:
    """Register this caller as the one rendering the key. False when taken."""
    with _lock:
        if key in _in_flight:
            return False
        _in_flight[key] = threading.Event()
        return True


def end(key) -> None:
    with _lock:
        event = _in_flight.pop(key, None)
    if event is not None:
        event.set()


def wait_for_fill(key, timeout=None) -> bool:
    """Wait for the in-flight renderer of the key; True when it finished."""
    with _lock:
        event = _in_flight.get(key)
    if event is None:
        return False
    budget = wait_seconds() if timeout is None else max(0.0, float(timeout))
    _counters['stampede_waits'] += 1
    filled = event.wait(timeout=budget)
    if filled:
        _counters['stampede_fills'] += 1
    return bool(filled)


def metrics() -> dict:
    with _lock:
        snapshot = dict(_counters)
        size = len(_entries)
        in_flight_count = len(_in_flight)
    total_lookups = snapshot['hits'] + snapshot['misses'] or 1
    snapshot.update({
        'entries': size,
        'in_flight': in_flight_count,
        'hit_rate': round(snapshot['hits'] / total_lookups, 4),
        'enabled': enabled(),
        'ttl_seconds': {'full': ttl_seconds('full'), 'fast': ttl_seconds('fast')},
        'max_entries': max_entries(),
    })
    return snapshot


def note_miss() -> None:
    with _lock:
        _counters['misses'] += 1
