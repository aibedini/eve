"""PostgreSQL advisory locks for cross-process critical sections.

Why not `threading.Lock`
-----------------------
A process-local lock only serialises callers inside ONE interpreter. Eve runs
gunicorn workers plus dedicated worker processes, so "is a backup already running
for server 7?" cannot be answered in memory: each process answers "no" and acts.
PostgreSQL advisory locks give the guarantee the directive asks for, and they do
it without adding a Redis dependency to a correctness path:

* the lock lives in the database session, so a crashed process or a dropped
  connection releases it automatically - no TTL, no renewal, no "the holder may
  still be alive but its lease expired" race;
* `pg_try_advisory_lock` is non-blocking, so a duplicate request reports
  ALREADY_RUNNING instead of queueing behind an unknown wait;
* the key space is ours to define, so the lock is per resource
  (`xui_backup:7`), not global: server 7 and server 8 back up concurrently.

Connection ownership (the subtle part)
--------------------------------------
A SESSION-level advisory lock is released when the *connection* ends, so the
connection must stay checked out for the whole critical section. If the lock were
taken on the ORM session, the next `commit()`/`rollback()` would return that
connection to the pool while the lock was still conceptually held, and the pool
would then hand a locked connection to unrelated work. This module therefore
takes a DEDICATED connection from the engine and keeps it, with an explicit
`close()` in a finally block, for the lock lifetime.

Cost: one pooled connection per active lock holder. Callers hold one lock at a
time, so the extra demand is one connection per worker, not one per task.

SQLite has no advisory locks. There the module degrades to a per-process lock;
`lock_is_cross_process()` lets a caller log the reduced guarantee instead of
believing it is protected.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass

import sqlalchemy as sa

logger = logging.getLogger(__name__)

_PER_PROCESS_LOCKS = {}
_PER_PROCESS_GUARD = threading.Lock()

# Why the most recent attempt on a resource did not acquire it. The context
# manager communicates through an owner token (so `with ... as owner` stays
# simple), and this side channel carries the REASON to `resource_lock_attempt`.
_LAST_OUTCOME = {}
_LAST_REASON = {}

ACQUIRED = 'ACQUIRED'
CONTENDED = 'CONTENDED'
LOCK_UNAVAILABLE = 'LOCK_UNAVAILABLE'

# Advisory lock keys are int64. Eve keeps its own namespace so these cannot
# collide with the ad-hoc keys used elsewhere in the project.
_LOCK_NAMESPACE = "eve:lock"


def advisory_key(resource: str) -> int:
    """Stable int64 key for one resource name.

    `hashlib` rather than `hash()`: Python randomises string hashes per process,
    so `hash()` would produce a different key in every worker and the lock would
    never collide - which is exactly the failure mode being fixed.
    """
    digest = hashlib.blake2b(
        ("%s:%s" % (_LOCK_NAMESPACE, resource)).encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big", signed=True)


def _process_lock(resource: str) -> threading.Lock:
    with _PER_PROCESS_GUARD:
        lock = _PER_PROCESS_LOCKS.get(resource)
        if lock is None:
            lock = threading.Lock()
            _PER_PROCESS_LOCKS[resource] = lock
        return lock


def _discard(connection) -> None:
    """Return a connection to the pool WITHOUT leaving lock state behind.

    `close()` alone is not enough on a pooled connection: SQLAlchemy may return
    the same physical PostgreSQL session to the pool, and a session-level
    advisory lock that was never released would then travel with it into
    unrelated work. `invalidate()` guarantees the next checkout is a new
    session."""
    try:
        connection.invalidate()
    except Exception:
        pass
    try:
        connection.close()
    except Exception:
        pass


def _release_advisory(connection, resource: str) -> None:
    """Unlock the advisory key and verify it, invalidating on any doubt.

    A skipped or failed unlock must never leave a lock on a pooled session, so
    the connection is discarded either way. Never masks the caller's own
    exception."""
    key = advisory_key(resource)
    released = False
    try:
        released = bool(connection.execute(
            sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key},
        ).scalar())
    except Exception:
        logger.warning(
            "[lock] advisory unlock raised for %s", resource, exc_info=True)
    if not released:
        # Either the unlock failed or PostgreSQL answered false (this session
        # did not hold the key). Both mean the pool may hold locked state.
        logger.warning(
            "[lock] advisory unlock did not confirm for %s; discarding the connection"
            " instead of returning it to the pool", resource)
    _discard(connection)


def _engine_or_default(engine):
    if engine is not None:
        return engine
    from panel.extensions import db  # deferred: avoids an import cycle
    return db.engine


def confirm_held(cursor, key: int) -> bool:
    """True when this session still holds `key` in the advisory lock table.

    A session-level advisory lock disappears with its CONNECTION, and the
    connection can die while the python process keeps running. Re-checking the
    lock table turns that silent loss into a decision the caller can act on
    instead of uploading an unguarded duplicate."""
    try:
        row = cursor.execute(
            sa.text('SELECT 1 FROM pg_locks WHERE locktype = :t AND '
                    'objid = :o AND granted'),
            {'t': 'advisory', 'o': key & 0xFFFFFFFF},
        ).first()
        return row is not None
    except Exception:
        # Cannot prove ownership -> treat as lost. Aborting one backup is
        # cheaper than a duplicate panel download and upload.
        return False


@dataclass(frozen=True)
class LockAttempt:
    """The outcome of one lock attempt: acquired, contended, or unavailable.

    `CONTENDED` and `LOCK_UNAVAILABLE` are DIFFERENT facts and must not collapse
    into one falsy value: "another worker holds this resource" is normal and
    coalesces the caller, while "the lock infrastructure is down" is a
    dependency failure the caller must report as such. Treating a database
    outage as ALREADY_RUNNING is how an operator ends up chasing a phantom
    concurrent backup."""

    state: str          # ACQUIRED | CONTENDED | LOCK_UNAVAILABLE
    owner: str | None = None
    reason: str | None = None
    cross_process: bool = False

    @property
    def acquired(self) -> bool:
        return self.state == ACQUIRED


def still_held(cursor, resource: str) -> bool:
    """Whether `cursor`'s session still owns `resource` (see :func:`confirm_held`)."""
    return confirm_held(cursor, advisory_key(resource))


