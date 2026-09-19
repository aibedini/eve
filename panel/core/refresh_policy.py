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

Per-server cadence (panel I/O):

* EVE_SERVER_POLL_ACTIVE_SECONDS - HOT target (2)
* EVE_SERVER_POLL_WARM_SECONDS - WARM target (10)
* EVE_SERVER_POLL_IDLE_SECONDS - IDLE target (45)
* EVE_SERVER_POLL_ACTIVE_TTL_SECONDS - how long a server stays HOT (120)
* EVE_SERVER_WARM_TTL_SECONDS - how long it stays WARM after HOT (600)
* EVE_SERVER_POLL_BACKOFF_BASE_SECONDS / _MAX_SECONDS - failing panel backoff
* EVE_SERVER_POLL_IDLE_JITTER_SECONDS - spread over which idle panels come due (10)
* EVE_REFRESH_BATCH_SERVERS - panels one fan-out may read (0 = every due panel)
* EVE_SERVER_POLL_WATCH_LIMIT - servers one open dashboard may keep hot (20)
* EVE_CLIENT_FENCE_SECONDS - read-your-writes fence lifetime (30)
* EVE_RENEW_BASELINE_MAX_AGE_SECONDS - oldest cached traffic view a renewal may use (30)
"""
import json
import os
import threading
import time
import zlib
from datetime import datetime, timezone

MIN_INTERVAL_SECONDS = 5
LEVELS = ('active', 'recent', 'idle')
ACTIVITY_KEY = 'eve:refresh:last_activity'
REMOTE_CACHE_SECONDS = 5.0

# Per-server watch marks are the one piece of cadence state that MUST cross the
# process boundary: the browser talks to a web process, the polling loop lives in
# the background process, and an in-process dict in the web worker cannot make a
# panel hot for the fetcher. Marks therefore live in Redis (with the TTL as the
# expiry, so a closed tab stops refreshing on its own) and the local dict stays as
# the fallback for a single-process install and for tests.
#
# The mark itself is one key per server; the INDEX over those keys is a sorted set
# scored by expiry, because enumerating the marks used to mean `SCAN eve:refresh:watch:*`
# on the fetcher's hot path. A sorted set gives an O(log n) insert and an O(log n)
# range read of exactly the live marks, and `ZREMRANGEBYSCORE` prunes the expired
# ones -- so the cost no longer grows with the number of Redis keys in the database.
WATCH_KEY_PREFIX = 'eve:refresh:watch:'
HOT_SERVERS_KEY = 'eve:refresh:hot_servers'
WATCH_REASON_KEY = 'eve:refresh:watch_reason'

# Cross-process wake. A watch mark written by a web process is only *observed* by
# the fetcher on its next evaluation, and that evaluation can be up to a whole sleep
# slice away. Redis Pub/Sub carries a nudge so the loop breaks its sleep immediately.
#
# The channel is NOT a source of truth: a dropped message only costs latency, because
# the durable mark (key + TTL + sorted-set index) is what the policy actually reads.
WAKE_CHANNEL = 'eve:refresh:wake'
WAKE_LISTEN_TIMEOUT = 1.0
WATCH_WAKE_THROTTLE_SECONDS = 5.0

# Read-your-writes fence. A verified EVE mutation writes telemetry that some panels
# only surface through their aggregate inbound list after a propagation delay. For a
# short window after the mutation the fetcher must not let that slower, older view
# overwrite the value EVE just read back from the client-level endpoint.
MUTATION_FENCE_PREFIX = 'eve:client_fence:'
MUTATION_FENCE_DEFAULT_SECONDS = 30

# How often a legacy install (watch keys written by an older build, no sorted-set
# index yet) is scanned to rebuild the index. One bounded scan, not one per read.
LEGACY_SCAN_SECONDS = 60.0

_lock = threading.RLock()
_wake = threading.Event()
_last_activity = None
_last_recorded = 0.0
_remote_cache = {'at': 0.0, 'value': None}
_watch_cache = {'at': 0.0, 'marks': {}}
_local_watches = {}
_local_fences = {}
_legacy_scan_at = 0.0
_last_watch_wake_at = 0.0
_wake_listener = None
_wake_listener_lock = threading.Lock()
_wake_listener_stop = threading.Event()


def _env_int(name, default, minimum=1):
    raw = (os.environ.get(name) or '').strip()
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        return default


def _env_float(name, default, minimum=0.0):
    raw = (os.environ.get(name) or '').strip()
    try:
        return max(minimum, float(raw)) if raw else default
    except ValueError:
        return default


def sync_event(event, *, level='info', **fields) -> None:
    """Structured sync log. Never raises, never logs a credential.

    Callers pass only ids, modes, counters and error types; a caller with a token,
    password or email must not forward it here -- the fields below are rendered
    as ``key=value`` and this module deliberately has no idea what is sensitive.

    ``level='debug'`` is for the high-frequency events (an unchanged poll on an idle
    panel every 45 s across every server): the event still exists for an operator who
    turns the logger up, without burying the events that mean something at INFO.
    """
    try:
        from panel.core.logging_config import get_resilient_logger
        payload = ' '.join(
            '%s=%s' % (key, value) for key, value in sorted(fields.items())
            if value is not None)
        message = '%s %s' % (event, payload) if payload else str(event)
        logger = get_resilient_logger('eve.sync')
        if level == 'debug':
            logger.debug(message)
        elif level == 'warning':
            logger.warning(message)
        else:
            logger.info(message)
    except Exception:
        pass


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
    global _last_activity, _last_recorded, _legacy_scan_at, _last_watch_wake_at
    with _lock:
        _last_activity = None
        _last_recorded = 0.0
        _legacy_scan_at = 0.0
        _last_watch_wake_at = 0.0
        _local_fences.clear()
        _server_sync_cache['at'] = 0.0
        _server_sync_cache['rows'] = {}
        _remote_cache['at'] = 0.0
        _remote_cache['value'] = None
        _watch_cache['at'] = 0.0
        _watch_cache['marks'] = {}
        _local_watches.clear()
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


def publish_wake(server_ids=None, *, reason='dashboard') -> bool:
    """Nudge the process that owns the fetch loop, from any process.

    Best effort by design: the message only shortens the *latency* until the loop
    re-evaluates. The durable watch mark (key + TTL + sorted-set index) remains what
    the policy reads, so a dropped nudge costs one sleep slice and nothing else.
    """
    client = _redis()
    if client is None:
        wake()
        return False
    ids = []
    if server_ids is not None:
        for value in (server_ids if isinstance(server_ids, (list, tuple, set)) else [server_ids]):
            sid = _coerce_server_id(value)
            if sid is not None and sid not in ids:
                ids.append(sid)
    try:
        client.publish(WAKE_CHANNEL, json.dumps({
            'server_ids': ids,
            'reason': str(reason or 'dashboard')[:32],
            'at': time.time(),
        }))
        return True
    except Exception:
        return False


def _iter_server_ids(value):
    """One id, a comma-separated string, a bytes payload, or an iterable of ids.

    A bare string must never be iterated as a sequence: "601" would become 6, 0 and 1,
    so a wake message from a publisher that sends a single id would quietly warm
    unrelated panels (including ids that do not exist) until the marks expired.
    """
    if value is None:
        return []
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    if isinstance(value, str):
        return value.split(',')
    if isinstance(value, dict):
        return []
    try:
        return list(value)
    except TypeError:
        return [value]


def _handle_wake_payload(raw) -> bool:
    """Apply one wake message locally. Returns True when it was understood."""
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    try:
        payload = json.loads(raw or '{}')
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    moment = time.time()
    for value in _iter_server_ids(payload.get('server_ids')):
        sid = _coerce_server_id(value)
        if sid is None:
            continue
        # A nudge is a reason to re-evaluate, not a licence to retry a failing panel:
        # note_server_activity keeps the backoff window for a server in backoff.
        # share=False: this listener must never re-publish and loop on its own message.
        note_server_activity(sid, now=moment, share=False, reason=None)
    _wake.set()
    return True


def start_wake_listener() -> bool:
    """Subscribe to the wake channel, once per process. Safe to call repeatedly.

    The listener only ever calls `note_server_activity()` and sets the local event, so
    it cannot mutate the schedule in a way the durable marks would not also produce.
    """
    global _wake_listener
    if _redis() is None:
        return False
    with _wake_listener_lock:
        if _wake_listener is not None and _wake_listener.is_alive():
            return True
        _wake_listener_stop.clear()

        def _run():
            while not _wake_listener_stop.is_set():
                client = _redis()
                if client is None:
                    return
                pubsub = None
                try:
                    pubsub = client.pubsub(ignore_subscribe_messages=True)
                    pubsub.subscribe(WAKE_CHANNEL)
                    while not _wake_listener_stop.is_set():
                        message = pubsub.get_message(
                            timeout=WAKE_LISTEN_TIMEOUT)
                        if not message:
                            continue
                        if message.get('type') == 'message':
                            _handle_wake_payload(message.get('data'))
                except Exception:
                    # A lost Redis connection must not kill the listener: back off and
                    # resubscribe. The durable marks still drive the cadence meanwhile.
                    time.sleep(min(5.0, WAKE_LISTEN_TIMEOUT * 5))
                finally:
                    try:
                        if pubsub is not None:
                            pubsub.close()
                    except Exception:
                        pass

        thread = threading.Thread(
            target=_run, name='eve-refresh-wake', daemon=True)
        thread.start()
        _wake_listener = thread
        return True


def stop_wake_listener() -> None:
    """Stop the listener (tests, and a clean shutdown)."""
    global _wake_listener
    _wake_listener_stop.set()
    with _wake_listener_lock:
        thread, _wake_listener = _wake_listener, None
    if thread is not None:
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass


def wake_listener_active() -> bool:
    return bool(_wake_listener is not None and _wake_listener.is_alive())


def max_wake_slice() -> float:
    """Upper bound on one sleep, so a missed nudge cannot strand the loop.

    With the listener working this is only a safety net; without Redis it is what
    keeps a split-role install from waiting out a whole idle slice.
    """
    return _env_float('EVE_REFRESH_SAFETY_SLICE_SECONDS', 5.0, minimum=0.25)


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
# a failing one backs off exponentially. The schedule itself (next_due/failures) is per
# process -- the fetcher role owns the loop -- but the WATCH MARKS are shared through
# Redis, because the operator declares "this panel is on screen" in a web process while
# the loop that has to act on it runs in the background process. Without the shared
# marks a dashboard on screen and a dashboard in another process disagreed about which
# panels were hot, which is the whole reason the per-server cadence looked broken.
# The shared cycle-level activity timestamp above still drives the cycle cadence.

SERVER_POLL_ACTIVE_TTL_DEFAULT = 120     # how long a server stays "active"
SERVER_POLL_WARM_TTL_DEFAULT = 600       # how long it stays warm after that
SERVER_POLL_BACKOFF_BASE_DEFAULT = 5
SERVER_POLL_BACKOFF_MAX_DEFAULT = 300

_servers = {}


def _new_server_state():
    """One server's runtime sync state.

    Diagnostics (the doctor page, the freshness badge) read this; only the values that
    another process genuinely needs -- the watch mark and the client fence -- are
    mirrored to Redis, because per-server timings are the fetch role's own business.
    """
    return {
        'next_due': 0.0, 'failures': 0, 'active_until': 0.0, 'warm_until': 0.0,
        'watch_reason': None, 'watched': False,
        'last_fetch_started_at': None, 'last_fetch_finished_at': None,
        'last_fetch_success_at': None, 'last_fetch_error_at': None,
        'last_fetch_duration_ms': None, 'last_snapshot_publish_at': None,
        'last_changed_at': None, 'consecutive_failures': 0, 'backoff_seconds': 0.0,
        'currently_fetching': False, 'last_error': None, 'server_revision': 0,
        'last_outcome': None,
        # Per-server scheduling state. The scheduler dispatches from THIS, not from a
        # batch: one fetch in flight per server, and a nudge that arrives while that
        # fetch is running is remembered instead of being lost with the sleep it
        # interrupted (see mark_wake_pending / note_server_result).
        'inflight': False, 'wake_pending': False,
        'due_since': None, 'last_dispatch_delay_ms': None,
        'last_start_gap_ms': None, 'snapshot_revision': 0,
    }


def server_active_seconds() -> float:
    """Target interval for a server with recent activity (1-3s by design)."""
    return float(_env_int('EVE_SERVER_POLL_ACTIVE_SECONDS', 2, minimum=1))


def server_warm_seconds() -> float:
    """Target interval for a healthy server that recently had real activity.

    WARM exists because HOT/IDLE alone forced a false choice: a panel that just left
    the dashboard either kept a two-second poll forever or dropped straight to 45 s.
    WARM holds the middle band (a PANEL_RENEW, an operator who just left a tab) for
    ``EVE_SERVER_WARM_TTL_SECONDS`` and then releases it to IDLE.
    """
    return float(_env_int('EVE_SERVER_POLL_WARM_SECONDS', 10, minimum=1))


def server_idle_seconds() -> float:
    """Target interval for a server nobody is watching."""
    return float(_env_int('EVE_SERVER_POLL_IDLE_SECONDS', 45, minimum=1))


def idle_jitter_span() -> float:
    """How wide the idle band is spread, so fifty panels do not share one due time.

    Without it every idle panel is scheduled ``idle_seconds`` after the same cycle and
    comes due in the same second, so one fan-out has to read the whole install while a
    panel the operator is looking at waits for the end of the queue. The span is
    normalised to at most the idle interval, keeping the average cadence unchanged.
    """
    return min(_env_float('EVE_SERVER_POLL_IDLE_JITTER_SECONDS', 10.0, minimum=0.0),
               server_idle_seconds())


def _stable_jitter(sid) -> float:
    """A per-server offset in [0, 1), identical in every process and after a restart.

    ``hash()`` is salted per interpreter, which would re-align every panel on every
    restart -- exactly the synchronisation the jitter exists to break -- so the value
    comes from a checksum of the id instead.
    """
    return (zlib.crc32(str(sid).encode('utf-8')) % 1000) / 1000.0


def server_idle_jitter(server_id, interval, *, now=None) -> float:
    """The offset added to an idle panel's next due time (0 for every other band)."""
    span = idle_jitter_span()
    if span <= 0:
        return 0.0
    if interval < server_idle_seconds():
        # HOT, WARM and backoff panels keep their exact schedule: those are the
        # intervals the operator's SLA is written against.
        return 0.0
    sid = _coerce_server_id(server_id)
    if sid is None:
        return 0.0
    return _stable_jitter(sid) * span


