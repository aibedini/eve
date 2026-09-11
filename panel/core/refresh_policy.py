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
