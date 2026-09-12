"""Adaptive refresh cadence for the panel fetcher.

The background fetcher used to poll every panel every 30 seconds regardless of
whether anybody was using the panel. This module turns that into a policy:

* an activity level derived from dashboard traffic (active / recent / idle);
* the sleep before the next cycle is that level's target interval, clamped so the
  snapshot never grows older than the maximum staleness;
* a request wakes the fetcher immediately (threading.Event) instead of waiting out
  an idle sleep, so a returning operator gets fresh data without paying for a fast
  poll all night.

Decisions are pure functions of the clock and the snapshot age, so the policy is
unit testable and simulatable.

Environment:

* EVE_REFRESH_INTERVAL_SECONDS - active target (default 30)
* EVE_REFRESH_RECENT_SECONDS - recently active target (default 90)
* EVE_REFRESH_IDLE_SECONDS - idle target (default 300)
* EVE_REFRESH_ACTIVE_WINDOW_SECONDS - activity younger than this is active (120)
* EVE_REFRESH_RECENT_WINDOW_SECONDS - activity younger than this is recent (600)
* EVE_REFRESH_MAX_STALENESS_SECONDS - never let the snapshot get older (900)
"""
import os
import threading
import time
from datetime import datetime, timezone

MIN_INTERVAL_SECONDS = 5
LEVELS = ('active', 'recent', 'idle')
ACTIVITY_KEY = 'eve:refresh:last_activity'
REMOTE_CACHE_SECONDS = 5.0

_lock = threading.RLock()
_wake = threading.Event()
_last_activity = None
_last_recorded = 0.0
_remote_cache = {'at': 0.0, 'value': None}


def _env_int(name, default, minimum=1):
    raw = (os.environ.get(name) or '').strip()
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        return default


def active_interval() -> int:
    return _env_int('EVE_REFRESH_INTERVAL_SECONDS', 30)


def recent_interval() -> int:
    return _env_int('EVE_REFRESH_RECENT_SECONDS', 90)


def idle_interval() -> int:
    return _env_int('EVE_REFRESH_IDLE_SECONDS', 300)


def active_window() -> int:
    return _env_int('EVE_REFRESH_ACTIVE_WINDOW_SECONDS', 120)


def recent_window() -> int:
    return _env_int('EVE_REFRESH_RECENT_WINDOW_SECONDS', 600)


def max_staleness() -> int:
    return _env_int('EVE_REFRESH_MAX_STALENESS_SECONDS', 900)


def activity_poll_seconds() -> int:
    """Slice length while idle, so shared activity is noticed promptly."""
    return _env_int('EVE_REFRESH_ACTIVITY_POLL_SECONDS', 60)


def _redis():
    try:
        from panel.core.redis_client import get_redis
        return get_redis()
    except Exception:
        return None


def _publish_activity(iso_value: str) -> None:
    """Share the activity timestamp so split web/background roles agree."""
    client = _redis()
    if client is None:
        return
    try:
        client.set(ACTIVITY_KEY, iso_value, ex=max(60, recent_window() * 2))
    except Exception:
        pass


def _read_remote_activity(now=None, force=False):
    """Wall-clock timestamp (seconds) of shared activity, or None."""
    moment = time.time() if now is None else float(now)
    with _lock:
        cached = _remote_cache
        if not force and cached['value'] is not None and (moment - cached['at']) < REMOTE_CACHE_SECONDS:
            return cached['value']
    client = _redis()
    if client is None:
        value = None
    else:
        try:
            raw = client.get(ACTIVITY_KEY)
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', 'replace')
            parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00')) if raw else None
            if parsed is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            value = parsed.timestamp() if parsed is not None else None
        except Exception:
            value = None
    with _lock:
        _remote_cache['at'] = moment
        _remote_cache['value'] = value
    return value