def server_active_ttl() -> float:
    return float(_env_int('EVE_SERVER_POLL_ACTIVE_TTL_SECONDS',
                          SERVER_POLL_ACTIVE_TTL_DEFAULT, minimum=5))


def server_warm_ttl() -> float:
    return float(_env_int('EVE_SERVER_WARM_TTL_SECONDS',
                          SERVER_POLL_WARM_TTL_DEFAULT, minimum=5))


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
        state = _new_server_state()
        _servers[sid] = state
    return state


def _watch_ttl_seconds(ttl=None) -> float:
    """How long one watch mark stays valid. Defaults to the active window."""
    if ttl is None:
        return max(5.0, server_active_ttl())
    try:
        return max(0.0, float(ttl))
    except (TypeError, ValueError):
        return max(5.0, server_active_ttl())


def _publish_watch(sid, *, ttl=None, reason='dashboard') -> None:
    """Share one watch mark so the process that owns the loop can see it.

    Two writes, both idempotent: the key carries the reason and owns the expiry, and
    the sorted set is the index the reader walks. The set member is scored by the same
    expiry, so a range read returns exactly the live marks and the stale members are
    pruned in the same round trip.
    """
    window = _watch_ttl_seconds(ttl)
    if window <= 0:
        return
    client = _redis()
    if client is None:
        return
    expires_at = time.time() + window
    try:
        pipe = client.pipeline()
        pipe.set(WATCH_KEY_PREFIX + str(sid), str(reason or 'dashboard')[:32],
                 ex=int(max(1, round(window))))
        pipe.zadd(HOT_SERVERS_KEY, {str(sid): expires_at})
        pipe.hset(WATCH_REASON_KEY, str(sid), str(reason or 'dashboard')[:32])
        pipe.execute()
    except Exception:
        pass


