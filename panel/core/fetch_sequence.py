"""Monotonic per-server fetch tickets: the ordering barrier for panel reads.

The problem this solves
-----------------------
A panel read takes seconds and several workers read the same panel: the periodic
fan-out, a manual "refresh now", the targeted recheck a depletion candidate
triggers, and the read-back after a mutation. Nothing guarantees that the results
are APPLIED in the order the reads STARTED, and the snapshot cannot tell:

* telemetry_updated_at is stamped when a result is applied
  (_recompute_cached_client), not when the read began, so a slow read that
  returns late looks NEWER than the fast read that already landed;
* X-UI reports usage counters, not an observation timestamp, so the panel cannot
  order two of its own responses either.

Ordering panel reads by arrival time is therefore impossible, and the cost is not
cosmetic: a stale "2 GB remaining" applied after a fresh "ended" reverts the
ledger, and the next fresh read then opens a SECOND depletion event for one
logical depletion -- a duplicate SMS.

The mechanism
-------------
begin() takes a ticket before the read; accept() is a compare-and-set that refuses
any result whose ticket is older than the newest ticket already applied for that
server. Tickets are handed out by Redis when it is configured, because the writers
are different processes (web, background fetcher, CLI); without Redis the module
keeps the same semantics per process, which is the correct answer for a
single-process install and never silently reorders within one process.

Tickets are ordered by issue, never by completion, so a read that never returns
cannot stall the sequence: the watermark only moves forward on apply.
"""
from __future__ import annotations

import logging
import threading

from panel.core.redis_client import get_redis

logger = logging.getLogger(__name__)

SEQUENCE_KEY_PREFIX = 'eve:fetch:seq:'
APPLIED_KEY_PREFIX = 'eve:fetch:applied:'
#: Watermarks are small integers; the TTL only exists so a deleted server does not
#: leave a key behind forever.
WATERMARK_TTL_SECONDS = 86400

# The compare-and-set has to be one round trip: two workers that read-then-write
# independently could both accept, which is exactly the race this module exists to
# close. Redis executes a script without interleaving, which is the lock.
_ACCEPT_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current and tonumber(current) >= tonumber(ARGV[1]) then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

_lock = threading.Lock()
_counters = {}
_watermarks = {}


def _coerce(server_id):
    try:
        return int(server_id)
    except (TypeError, ValueError):
        return None


def begin(server_id) -> int:
    """Take a ticket for a read that is about to start.

    Always increasing, per server. The caller hands the ticket back to
    accept() when (and if) the read produces something to apply.
    """
    sid = _coerce(server_id)
    if sid is None:
        return 0
    client = get_redis()
    if client is not None:
        try:
            return int(client.incr(SEQUENCE_KEY_PREFIX + str(sid)))
        except Exception:
            logger.debug("fetch_sequence: Redis INCR failed for %s", sid, exc_info=True)
    with _lock:
        _counters[sid] = int(_counters.get(sid) or 0) + 1
        return _counters[sid]


def accept(server_id, ticket) -> bool:
    """Claim the right to apply a result; False means a newer read already won.

    A missing or zero ticket is accepted: the monotonic guard only makes a claim
    about reads that took a ticket, and refusing unticketed results would silently
    drop every path that does not take one.
    """
    sid = _coerce(server_id)
    try:
        value = int(ticket)
    except (TypeError, ValueError):
        value = 0
    if sid is None or value <= 0:
        return True
    client = get_redis()
    if client is not None:
        try:
            accepted = client.eval(_ACCEPT_SCRIPT, 1,
                                   APPLIED_KEY_PREFIX + str(sid), value,
                                   WATERMARK_TTL_SECONDS)
            return bool(int(accepted or 0))
        except Exception:
            logger.debug("fetch_sequence: Redis CAS failed for %s", sid, exc_info=True)
    with _lock:
        if value <= int(_watermarks.get(sid) or 0):
            return False
        _watermarks[sid] = value
        return True


def last_accepted(server_id) -> int:
    """The newest ticket already applied for this server (0 when none)."""
    sid = _coerce(server_id)
    if sid is None:
        return 0
    client = get_redis()
    if client is not None:
        try:
            return int(client.get(APPLIED_KEY_PREFIX + str(sid)) or 0)
        except Exception:
            logger.debug("fetch_sequence: Redis GET failed for %s", sid, exc_info=True)
    with _lock:
        return int(_watermarks.get(sid) or 0)


def reset(server_id=None) -> None:
    """Forget local state (tests, and a fresh process). Never clears Redis: the
    shared watermark is a cross-process fact, not this process cache."""
    with _lock:
        if server_id is None:
            _counters.clear()
            _watermarks.clear()
        else:
            sid = _coerce(server_id)
            _counters.pop(sid, None)
            _watermarks.pop(sid, None)


def status() -> dict:
    """Diagnostics for the doctor page."""
    client = get_redis()
    with _lock:
        counters = dict(_counters)
        watermarks = dict(_watermarks)
    return {
        'backend': 'redis' if client is not None else 'process',
        'tracked_servers': len(counters),
        'local_counters': counters,
        'local_watermarks': watermarks,
    }