def reset_state() -> None:
    global _last_activity, _last_recorded
    with _lock:
        _last_activity = None
        _last_recorded = 0.0
        _remote_cache['at'] = 0.0
        _remote_cache['value'] = None
        _servers.clear()
        _wake.clear()


def record_activity(throttle_seconds: float = 1.0, now=None) -> bool:
    """Mark dashboard activity; wakes the fetcher so it can re-evaluate.

    Repeated calls inside the throttle window are ignored so a busy dashboard does
    not touch the shared state on every request.
    """
    global _last_activity, _last_recorded
    moment = time.time() if now is None else float(now)
    with _lock:
        if _last_recorded and (moment - _last_recorded) < max(0.0, throttle_seconds):
            return False
        _last_activity = moment
        _last_recorded = moment
    try:
        _publish_activity(datetime.fromtimestamp(moment, timezone.utc).isoformat())
    except Exception:
        pass
    _wake.set()
    return True


def activity_age(now=None):
    """Seconds since the last recorded activity (local or shared), or None."""
    moment = time.time() if now is None else float(now)
    with _lock:
        local = _last_activity
    remote = _read_remote_activity(now=moment)
    candidates = [value for value in (local, remote) if value]
    if not candidates:
        return None
    return max(0.0, moment - max(candidates))


def activity_level(now=None) -> str:
    age = activity_age(now=now)
    if age is None:
        return 'idle'
    if age <= active_window():
        return 'active'
    if age <= recent_window():
        return 'recent'
    return 'idle'


def target_interval(level: str) -> int:
    if level == 'active':
        return active_interval()
    if level == 'recent':
        return recent_interval()
    return idle_interval()


def snapshot_age_seconds(last_update, now=None):
    """Age of an ISO timestamp from the snapshot, or None when unknown."""
    if not last_update:
        return None
    moment = datetime.now(timezone.utc) if now is None else now
    try:
        parsed = datetime.fromisoformat(str(last_update).replace('Z', '+00:00'))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return max(0.0, (moment - parsed).total_seconds())
    except Exception:
        return None


def next_interval(snapshot_age, now=None) -> float:
    """Seconds the fetcher may sleep before its next evaluation."""
    level = activity_level(now=now)
    target = target_interval(level)
    if snapshot_age is None:
        return 0.0
    remaining = max_staleness() - float(snapshot_age)
    return float(max(MIN_INTERVAL_SECONDS, min(target, remaining)))


def should_fetch_now(snapshot_age, now=None):
    """(bool, reason) for the fetcher: is the snapshot stale enough to refetch?"""
    if snapshot_age is None:
        return True, 'no_snapshot'
    if float(snapshot_age) >= max_staleness():
        return True, 'max_staleness'
    level = activity_level(now=now)
    if float(snapshot_age) >= target_interval(level):
        return True, level + '_interval'
    return False, 'fresh'


def wait_for_interval(seconds: float) -> bool:
    """Sleep up to that many seconds; True when a request woke the fetcher."""
    if seconds is None or seconds <= 0:
        return False
    woken = _wake.wait(timeout=seconds)
    _wake.clear()
    return bool(woken)


def wake() -> None:
    _wake.set()


def status(snapshot_age=None, now=None) -> dict:
    level = activity_level(now=now)
    age = activity_age(now=now)
    return {
        'level': level,
        'activity_age_seconds': None if age is None else round(age, 1),
        'target_interval_seconds': target_interval(level),
        'next_interval_seconds': next_interval(snapshot_age, now=now),
        'snapshot_age_seconds': None if snapshot_age is None else round(float(snapshot_age), 1),
        'intervals': {
            'active': active_interval(),
            'recent': recent_interval(),
            'idle': idle_interval(),
        },
        'windows': {
            'active': active_window(),
            'recent': recent_window(),
        },
        'max_staleness_seconds': max_staleness(),
    }