def _legacy_watch_scan(now=None) -> dict:
    """Index watch keys written by a build that predates the sorted set.

    A rolling upgrade leaves the old keys in place with nothing in the index. Rather
    than scanning the keyspace on every read (the cost this index exists to remove),
    scan at most once per ``LEGACY_SCAN_SECONDS`` and populate the index from it.
    """
    global _legacy_scan_at
    moment = time.time() if now is None else float(now)
    if (moment - _legacy_scan_at) < LEGACY_SCAN_SECONDS:
        return {}
    client = _redis()
    if client is None:
        return {}
    _legacy_scan_at = moment
    marks = {}
    try:
        for key in client.scan_iter(match=WATCH_KEY_PREFIX + '*', count=100):
            name = key.decode('utf-8', 'replace') if isinstance(key, bytes) else str(key)
            sid = _coerce_server_id(name[len(WATCH_KEY_PREFIX):])
            if sid is None:
                continue
            try:
                ttl = int(client.ttl(name) or 0)
            except Exception:
                ttl = 0
            if ttl <= 0:
                continue
            marks[sid] = moment + ttl
        if marks:
            try:
                client.zadd(HOT_SERVERS_KEY, {str(sid): at for sid, at in marks.items()})
            except Exception:
                pass
    except Exception:
        return {}
    return marks


def _read_watch_marks(now=None, force=False) -> dict:
    """Currently marked servers as {server_id: reason}, cached briefly.

    Reads the sorted-set index by score, so the cost is O(log n + live marks) instead
    of a keyspace scan plus one GET per key. The mark TTL remains Redis's own expiry.
    """
    moment = time.time() if now is None else float(now)
    with _lock:
        cached = _watch_cache
        if not force and (moment - cached['at']) < REMOTE_CACHE_SECONDS:
            return dict(cached['marks'])
    marks = {}
    client = _redis()
    if client is not None:
        try:
            # Drop members whose TTL already passed, then read the survivors.
            client.zremrangebyscore(HOT_SERVERS_KEY, '-inf', moment)
            members = client.zrangebyscore(HOT_SERVERS_KEY, moment, '+inf')
            ids = []
            for member in members or []:
                sid = _coerce_server_id(
                    member.decode('utf-8', 'replace') if isinstance(member, bytes) else member)
                if sid is not None:
                    ids.append(sid)
            if ids:
                reasons = {}
                try:
                    raw_reasons = client.hmget(
                        WATCH_REASON_KEY, [str(sid) for sid in ids])
                    for sid, value in zip(ids, raw_reasons or []):
                        if isinstance(value, bytes):
                            value = value.decode('utf-8', 'replace')
                        if value:
                            reasons[sid] = str(value)
                except Exception:
                    reasons = {}
                for sid in ids:
                    marks[sid] = reasons.get(sid, 'dashboard')
            else:
                # Nothing indexed: a pre-index install may still hold live keys.
                for sid, expires_at in _legacy_watch_scan(now=moment).items():
                    if expires_at > moment:
                        marks[sid] = 'dashboard'
        except Exception:
            marks = {}
    with _lock:
        # Marks declared in this process are never lost to a failed census.
        for sid in list(_local_watches):
            marks.setdefault(sid, _local_watches[sid])
        cached['at'] = moment
        cached['marks'] = dict(marks)
    return marks


def watched_server_ids(*, now=None) -> list:
    """Every server currently declared on screen, from any process, sorted."""
    moment = time.time() if now is None else float(now)
    with _lock:
        local = {sid for sid, until in _local_watches.items() if until > moment}
    remote = set(_read_watch_marks(now=moment))
    return sorted(local | remote)


def is_server_watched(server_id, *, now=None) -> bool:
    """Whether any process has declared this server as on screen right now."""
    sid = _coerce_server_id(server_id)
    if sid is None:
        return False
    moment = time.time() if now is None else float(now)
    with _lock:
        if float(_local_watches.get(sid) or 0.0) > moment:
            return True
    return sid in _read_watch_marks(now=moment)


def note_watch(sid, *, now=None, ttl=None, reason='dashboard') -> None:
    """Record one watch mark locally, share it, and nudge the fetch process.

    The nudge is what turns "the fetcher will notice within a sleep slice" into "the
    fetcher notices now". Renewals are throttled because a tab renews its marks on
    every poll; a server becoming watched is always published immediately.
    """
    global _last_watch_wake_at
    moment = time.time() if now is None else float(now)
    window = _watch_ttl_seconds(ttl)
    if window <= 0:
        return
    with _lock:
        was_watched = float(_local_watches.get(sid) or 0.0) > moment
        _local_watches[sid] = max(float(_local_watches.get(sid) or 0.0), moment + window)
        _watch_cache['marks'][sid] = str(reason or 'dashboard')[:32]
    _publish_watch(sid, ttl=window, reason=reason)
    if (not was_watched) or (moment - _last_watch_wake_at) >= WATCH_WAKE_THROTTLE_SECONDS:
        _last_watch_wake_at = moment
        publish_wake([sid], reason=reason)


def server_watch_marks(*, now=None) -> dict:
    """Diagnostics: which servers are hot, and where each mark came from."""
    moment = time.time() if now is None else float(now)
    with _lock:
        local = {sid: round(until - moment, 1) for sid, until in _local_watches.items()
                 if until > moment}
    remote = {}
    for sid, reason in _read_watch_marks(now=moment).items():
        remote[str(sid)] = reason
    return {
        'local': {str(sid): ttl for sid, ttl in sorted(local.items())},
        'shared': remote,
        'shared_backend': 'redis' if _redis() is not None else 'process',
    }


