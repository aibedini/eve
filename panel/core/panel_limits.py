"""Bounded, coalesced panel access.

Requests and background jobs all talk to the X-UI panels. Without a bound, a burst
of callers opens as many simultaneous panel sessions as the process has threads,
and two callers asking for the same server fetch identical data twice. This module
adds two primitives used by the fetch paths:

* coalesce(key): the first caller for a key runs the work, everyone else waits for
  its result instead of fetching again (single flight);
* a process-wide panel concurrency cap (EVE_PANEL_CONCURRENCY, default 12) that the
  leader of a flight and every background fan-out worker shares, so the total
  number of simultaneous panel fetches is bounded no matter how many requests
  arrive.

Callers must honour the slot: inside a coalesce block, run the work only when
slot.leader is true and assign the result to slot.result; followers get the
leader's slot.result.

Configuration:

* EVE_PANEL_CONCURRENCY - maximum simultaneous panel fetches (default 12);
* EVE_PANEL_FETCH_WAIT_SECONDS - how long a caller waits for its turn (default 10);
* EVE_REFRESH_WORKERS - worker threads for the background fan-out (default 5).
"""
import os
import threading
from contextlib import contextmanager

CONCURRENCY_ENV = 'EVE_PANEL_CONCURRENCY'
WAIT_ENV = 'EVE_PANEL_FETCH_WAIT_SECONDS'
WORKERS_ENV = 'EVE_REFRESH_WORKERS'

_lock = threading.RLock()
_flights = {}
_semaphore = None
_semaphore_limit = None
_counters = {
    'started': 0,
    'coalesced': 0,
    'completed': 0,
    'rejected': 0,
    'timed_out': 0,
    'in_flight': 0,
    'max_in_flight': 0,
}


class PanelBusy(RuntimeError):
    """No panel slot or flight result became available within the caller's budget."""


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


def concurrency_limit() -> int:
    return _env_int(CONCURRENCY_ENV, 12)


def fetch_wait_seconds() -> float:
    return _env_float(WAIT_ENV, 10.0)


def refresh_worker_limit() -> int:
    return _env_int(WORKERS_ENV, 5)


def panel_metrics() -> dict:
    with _lock:
        snapshot = dict(_counters)
    snapshot.update({
        'concurrency_limit': concurrency_limit(),
        'fetch_wait_seconds': fetch_wait_seconds(),
        'refresh_workers': refresh_worker_limit(),
        'active_flights': len(_flights),
    })
    return snapshot


def reset_panel_metrics() -> None:
    with _lock:
        for key in _counters:
            _counters[key] = 0


def _semaphore_for(limit):
    global _semaphore, _semaphore_limit
    with _lock:
        if _semaphore is None or _semaphore_limit != limit:
            _semaphore = threading.BoundedSemaphore(limit)
            _semaphore_limit = limit
        return _semaphore


def _enter_flight(key):
    with _lock:
        flight = _flights.get(key)
        if flight is None:
            flight = _Flight()
            _flights[key] = flight
            _counters['started'] += 1
            return flight, True
        _counters['coalesced'] += 1
        return flight, False


def _leave_flight(key, flight):
    with _lock:
        _flights.pop(key, None)
        _counters['completed'] += 1
        _counters['in_flight'] = max(0, _counters['in_flight'] - 1)
    flight.event.set()


class _Flight:
    __slots__ = ('event', 'result', 'error')

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class PanelSlot:
    """What coalesce yields: the leader flag and the shared result."""

    __slots__ = ('leader', 'result')

    def __init__(self, leader, result=None):
        self.leader = leader
        self.result = result


@contextmanager
def panel_slot(wait_seconds=None):
    """Bound one panel fetch against the process-wide concurrency cap."""
    budget = fetch_wait_seconds() if wait_seconds is None else wait_seconds
    semaphore = _semaphore_for(concurrency_limit())
    acquired = semaphore.acquire(timeout=budget)
    if not acquired:
        with _lock:
            _counters['rejected'] += 1
        raise PanelBusy('panel concurrency limit reached')
    with _lock:
        _counters['in_flight'] += 1
        _counters['max_in_flight'] = max(_counters['max_in_flight'], _counters['in_flight'])
    try:
        yield
    finally:
        with _lock:
            _counters['in_flight'] = max(0, _counters['in_flight'] - 1)
        try:
            semaphore.release()
        except ValueError:
            pass


@contextmanager
def coalesce(key, *, wait_seconds=None, acquire_slot=True):
    """Single-flight wrapper around a panel fetch.

    Yields a PanelSlot. The leader runs the work (holding a panel_slot when
    acquire_slot is true); a follower waits for the leader's result and must skip
    the work. A follower that times out raises PanelBusy; a leader that raised
    re-raises the same error for its followers.
    """
    name = str(key)
    flight, leader = _enter_flight(name)
    budget = fetch_wait_seconds() if wait_seconds is None else wait_seconds

    if not leader:
        if not flight.event.wait(timeout=budget):
            with _lock:
                _counters['timed_out'] += 1
            raise PanelBusy('another panel fetch for %s is still running' % name)
        if flight.error is not None:
            raise flight.error
        yield PanelSlot(False, flight.result)
        return

    with _lock:
        _counters['in_flight'] += 1
        _counters['max_in_flight'] = max(_counters['max_in_flight'], _counters['in_flight'])
    slot = PanelSlot(True)
    semaphore = None
    try:
        if acquire_slot:
            semaphore = _semaphore_for(concurrency_limit())
            if not semaphore.acquire(timeout=budget):
                with _lock:
                    _counters['rejected'] += 1
                raise PanelBusy('panel concurrency limit reached')
        yield slot
        flight.result = slot.result
    except BaseException as exc:
        flight.error = exc
        raise
    finally:
        if semaphore is not None:
            try:
                semaphore.release()
            except ValueError:
                pass
        _leave_flight(name, flight)