# ── Per-server adaptive polling (Phase 10) ───────────────────────────────────
# The cadence above decides how often the fetcher wakes; this decides whether it is a
# given panel's turn. External X-UI changes reach Eve only through this loop (the panel
# has no webhook), so the per-server interval is what bounds that latency -- without
# hammering a panel that is down. A server the operator just looked at or that an Eve
# mutation touched is polled every couple of seconds; an untouched one far less often;
# a failing one backs off exponentially. State is per process (the fetcher role owns
# the loop); the shared activity timestamp above still drives the cycle cadence.

SERVER_POLL_ACTIVE_TTL_DEFAULT = 120     # how long a server stays "active"
SERVER_POLL_BACKOFF_BASE_DEFAULT = 5
SERVER_POLL_BACKOFF_MAX_DEFAULT = 300

_servers = {}


def server_active_seconds() -> float:
    """Target interval for a server with recent activity (1-3s by design)."""
    return float(_env_int('EVE_SERVER_POLL_ACTIVE_SECONDS', 2, minimum=1))


def server_idle_seconds() -> float:
    """Target interval for a server nobody is watching."""
    return float(_env_int('EVE_SERVER_POLL_IDLE_SECONDS', 45, minimum=1))


def server_active_ttl() -> float:
    return float(_env_int('EVE_SERVER_POLL_ACTIVE_TTL_SECONDS',
                          SERVER_POLL_ACTIVE_TTL_DEFAULT, minimum=5))


def server_backoff_base() -> float:
    return float(_env_int('EVE_SERVER_POLL_BACKOFF_BASE_SECONDS',
                          SERVER_POLL_BACKOFF_BASE_DEFAULT, minimum=1))


def server_backoff_max() -> float:
    return float(_env_int('EVE_SERVER_POLL_BACKOFF_MAX_SECONDS',
                          SERVER_POLL_BACKOFF_MAX_DEFAULT, minimum=1))


def _server_state(server_id):
    try:
        sid = int(server_id)
    except (TypeError, ValueError):
        return None
    state = _servers.get(sid)
    if state is None:
        state = {'next_due': 0.0, 'failures': 0, 'active_until': 0.0}
        _servers[sid] = state
    return state


def note_server_activity(server_id, *, now=None, ttl=None) -> None:
    """Mark a server as worth watching (the operator looked at it, or Eve wrote to it).

    A panel that just became interesting is pulled in to the active wait as well: a
    panel coming on screen (or just mutated) must not sit out a remaining idle window
    of up to ``EVE_SERVER_POLL_IDLE_SECONDS`` before its first fast poll. A panel in
    backoff keeps its window -- a watch mark is not a reason to retry a failing panel.
    """
    state = _server_state(server_id)
    if state is None:
        return
    moment = time.time() if now is None else float(now)
    window = server_active_ttl() if ttl is None else max(0.0, float(ttl))
    with _lock:
        state['active_until'] = max(state.get('active_until') or 0.0, moment + window)
        if not state.get('failures'):
            soonest = moment + server_active_seconds()
            scheduled = float(state.get('next_due') or 0.0)
            if scheduled and scheduled > soonest:
                state['next_due'] = soonest


def server_interval(server_id, *, now=None) -> float:
    """The interval this server's next poll is measured against."""
    state = _server_state(server_id)
    if state is None:
        return server_idle_seconds()
    moment = time.time() if now is None else float(now)
    if state.get('failures'):
        delay = server_backoff_base() * (2 ** (state['failures'] - 1))
        return min(server_backoff_max(), delay)
    if (state.get('active_until') or 0.0) > moment:
        return server_active_seconds()
    return server_idle_seconds()


def server_due(server_id, *, now=None) -> bool:
    """True when it is this panel's turn (a server never seen before is always due)."""
    state = _server_state(server_id)
    if state is None or not state.get('next_due'):
        return True
    moment = time.time() if now is None else float(now)
    return moment >= float(state['next_due'])