def _remote_watch_present(sid, moment) -> bool:
    """Whether any process marked this server, backoff and failures aside."""
    with _lock:
        if float(_local_watches.get(sid) or 0.0) > moment:
            return True
    return sid in _read_watch_marks(now=moment)


def _apply_remote_watch(sid, moment) -> bool:
    """Pull a watched server's next poll in to the active interval.

    This is what makes a dashboard in the web process speed up the loop in the
    background process. A panel in backoff keeps its window: being looked at is not
    a reason to retry a failing panel, the same rule note_server_activity follows.
    """
    state = _servers.get(sid)
    if state is None or state.get('failures'):
        return False
    if not _remote_watch_present(sid, moment):
        return False
    soonest = moment + server_active_seconds()
    if float(state.get('next_due') or 0.0) > soonest:
        state['next_due'] = soonest
    return True


def note_server_activity(server_id, *, now=None, ttl=None, share=True, reason='mutation') -> None:
    """Mark a server as worth watching (the operator looked at it, or Eve wrote to it).

    A panel that just became interesting is pulled in to the active wait as well: a
    panel coming on screen (or just mutated) must not sit out a remaining idle window
    of up to ``EVE_SERVER_POLL_IDLE_SECONDS`` before its first fast poll. A panel in
    backoff keeps its window -- a watch mark is not a reason to retry a failing panel.

    ``share`` publishes the mark and a wake nudge so the OTHER process (the one that
    owns the fetch loop) acts on it now. This is the mutation path's crossing of the
    process boundary: without it, an EVE renew made the panel hot only inside the web
    worker that handled the request, and the fetcher kept polling it on the idle
    cadence -- so the "verified" write-through and the next background read disagreed.

    Leaving HOT does not drop straight to IDLE: the warm window keeps a just-mutated or
    just-watched panel on the middle cadence until ``EVE_SERVER_WARM_TTL_SECONDS``
    elapses, which is what makes a renew settle rather than snap back to 45 s.
    """
    state = _server_state(server_id)
    if state is None:
        return
    moment = time.time() if now is None else float(now)
    window = server_active_ttl() if ttl is None else max(0.0, float(ttl))
    with _lock:
        state['active_until'] = max(state.get('active_until') or 0.0, moment + window)
        state['warm_until'] = max(
            state.get('warm_until') or 0.0, moment + window + server_warm_ttl())
        state['watched'] = True
        if reason:
            state['watch_reason'] = str(reason)[:32]
        if not state.get('failures'):
            soonest = moment + server_active_seconds()
            scheduled = float(state.get('next_due') or 0.0)
            if scheduled and scheduled > soonest:
                state['next_due'] = soonest
    # A read of this panel may already be running: the nudge cannot shorten it, but it
    # must not be lost either, or a mutation landing mid-poll stays invisible for a
    # whole cadence. note_server_result turns the flag into "due immediately". This is
    # local bookkeeping, so it happens whether or not the mark is also published.
    mark_wake_pending(server_id, now=moment)
    if not share:
        return
    sid = _coerce_server_id(server_id)
    if sid is None:
        return
    _publish_watch(sid, ttl=max(window, server_active_ttl()), reason=reason or 'mutation')
    publish_wake([sid], reason=reason or 'mutation')


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
    sid = _coerce_server_id(server_id)
    if sid is not None and _remote_watch_present(sid, moment):
        return server_active_seconds()
    # Nobody is looking right now, but this panel was recently watched or mutated:
    # the middle band. It exists so the hand-off from HOT to IDLE is not a cliff.
    if (state.get('warm_until') or 0.0) > moment:
        return server_warm_seconds()
    return server_idle_seconds()


def server_mode(server_id, *, now=None) -> str:
    """One server's activity class: hot / warm / idle / backoff.

    Diagnostics only -- ``server_interval()`` is what the scheduler acts on. The two
    agree by construction, which is the point: a freshness badge and the poll cadence
    must describe the same server, not two different opinions of it.
    """
    state = _server_state(server_id)
    if state is None:
        return 'idle'
    if state.get('failures'):
        return 'backoff'
    moment = time.time() if now is None else float(now)
    if (state.get('active_until') or 0.0) > moment:
        return 'hot'
    sid = _coerce_server_id(server_id)
    if sid is not None and _remote_watch_present(sid, moment):
        return 'hot'
    if (state.get('warm_until') or 0.0) > moment:
        return 'warm'
    return 'idle'


def note_fetch_started(server_id, *, now=None, queued_at=None) -> float:
    """Record that a panel read began; returns how late it started.

    ``inflight`` is the scheduler's own guard: the invariant is ONE fetch in flight per
    server, so a due panel that is already being read is skipped rather than queued a
    second time. The returned delay is measured against the panel's own schedule
    (``next_due``), which is what makes worker saturation measurable instead of
    invisible: a HOT panel whose start keeps slipping past its cadence shows up as
    queue delay rather than being reported as "2 s polling".
    """
    state = _server_state(server_id)
    if state is None:
        return 0.0
    moment = time.time() if now is None else float(now)
    with _lock:
        previous_start = state.get('last_fetch_started_at')
        scheduled = state.get('next_due')
        due_at = float(scheduled or 0.0)
        state['last_fetch_started_at'] = moment
        state['currently_fetching'] = True
        state['inflight'] = True
        state['due_since'] = None
        # Remember WHICH schedule this read is serving: the next one is measured from it
        # (period-preserving), so a read does not push the whole cadence out by its own
        # duration. While the read runs the panel carries a provisional schedule, so it
        # is neither reported as overdue nor re-dispatched.
        state['dispatched_for'] = due_at if scheduled else moment
        delay = max(0.0, moment - due_at) if due_at else 0.0
        if queued_at is not None:
            try:
                delay = max(delay, moment - float(queued_at))
            except (TypeError, ValueError):
                pass
        state['last_dispatch_delay_ms'] = int(round(delay * 1000))
        if previous_start:
            try:
                state['last_start_gap_ms'] = int(round((moment - float(previous_start)) * 1000))
            except (TypeError, ValueError):
                pass
        try:
            state['next_due'] = moment + server_interval(server_id, now=moment)
        except Exception:
            pass
    return delay


def mark_wake_pending(server_id, *, now=None) -> bool:
    """Remember that this server was nudged while a read of it was already running.

    The scheduler is level-triggered (it dispatches whatever is due), so the only thing
    a nudge during an in-flight read can add is "re-evaluate this one the moment the
    read finishes instead of waiting out its freshly scheduled interval". Dropping that
    would make a mutation land during a poll invisible for a whole cadence.
    """
    state = _server_state(server_id)
    if state is None:
        return False
    with _lock:
        if not state.get('inflight'):
            return False
        state['wake_pending'] = True
        return True


def wake_pending(server_id) -> bool:
    state = _server_state(server_id)
    return bool(state and state.get('wake_pending'))


def inflight(server_id) -> bool:
    state = _server_state(server_id)
    return bool(state and state.get('inflight'))


def consume_wake() -> bool:
    """Test-and-clear this process's wake event.

    The scheduler waits on the thread pool, not only on a sleep, so it needs a way to
    ask "was I nudged since the last time I looked?" without blocking. A plain
    ``is_set`` would leave the event raised and spin the loop.
    """
    if _wake.is_set():
        _wake.clear()
        return True
    return False