@contextmanager
def resource_lock_attempt(resource: str, *, engine=None):
    """Like :func:`resource_lock` but yields a typed :class:`LockAttempt`.

    Callers that only care whether they may proceed keep using
    :func:`resource_lock`; callers that must distinguish "busy" from "broken"
    use this."""
    with resource_lock(resource, engine=engine) as owner:
        if owner is not None:
            try:
                cross = lock_is_cross_process(engine)
            except Exception:
                cross = False
            yield LockAttempt(ACQUIRED, owner=owner, cross_process=cross)
            return
        yield LockAttempt(_LAST_OUTCOME.get(resource, CONTENDED),
                          reason=_LAST_REASON.get(resource))


@contextmanager
def resource_lock(resource: str, *, engine=None):
    """Yield an owner token when this process may act on `resource`, else None.

    Usage::

        with resource_lock("xui_backup:%d" % server.id) as owner:
            if owner is None:
                return coalesced()
            ...critical section...

    The yield value is an opaque holder identifier for logs and audit rows, or
    ``None`` when the resource is already locked. Exceptions propagate; the lock
    and the dedicated connection are released on every exit path.
    """
    engine = _engine_or_default(engine)

    local = _process_lock(resource)
    if not local.acquire(blocking=False):
        _LAST_OUTCOME[resource] = CONTENDED
        _LAST_REASON[resource] = 'process_local_lock_held'
        yield None
        return

    connection = None
    acquired = False
    try:
        if engine.dialect.name == "postgresql":
            try:
                connection = engine.connect()
                acquired = bool(connection.execute(
                    sa.text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": advisory_key(resource)},
                ).scalar())
            except Exception as exc:
                # The lock INFRASTRUCTURE failed (database down, pool exhausted).
                # That is not "someone else is running it" and must be reported as
                # a dependency failure.
                logger.exception("[lock] advisory lock attempt failed for %s", resource)
                _LAST_OUTCOME[resource] = LOCK_UNAVAILABLE
                _LAST_REASON[resource] = 'lock_backend_error:%s' % type(exc).__name__
                if connection is not None:
                    _discard(connection)
                    connection = None
                yield None
                return
            if not acquired:
                _LAST_OUTCOME[resource] = CONTENDED
                _LAST_REASON[resource] = 'advisory_lock_held_elsewhere'
                _discard(connection)
                connection = None
                yield None
                return
        _LAST_OUTCOME[resource] = ACQUIRED
        _LAST_REASON[resource] = None
        owner = "%s:%x" % (threading.current_thread().name, id(local))
        try:
            yield owner
        finally:
            if acquired and connection is not None:
                _release_advisory(connection, resource)
    finally:
        if connection is not None:
            _discard(connection)
        local.release()


def lock_is_cross_process(engine=None) -> bool:
    """True when :func:`resource_lock` is backed by the database on this engine."""
    try:
        return _engine_or_default(engine).dialect.name == "postgresql"
    except Exception:
        return False
