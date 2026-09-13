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

import sqlalchemy as sa

logger = logging.getLogger(__name__)

_PER_PROCESS_LOCKS = {}
_PER_PROCESS_GUARD = threading.Lock()

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


def _engine_or_default(engine):
    if engine is not None:
        return engine
    from panel.extensions import db  # deferred: avoids an import cycle
    return db.engine


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
            except Exception:
                logger.exception("[lock] advisory lock attempt failed for %s", resource)
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    connection = None
                yield None
                return
            if not acquired:
                try:
                    connection.close()
                except Exception:
                    pass
                connection = None
                yield None
                return
        owner = "%s:%x" % (threading.current_thread().name, id(local))
        try:
            yield owner
        finally:
            if acquired and connection is not None:
                try:
                    connection.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"),
                        {"key": advisory_key(resource)},
                    )
                except Exception:
                    # The connection is going away; PostgreSQL releases the lock
                    # with it. Never mask the caller's own exception with this.
                    logger.warning(
                        "[lock] advisory unlock failed for %s; the lock is",
                        resource, exc_info=True)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        local.release()


def lock_is_cross_process(engine=None) -> bool:
    """True when :func:`resource_lock` is backed by the database on this engine."""
    try:
        return _engine_or_default(engine).dialect.name == "postgresql"
    except Exception:
        return False