def snapshot_sync_health(age_seconds, *, failures=0, backoff_seconds=0.0) -> str:
    """Freshness of a server BLOCK, from the age of its authoritative read.

    The same vocabulary ``sync_health()`` uses for the scheduler's own state, so a
    dashboard reading a published snapshot and the fetcher reading its memory describe
    one server the same way. Reachability is a different question and is answered
    elsewhere: a panel can be online and still be stale.
    """
    if failures:
        return 'backoff' if float(backoff_seconds or 0.0) < server_backoff_max() else 'down'
    if age_seconds is None:
        return 'down'
    try:
        age = max(0.0, float(age_seconds))
    except (TypeError, ValueError):
        return 'down'
    if age <= LIVE_MAX_AGE_SECONDS:
        return 'live'
    if age <= FRESH_MAX_AGE_SECONDS:
        return 'fresh'
    return 'stale'


def note_snapshot_publish(server_id, *, now=None) -> None:
    """Record that this panel's block reached the shared snapshot."""
    state = _server_state(server_id)
    if state is None:
        return
    with _lock:
        state['last_snapshot_publish_at'] = time.time() if now is None else float(now)


def note_server_revisions(server_id, *, server_revision=None,
                          snapshot_revision=None) -> None:
    """Record the revisions a completed read left behind.

    Diagnostics only, but the two answer different questions: the per-server revision is
    the ordering barrier a mutation bumps, and the snapshot revision is the cursor the
    browser delta-syncs from. A panel whose server revision moved after its read was
    discarded is a panel whose displayed block is deliberately older than the mutation.
    """
    state = _server_state(server_id)
    if state is None:
        return
    with _lock:
        if server_revision is not None:
            try:
                state['server_revision'] = int(server_revision)
            except (TypeError, ValueError):
                pass
        if snapshot_revision is not None:
            try:
                state['snapshot_revision'] = int(snapshot_revision)
            except (TypeError, ValueError):
                pass


def client_fence_seconds() -> float:
    return _env_float('EVE_CLIENT_FENCE_SECONDS',
                      float(MUTATION_FENCE_DEFAULT_SECONDS), minimum=0.0)


def baseline_max_age_seconds() -> float:
    """How old a cached traffic view may be before a renewal refuses it.

    A renewal derives the new cap and the "previous" figures of its ledger entry from
    the pre-mutation traffic state, so the age of that state decides whether the
    fast path (reuse the cached row, skip the panel read) is answering the question
    that was asked. A row whose own stamp says it is older than this is read from the
    panel first; a row with no usable stamp is accepted, because the cache is then the
    only state available and refusing it would buy nothing. ``0`` disables the bound
    and restores the old always-cache behaviour, which is only correct on an install
    that polls faster than it renews.
    """
    return _env_float('EVE_RENEW_BASELINE_MAX_AGE_SECONDS', 30.0, minimum=0.0)


def record_client_fence(server_id, email, state, *, now=None, ttl=None) -> bool:
    """Remember a freshly verified client state so a slower read cannot revert it.

    The panel's client-level endpoint reflects an EVE write immediately; its aggregate
    inbound list can lag. Without a fence the next background poll reads the aggregate,
    sees the pre-mutation numbers and writes them over the verified ones -- the renew
    "disappearing" a minute later. The fence is deliberately short lived and is dropped
    as soon as the direct read agrees, so it can never pin a value the panel really did
    change.

    Recorded in this process even without Redis: a single-process install runs the web
    request and the fetch loop in the same interpreter, and the guard must hold there
    too (it is the install that is most likely to be small enough to notice).
    """
    if not email or not isinstance(state, dict):
        return False
    window = client_fence_seconds() if ttl is None else max(0.0, float(ttl))
    if window <= 0:
        return False
    sid = _coerce_server_id(server_id)
    if sid is None:
        return False
    moment = time.time() if now is None else float(now)
    payload = {
        'email': str(email),
        'used_up': state.get('used_up'),
        'used_down': state.get('used_down'),
        'total_bytes': state.get('total_bytes'),
        'expiry_time': state.get('expiry_time'),
        'verified_at': moment,
        'expires_at': moment + window,
    }
    with _lock:
        _local_fences.setdefault(sid, {})[str(email)] = payload
    sync_event('sync.server.fence', server_id=sid, ttl_seconds=int(window))
    client = _redis()
    if client is None:
        return True
    try:
        key = MUTATION_FENCE_PREFIX + str(sid)
        client.hset(key, str(email), json.dumps(payload))
        client.expire(key, int(max(1, round(window))))
    except Exception:
        pass
    return True


def _local_client_fences(sid, moment) -> dict:
    """This process's fences, with the expired ones dropped as they are found."""
    fences = {}
    with _lock:
        rows = dict(_local_fences.get(sid) or {})
    for email, payload in rows.items():
        if float(payload.get('expires_at') or 0.0) <= moment:
            with _lock:
                _local_fences.get(sid, {}).pop(email, None)
            continue
        fences[email] = payload
    return fences


def client_fences(server_id, *, now=None) -> dict:
    """Active fences for one server as {email: state}, pruning the expired ones.

    Merges the shared (Redis) fences with this process's own: the two must agree when
    both exist, and a web process that recorded a fence before Redis was reachable
    must still be protected after it comes back.
    """
    sid = _coerce_server_id(server_id)
    if sid is None:
        return {}
    moment = time.time() if now is None else float(now)
    fences = _local_client_fences(sid, moment)
    client = _redis()
    if client is None:
        return fences
    key = MUTATION_FENCE_PREFIX + str(sid)
    try:
        raw = client.hgetall(key)
    except Exception:
        return fences
    stale = []
    for field, value in (raw or {}).items():
        email = field.decode('utf-8', 'replace') if isinstance(field, bytes) else str(field)
        if isinstance(value, bytes):
            value = value.decode('utf-8', 'replace')
        try:
            payload = json.loads(value or '{}')
        except Exception:
            stale.append(field)
            continue
        if float(payload.get('expires_at') or 0.0) <= moment:
            stale.append(field)
            continue
        fences[email] = payload
    if stale:
        try:
            client.hdel(key, *stale)
        except Exception:
            pass
    return fences


def clear_client_fence(server_id, email=None) -> bool:
    """Drop one fence (the direct read agreed) or all of a server's fences."""
    sid = _coerce_server_id(server_id)
    if sid is None:
        return False
    with _lock:
        if email:
            _local_fences.get(sid, {}).pop(str(email), None)
        else:
            _local_fences.pop(sid, None)
    client = _redis()
    if client is None:
        return True
    key = MUTATION_FENCE_PREFIX + str(sid)
    try:
        if email:
            client.hdel(key, str(email))
        else:
            client.delete(key)
        return True
    except Exception:
        return False


def server_due(server_id, *, now=None) -> bool:
    """True when it is this panel's turn (a server never seen before is always due).

    The staleness ceiling is enforced here rather than by a periodic global sweep: a
    panel that has not been read for ``EVE_REFRESH_MAX_STALENESS_SECONDS`` becomes due
    again even if its own band would have it waiting longer, which is what keeps the
    safety net while the loop itself is purely per-server.
    """
    state = _server_state(server_id)
    if state is None:
        return True
    moment = time.time() if now is None else float(now)
    sid = _coerce_server_id(server_id)
    if sid is not None:
        # A server another process put on screen is due at the active interval, not
        # at the idle window its schedule was still serving.
        _apply_remote_watch(sid, moment)
    if not state.get('next_due'):
        state['due_since'] = moment
        return True
    due = moment >= float(state['next_due'])
    ceiling = max_staleness()
    if not due and ceiling > 0 and not state.get('failures'):
        last_started = state.get('last_fetch_started_at')
        if last_started and (moment - float(last_started)) >= ceiling:
            # Nothing may stay unread past the ceiling: an install whose cadence maths
            # is wrong (a huge idle band, a stuck clock) must still converge.
            due = True
            state['next_due'] = moment
    if due:
        state['due_since'] = min(
            float(state.get('due_since') or float(state['next_due'])), float(state['next_due']))
    else:
        state['due_since'] = None
    return due