def note_server_result(server_id, ok, *, now=None) -> float:
    """Record one poll's outcome and schedule the next one; returns the interval used."""
    state = _server_state(server_id)
    if state is None:
        return 0.0
    moment = time.time() if now is None else float(now)
    if ok:
        state['failures'] = 0
    else:
        state['failures'] = int(state.get('failures') or 0) + 1
    interval = server_interval(server_id, now=moment)
    state['next_due'] = moment + interval
    return interval


def next_server_due_in(*, now=None):
    """Seconds until the earliest scheduled poll, or None when nothing is tracked."""
    if not _servers:
        return None
    moment = time.time() if now is None else float(now)
    soonest = min(float(state.get('next_due') or 0.0) for state in _servers.values())
    return max(0.0, soonest - moment)


def reset_server_state() -> None:
    with _lock:
        _servers.clear()


def _coerce_server_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def server_watch_limit() -> int:
    """How many servers one open dashboard may hold on the fast cadence."""
    return _env_int('EVE_SERVER_POLL_WATCH_LIMIT', 20, minimum=1)


def note_watched_servers(server_ids, *, now=None, limit=None) -> list:
    """Mark the servers a browser says it is rendering as worth watching.

    The dashboard declares what is on screen on every poll, so the fast cadence
    follows the operator's attention instead of the whole install, and it expires on
    its own when the tab closes (nothing renews the mark). The declared set is capped
    so one open tab cannot pin a hundred-server install to a two-second fan-out.
    """
    if isinstance(server_ids, str):
        raw_values = server_ids.split(',')
    elif server_ids is None:
        raw_values = []
    else:
        try:
            raw_values = list(server_ids)
        except TypeError:
            raw_values = [server_ids]
    cap = server_watch_limit() if limit is None else max(1, int(limit))
    marked = []
    for value in raw_values:
        if len(marked) >= cap:
            break
        sid = _coerce_server_id(value)
        if sid is None or sid in marked:
            continue
        note_server_activity(sid, now=now)
        marked.append(sid)
    return marked


def defer_server_until(server_id, until) -> None:
    """Push a server's next poll to a wall-clock time; never pulls it earlier.

    The fetch layer owns its own backoff table (``panel/jobs/refresh.py``); mirroring
    the window here keeps the loop from waking for a panel it would only skip again.
    """
    state = _server_state(server_id)
    if state is None:
        return
    try:
        target = float(until)
    except (TypeError, ValueError):
        return
    with _lock:
        state['next_due'] = max(float(state.get('next_due') or 0.0), target)


def retain_servers(server_ids) -> None:
    """Forget servers that are no longer part of the enabled set.

    Without this a deleted or disabled panel would stay "due" forever and the loop
    would keep waking for a fetch that has nothing to do.
    """
    keep = set()
    for value in server_ids or ():
        sid = _coerce_server_id(value)
        if sid is not None:
            keep.add(sid)
    with _lock:
        for sid in list(_servers):
            if sid not in keep:
                _servers.pop(sid, None)


def server_due_in(server_id, *, now=None):
    """Seconds until this server's next poll (negative/zero == due now)."""
    state = _server_state(server_id)
    if state is None or not state.get('next_due'):
        return 0.0
    moment = time.time() if now is None else float(now)
    return float(state['next_due']) - moment


def server_states(*, now=None) -> dict:
    """Per-server cadence snapshot: diagnostics, the doctor page, and tests."""
    moment = time.time() if now is None else float(now)
    with _lock:
        items = sorted(_servers.items())
    rows = {}
    for sid, state in items:
        active_until = float(state.get('active_until') or 0.0)
        rows[str(sid)] = {
            'interval_seconds': round(server_interval(sid, now=moment), 3),
            'due': server_due(sid, now=moment),
            'due_in_seconds': round(max(0.0, server_due_in(sid, now=moment)), 3),
            'failures': int(state.get('failures') or 0),
            'active': active_until > moment,
            'active_for_seconds': round(max(0.0, active_until - moment), 1),
        }
    return rows