def note_server_result(server_id, ok, *, now=None, duration_ms=None, changed=None,
                       error=None) -> float:
    """Record one poll's outcome and schedule the next one; returns the delay applied.

    Also the single writer of the per-server sync state the doctor page and the
    freshness badge read. ``changed`` distinguishes "the panel answered" from "the panel
    answered with new data", which is what separates a fresh snapshot from a live one.
    The returned delay includes the idle jitter, so it is the delay the scheduler
    actually has to wait for rather than the nominal band.
    """
    state = _server_state(server_id)
    if state is None:
        return 0.0
    moment = time.time() if now is None else float(now)
    if ok:
        state['failures'] = 0
        state['consecutive_failures'] = 0
        state['last_fetch_success_at'] = moment
        state['last_error'] = None
        state['last_outcome'] = 'changed' if changed else 'no_change'
        if changed:
            state['last_changed_at'] = moment
            # A panel that actually moved is worth the middle cadence even after the
            # operator's attention has moved on (traffic can keep changing).
            state['warm_until'] = max(
                state.get('warm_until') or 0.0, moment + server_warm_ttl())
        elif state.get('last_changed_at') is None:
            state['last_changed_at'] = moment
    else:
        state['failures'] = int(state.get('failures') or 0) + 1
        state['consecutive_failures'] = state['failures']
        state['last_fetch_error_at'] = moment
        state['last_error'] = str(error)[:200] if error else 'fetch_failed'
        state['last_outcome'] = 'error'
    interval = server_interval(server_id, now=moment)
    # Idle panels are spread over the jitter band; a failing panel keeps its exact
    # backoff (its interval is a retry ladder, not a schedule to smooth out).
    jitter = 0.0 if state.get('failures') else server_idle_jitter(
        server_id, interval, now=moment)
    # Period-preserving, not completion-relative: the next poll is measured from the
    # schedule this read was serving, so a HOT panel whose read takes 300 ms is polled
    # every 2 s (start-to-start) rather than every 2.3 s, and a slow panel does not make
    # the cadence drift. A read that took longer than its interval leaves the schedule in
    # the past, which means "due now" -- the panel is behind and catches up instead of
    # silently skipping a slot. The clamp keeps a stale schedule (a backoff window that
    # was deferred while a manual refresh was running) from pushing the next poll out
    # beyond one full interval.
    served = state.pop('dispatched_for', None)
    try:
        base = float(served) if served and float(served) <= moment else moment
    except (TypeError, ValueError):
        base = moment
    next_due = min(base + interval + jitter, moment + interval + jitter)
    state['next_due'] = max(next_due, moment)
    state['backoff_seconds'] = interval if state.get('failures') else 0.0
    if duration_ms is not None:
        try:
            state['last_fetch_duration_ms'] = int(duration_ms)
        except (TypeError, ValueError):
            pass
    state['last_fetch_finished_at'] = moment
    state['currently_fetching'] = False
    state['inflight'] = False
    # A nudge that arrived while this read was running must not be swallowed by the
    # interval just computed: the panel is still wanted now, so it is due now. A failing
    # panel keeps its backoff window -- being nudged is not a reason to retry sooner.
    if state.pop('wake_pending', False) and not state.get('failures'):
        state['next_due'] = moment
        state['due_since'] = moment
    # Mirror the scheduling row for the processes that do not own this schedule (a web
    # worker rendering the dashboard). One HSET per completed read, and never an input
    # to a scheduling decision -- the memory above stays the authority.
    try:
        publish_server_sync(server_id, now=moment)
    except Exception:
        pass
    sync_event(
        'sync.server.fetch.%s' % ('success' if ok else 'error'),
        # An unchanged poll is the common case at scale; it keeps its specified event
        # name but at DEBUG, so the INFO log stays a record of things that moved.
        level='info' if (not ok or changed) else 'debug',
        server_id=_coerce_server_id(server_id),
        mode=server_mode(server_id, now=moment),
        reason=state.get('watch_reason'),
        duration_ms=state.get('last_fetch_duration_ms'),
        changed=bool(changed) if ok else None,
        next_due_seconds=round(interval + jitter, 3),
        jitter_seconds=(round(jitter, 3) if jitter else None),
        error_type=(str(error)[:80] if error else None),
    )
    return interval + jitter


def next_server_due_in(*, now=None):
    """Seconds until the earliest scheduled poll, or None when nothing is tracked."""
    if not _servers:
        return None
    moment = time.time() if now is None else float(now)
    soonest = min(float(state.get('next_due') or 0.0) for state in _servers.values())
    return max(0.0, soonest - moment)


#: Poll bands in the order a fan-out must read them. A panel the operator is looking
#: at (or that EVE just wrote to) outranks one nobody has opened; the scheduler never
#: queues a HOT panel behind an idle sweep.
_POLL_RANKS = {'hot': 0, 'warm': 1, 'backoff': 2, 'idle': 3}


def server_poll_rank(server_id, *, now=None) -> int:
    """Sort key for one panel: lower is more urgent."""
    return _POLL_RANKS.get(server_mode(server_id, now=now), 3)


def scheduler_plan(rows, *, now=None, limit=None) -> list:
    """The servers this scheduler tick should dispatch, most urgent first.

    ``rows`` is an iterable of server ids or of dicts carrying ``id``. A server in
    flight is never returned twice (one read per panel), a server in backoff is left to
    the fetch layer's own retry window, and the list is ordered by band and then by how
    long each panel has been waiting, so a HOT panel that is overdue outranks an idle
    panel that just came due. ``limit`` caps the result to the free workers; when it is
    None every due server is returned (the caller decides how many it can start).
    """
    moment = time.time() if now is None else float(now)
    candidates = []
    for row in rows or ():
        sid = _coerce_server_id((row or {}).get('id') if isinstance(row, dict) else row)
        if sid is None:
            continue
        state = _servers.get(sid) or {}
        if state.get('inflight'):
            continue
        if not server_due(sid, now=moment):
            continue
        due_since = float(state.get('due_since') or state.get('next_due') or moment)
        candidates.append((server_poll_rank(sid, now=moment), due_since, sid))
    candidates.sort()
    ordered = [sid for _rank, _due, sid in candidates]
    if limit is not None:
        return ordered[:max(0, int(limit))]
    return ordered


def fetch_batch_limit() -> int:
    """How many panels one fan-out may read; 0 means every panel that is due.

    The cycle used to be ``max(2s, length of the whole due set)``: with a hundred
    panels a two-second HOT target was structurally impossible, because the HOT panel
    shared one bounded worker pool with ninety-nine idle ones and the loop only came
    back after the last of them answered. Bounding the batch (and ordering it, see
    ``prioritize_fetch_batch``) makes the cycle short and the deferred idle panels
    simply come due again -- their own interval is what paces them, not the sweep.
    """
    raw = (os.environ.get('EVE_REFRESH_BATCH_SERVERS') or '').strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    try:
        from panel.core import panel_limits
        workers = int(panel_limits.refresh_worker_limit())
    except Exception:
        workers = 5
    return max(1, workers * 2)


def prioritize_fetch_batch(rows, *, now=None, limit=None) -> list:
    """Order one cycle's due panels (HOT first) and bound how many it reads.

    ``rows`` is the scheduler's server dictionaries. Ordering is by band and then by
    how long the panel has been due, so a panel that just became HOT is read in this
    cycle's first worker slot instead of behind everything else that is due. The
    returned list may be shorter than the input; the caller keeps the rest due.
    """
    moment = time.time() if now is None else float(now)
    bounded = fetch_batch_limit() if limit is None else max(0, int(limit))

    def sort_key(row):
        sid = _coerce_server_id((row or {}).get('id') if isinstance(row, dict) else row)
        if sid is None:
            return (3, float('inf'), 0)
        state = _servers.get(sid) or {}
        return (server_poll_rank(sid, now=moment),
                float(state.get('next_due') or 0.0), sid)

    ordered = sorted(list(rows or []), key=sort_key)
    if bounded and len(ordered) > bounded:
        return ordered[:bounded]
    return ordered


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


def note_watched_servers(server_ids, *, now=None, limit=None, reason='dashboard',
                        ttl=None) -> list:
    """Mark the servers a browser says it is rendering as worth watching.

    The dashboard declares what is on screen on every poll, so the fast cadence
    follows the operator's attention instead of the whole install, and it expires on
    its own when the tab closes (nothing renews the mark). The declared set is capped
    so one open tab cannot pin a hundred-server install to a two-second fan-out.

    The marks are SHARED as well as local, because the browser request lands in a web
    process while the loop that has to speed up is the background fetcher: a
    local-only mark made "the panel I am looking at" and "the panel being polled
    fast" two unrelated facts in a split-role install.
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
        # note_watch already publishes the mark and the wake nudge, so the local
        # activity mark must not publish a second time.
        note_server_activity(sid, now=now, share=False, reason=reason)
        note_watch(sid, now=now, ttl=ttl, reason=reason)
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
        # A fence for a panel that is no longer enabled can never be applied again, so
        # it would only sit in memory until the process restarted.
        for sid in list(_local_fences):
            if sid not in keep:
                _local_fences.pop(sid, None)


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
    shared_marks = _read_watch_marks(now=moment)
    rows = {}
    for sid, state in items:
        active_until = float(state.get('active_until') or 0.0)
        warm_until = float(state.get('warm_until') or 0.0)
        rows[str(sid)] = {
            'watch_shared': sid in shared_marks,
            'watch_reason': state.get('watch_reason'),
            'mode': server_mode(sid, now=moment),
            'interval_seconds': round(server_interval(sid, now=moment), 3),
            'due': server_due(sid, now=moment),
            'due_in_seconds': round(max(0.0, server_due_in(sid, now=moment)), 3),
            'failures': int(state.get('failures') or 0),
            'active': active_until > moment,
            'active_for_seconds': round(max(0.0, active_until - moment), 1),
            'warm': warm_until > moment,
            'warm_for_seconds': round(max(0.0, warm_until - moment), 1),
        }
    return rows


#: Freshness thresholds. Deliberately not env-tunable: the health vocabulary is part
#: of the contract a human reads ("Live" must mean something specific), and an install
#: that widened it would be lying in the UI rather than polling faster.
LIVE_MAX_AGE_SECONDS = 6.0
FRESH_MAX_AGE_SECONDS = 60.0


def sync_health(server_id, *, now=None) -> str:
    """live / fresh / stale / backoff / down for one server.

    Reachability and freshness are separate questions, and this answers only the second
    one: a panel can be online and still have a 45-second-old snapshot. "live" is
    reserved for a server whose last successful authoritative read is inside the HOT
    cadence, so the badge never claims real-time for data the loop has not refreshed.
    """
    state = _server_state(server_id)
    if state is None:
        return 'down'
    moment = time.time() if now is None else float(now)
    if state.get('failures'):
        # Distinguish "slowly failing" from "not answering at all": a bounded backoff
        # is still a panel we will reach again, a long one is effectively down.
        backoff = float(state.get('backoff_seconds') or 0.0)
        return 'backoff' if backoff < server_backoff_max() else 'down'
    last_success = state.get('last_fetch_success_at')
    if last_success is None:
        return 'down'
    age = max(0.0, moment - float(last_success))
    if age <= LIVE_MAX_AGE_SECONDS:
        return 'live'
    if age <= FRESH_MAX_AGE_SECONDS:
        return 'fresh'
    return 'stale'


def server_sync_state(server_id, *, now=None) -> dict:
    """Full per-server sync diagnostics for /api/doctor and the freshness badge.

    Carries no credential, token, email or panel address: ids, modes, ages and
    revisions only, so it is safe to surface on an operator-facing diagnostics page.
    """
    sid = _coerce_server_id(server_id)
    state = _server_state(server_id)
    if state is None or sid is None:
        return {}
    moment = time.time() if now is None else float(now)

    def _age(value):
        if value is None:
            return None
        return round(max(0.0, moment - float(value)), 3)

    return {
        'server_id': sid,
        'mode': server_mode(sid, now=moment),
        'watched': bool(state.get('watched')) or is_server_watched(sid, now=moment),
        'watch_reason': state.get('watch_reason'),
        'poll_interval_seconds': round(server_interval(sid, now=moment), 3),
        'last_fetch_age_seconds': _age(state.get('last_fetch_finished_at')),
        'last_success_age_seconds': _age(state.get('last_fetch_success_at')),
        'last_error_age_seconds': _age(state.get('last_fetch_error_at')),
        'last_fetch_duration_ms': state.get('last_fetch_duration_ms'),
        'last_publish_age_seconds': _age(state.get('last_snapshot_publish_at')),
        'last_changed_age_seconds': _age(state.get('last_changed_at')),
        'next_due_in_seconds': round(max(0.0, server_due_in(sid, now=moment)), 3),
        # Absolute stamps next to the ages: an operator reading the page at 14:03 needs to
        # know WHEN, not only "37 s ago", and a log line can be matched against them.
        'last_fetch_started_at': state.get('last_fetch_started_at'),
        'last_fetch_finished_at': state.get('last_fetch_finished_at'),
        'last_success_at': state.get('last_fetch_success_at'),
        'last_publish_at': state.get('last_snapshot_publish_at'),
        'next_due_at': (round(float(state['next_due']), 3) if state.get('next_due') else None),
        'consecutive_failures': int(state.get('consecutive_failures') or 0),
        'backoff_seconds': float(state.get('backoff_seconds') or 0.0),
        'currently_fetching': bool(state.get('currently_fetching')),
        # Per-server scheduling evidence: whether a read is running right now, whether a
        # nudge was held back by that read, and how late this server's last dispatch was
        # against its own schedule (the number that exposes worker saturation).
        'inflight': bool(state.get('inflight')),
        'wake_pending': bool(state.get('wake_pending')),
        'scheduler_queue_delay_ms': state.get('last_dispatch_delay_ms'),
        'last_start_gap_ms': state.get('last_start_gap_ms'),
        'server_revision': int(state.get('server_revision') or 0),
        'snapshot_revision': int(state.get('snapshot_revision') or 0),
        'last_outcome': state.get('last_outcome'),
        'last_error': state.get('last_error'),
        'sync_health': sync_health(sid, now=moment),
    }


#: One published row per server for the processes that do NOT own the schedule. The
#: fetcher is the only writer; a web worker reads it to answer "is what I am rendering
#: live?" without pretending it knows the fetcher's memory.
SERVER_SYNC_KEY = 'eve:refresh:server_sync'
SERVER_SYNC_TTL = 900
_server_sync_cache = {'at': 0.0, 'rows': {}}
SERVER_SYNC_CACHE_SECONDS = 2.0


def publish_server_sync(server_id, *, now=None) -> bool:
    """Mirror one server's scheduling row to the shared backend.

    Written on every completed fetch (one HSET), so the web process can render the
    freshness/mode of the data it is serving. This is a report, never an input to
    scheduling: the fetcher's own memory stays the authority, and a missing row means
    "unknown", which the reader must not turn into "live".
    """
    sid = _coerce_server_id(server_id)
    if sid is None:
        return False
    client = _redis()
    if client is None:
        return False
    state = _servers.get(sid) or {}
    moment = time.time() if now is None else float(now)
    row = {
        'mode': server_mode(sid, now=moment),
        'watched': bool(state.get('watched')) or is_server_watched(sid, now=moment),
        'watch_reason': state.get('watch_reason'),
        'inflight': bool(state.get('inflight')),
        'wake_pending': bool(state.get('wake_pending')),
        'last_success_at': state.get('last_fetch_success_at'),
        'last_fetch_duration_ms': state.get('last_fetch_duration_ms'),
        'last_publish_at': state.get('last_snapshot_publish_at'),
        'next_due': state.get('next_due'),
        'queue_delay_ms': state.get('last_dispatch_delay_ms'),
        'last_start_gap_ms': state.get('last_start_gap_ms'),
        'failures': int(state.get('consecutive_failures') or 0),
        'backoff_seconds': float(state.get('backoff_seconds') or 0.0),
        'sync_health': sync_health(sid, now=moment),
        'updated_at': moment,
    }
    try:
        client.hset(SERVER_SYNC_KEY, str(sid), json.dumps(row))
        client.expire(SERVER_SYNC_KEY, SERVER_SYNC_TTL)
        # Keep the reader's short cache coherent with what was just published.
        with _lock:
            _server_sync_cache['rows'][str(sid)] = row
        return True
    except Exception:
        return False


def shared_server_sync(*, now=None, force=False) -> dict:
    """The published per-server rows, cached briefly, keyed by server id string."""
    moment = time.time() if now is None else float(now)
    with _lock:
        cached = _server_sync_cache
        if not force and (moment - cached['at']) < SERVER_SYNC_CACHE_SECONDS:
            return dict(cached['rows'])
    client = _redis()
    rows = {}
    if client is not None:
        try:
            raw = client.hgetall(SERVER_SYNC_KEY)
        except Exception:
            raw = None
        for field, value in (raw or {}).items():
            key = field.decode('utf-8', 'replace') if isinstance(field, bytes) else str(field)
            if isinstance(value, bytes):
                value = value.decode('utf-8', 'replace')
            try:
                rows[key] = json.loads(value or '{}')
            except Exception:
                continue
    with _lock:
        _server_sync_cache['at'] = moment
        _server_sync_cache['rows'] = dict(rows)
    return rows


def server_sync_report(server_ids=None, *, now=None) -> dict:
    """Per-server scheduling rows for the servers actually being rendered.

    Local state wins where this process owns the schedule (the fetcher); otherwise the
    published row is used. A server present in neither is omitted rather than invented.
    """
    moment = time.time() if now is None else float(now)
    with _lock:
        local_ids = set(_servers.keys())
    wanted = None
    if server_ids is not None:
        wanted = set()
        for value in server_ids:
            sid = _coerce_server_id(value)
            if sid is not None:
                wanted.add(sid)
    published = shared_server_sync(now=moment)
    report = {}
    for sid in sorted(local_ids | {_coerce_server_id(k) for k in published if _coerce_server_id(k) is not None}):
        if wanted is not None and sid not in wanted:
            continue
        if sid in local_ids:
            report[str(sid)] = server_sync_state(sid, now=moment)
            continue
        row = published.get(str(sid)) or {}
        report[str(sid)] = _public_sync_row(sid, row, now=moment)
    return report


def _public_sync_row(sid, row, *, now) -> dict:
    """Turn a published row into the same shape the local one has (ages, not stamps)."""
    def _age(stamp):
        if not stamp:
            return None
        try:
            return round(max(0.0, float(now) - float(stamp)), 3)
        except (TypeError, ValueError):
            return None

    next_due = row.get('next_due')
    return {
        'server_id': sid,
        'mode': row.get('mode') or 'idle',
        'watched': bool(row.get('watched')),
        'watch_reason': row.get('watch_reason'),
        'last_success_age_seconds': _age(row.get('last_success_at')),
        'last_fetch_duration_ms': row.get('last_fetch_duration_ms'),
        'last_publish_age_seconds': _age(row.get('last_publish_at')),
        'next_due_in_seconds': (round(max(0.0, float(next_due) - float(now)), 3)
                                if next_due else None),
        'next_due_at': (round(float(next_due), 3) if next_due else None),
        'consecutive_failures': int(row.get('failures') or 0),
        'backoff_seconds': float(row.get('backoff_seconds') or 0.0),
        'inflight': bool(row.get('inflight')),
        'wake_pending': bool(row.get('wake_pending')),
        'scheduler_queue_delay_ms': row.get('queue_delay_ms'),
        'last_start_gap_ms': row.get('last_start_gap_ms'),
        'sync_health': row.get('sync_health') or 'down',
        'source': 'published',
    }


def sync_summary(*, now=None) -> dict:
    """Aggregate sync health: the counters a doctor page and metrics expose."""
    moment = time.time() if now is None else float(now)
    with _lock:
        ids = sorted(_servers.keys())
    counts = {'hot': 0, 'warm': 0, 'idle': 0, 'backoff': 0}
    health = {'live': 0, 'fresh': 0, 'stale': 0, 'backoff': 0, 'down': 0}
    soonest = None
    staleness = 0.0
    tracked = 0
    for sid in ids:
        mode = server_mode(sid, now=moment)
        counts[mode] = counts.get(mode, 0) + 1
        state = _servers.get(sid) or {}
        last_success = state.get('last_fetch_success_at')
        if last_success is None:
            # Never successfully read: it counts as down, and never as fresh.
            health['down'] += 1
            continue
        tracked += 1
        staleness = max(staleness, max(0.0, moment - float(last_success)))
        health[sync_health(sid, now=moment)] += 1
        due = server_due_in(sid, now=moment)
        soonest = due if soonest is None else min(soonest, due)
    return {
        'tracked_servers': tracked,
        'modes': counts,
        'health': health,
        'hot_servers': counts['hot'],
        'warm_servers': counts['warm'],
        'backoff_servers': counts['backoff'],
        'max_staleness_seconds': round(staleness, 3),
        'next_due_in_seconds': None if soonest is None else round(max(0.0, soonest), 3),
        'watch_shared_backend': 'redis' if _redis() is not None else 'process',
        'wake_listener': wake_listener_active(),
        'poll_intervals': {
            'hot': server_active_seconds(),
            'warm': server_warm_seconds(),
            'idle': server_idle_seconds(),
            'backoff_base': server_backoff_base(),
            'backoff_max': server_backoff_max(),
        },
    }
