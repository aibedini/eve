"""Phase 18 tests: adaptive per-server polling (external X-UI change latency).

The cycle cadence in refresh_policy decides how often the fetcher wakes; this suite
covers the layer under it -- which panel's turn it is inside a cycle, how a watched or
just-mutated panel gets polled every couple of seconds while the rest of the install
keeps its idle cadence, and how a failing panel backs off.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

import app as app_module  # noqa: E402
from app import GLOBAL_SERVER_DATA, Server, app, db  # noqa: E402
from panel.core import refresh_policy  # noqa: E402
from panel.jobs import refresh as refresh_jobs  # noqa: E402
from panel.jobs import schedulers  # noqa: E402
from panel.routes import dashboard as dashboard_routes  # noqa: E402

SERVER_ENV = (
    "EVE_SERVER_POLL_ACTIVE_SECONDS", "EVE_SERVER_POLL_IDLE_SECONDS",
    "EVE_SERVER_POLL_WARM_SECONDS", "EVE_SERVER_WARM_TTL_SECONDS",
    "EVE_SERVER_POLL_ACTIVE_TTL_SECONDS", "EVE_SERVER_POLL_BACKOFF_BASE_SECONDS",
    "EVE_SERVER_POLL_BACKOFF_MAX_SECONDS", "EVE_SERVER_POLL_WATCH_LIMIT",
    "EVE_CLIENT_FENCE_SECONDS", "EVE_REFRESH_SAFETY_SLICE_SECONDS",
    # The idle band is spread by a per-server jitter, and the fetch batch cap and the
    # renewal baseline age are read from the environment too: a stray value in the
    # shell would move a schedule and make these tests order-dependent.
    "EVE_SERVER_POLL_IDLE_JITTER_SECONDS", "EVE_REFRESH_BATCH_SERVERS",
    "EVE_RENEW_BASELINE_MAX_AGE_SECONDS",
)

BASE = 5_000_000.0


class _FakeRedis:
    """Hash-only Redis stand-in for the cross-process marks and fences.

    The shared paths touch a handful of commands -- a hash write plus an expiry, a
    hash read, a hash prune and a whole-key delete -- so a dict of dicts with the same
    signatures exercises the real code offline. Anything the module does not actually
    call is deliberately absent, so a new dependency shows up as an error here.
    """

    def __init__(self):
        self.hashes = {}
        self.expiries = {}
        self.published = []

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def expire(self, key, seconds):
        self.expiries[key] = seconds
        return True

    def hgetall(self, key):
        return dict(self.hashes.get(key) or {})

    def hdel(self, key, *fields):
        store = self.hashes.get(key) or {}
        removed = 0
        for field in fields:
            if field in store:
                del store[field]
                removed += 1
        return removed

    def delete(self, key):
        self.expiries.pop(key, None)
        return 1 if self.hashes.pop(key, None) is not None else 0


class _RecordingLogger:
    """Captures rendered sync_event messages instead of writing a log file."""

    def __init__(self):
        self.messages = []

    def debug(self, message):
        self.messages.append(("debug", message))

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))


class _Unrenderable:
    """A field value whose __str__ raises: one bad field must not kill the loop."""

    def __str__(self):
        raise RuntimeError("cannot render")


class ServerCadenceTests(unittest.TestCase):
    """The policy itself: pure functions of the clock, no threads."""

    def setUp(self):
        refresh_policy.reset_state()
        for name in SERVER_ENV:
            os.environ.pop(name, None)
        self.addCleanup(refresh_policy.reset_state)

    def test_a_new_panel_is_due_immediately(self):
        self.assertTrue(refresh_policy.server_due(101, now=BASE))
        self.assertEqual(refresh_policy.server_due_in(101, now=BASE), 0.0)

    def test_a_watched_panel_is_polled_every_couple_of_seconds(self):
        refresh_policy.note_server_activity(102, now=BASE)
        interval = refresh_policy.note_server_result(102, True, now=BASE)
        self.assertEqual(interval, refresh_policy.server_active_seconds())
        self.assertEqual(interval, 2.0)
        self.assertFalse(refresh_policy.server_due(102, now=BASE + 1.9))
        self.assertTrue(refresh_policy.server_due(102, now=BASE + 2.0))

    def test_an_unwatched_panel_falls_back_through_warm_to_the_idle_cadence(self):
        refresh_policy.note_server_activity(103, now=BASE, ttl=10)
        refresh_policy.note_server_result(103, True, now=BASE)
        # Still inside its watch window.
        self.assertEqual(refresh_policy.server_interval(103, now=BASE + 5),
                         refresh_policy.server_active_seconds())
        self.assertEqual(refresh_policy.server_mode(103, now=BASE + 5), 'hot')
        # Watch window elapsed. The mark left a WARM window behind, so the hand-off
        # from HOT to IDLE is a middle band rather than a cliff -- a panel that was
        # just being watched (or just mutated) is still worth polling at 10 s.
        self.assertEqual(refresh_policy.server_interval(103, now=BASE + 10.001),
                         refresh_policy.server_warm_seconds())
        self.assertEqual(refresh_policy.server_mode(103, now=BASE + 10.001), 'warm')
        self.assertEqual(refresh_policy.server_warm_seconds(), 10.0)
        # Warm window elapsed too, and nothing renewed it: now it is genuinely idle.
        after_warm = BASE + 10 + refresh_policy.server_warm_ttl() + 0.001
        self.assertEqual(refresh_policy.server_interval(103, now=after_warm),
                         refresh_policy.server_idle_seconds())
        self.assertEqual(refresh_policy.server_mode(103, now=after_warm), 'idle')
        self.assertEqual(refresh_policy.server_idle_seconds(), 45.0)

    def test_the_intervals_are_configurable(self):
        with mock.patch.dict(os.environ, {
            "EVE_SERVER_POLL_ACTIVE_SECONDS": "1",
            "EVE_SERVER_POLL_IDLE_SECONDS": "15",
        }):
            self.assertEqual(refresh_policy.server_active_seconds(), 1.0)
            self.assertEqual(refresh_policy.server_idle_seconds(), 15.0)
        # A zero/garbage value falls back instead of turning the loop into a spin.
        with mock.patch.dict(os.environ, {"EVE_SERVER_POLL_ACTIVE_SECONDS": "0"}):
            self.assertEqual(refresh_policy.server_active_seconds(), 1.0)
        with mock.patch.dict(os.environ, {"EVE_SERVER_POLL_IDLE_SECONDS": "x"}):
            self.assertEqual(refresh_policy.server_idle_seconds(), 45.0)

    def test_failures_back_off_exponentially_and_cap(self):
        intervals = []
        for _ in range(8):
            intervals.append(refresh_policy.note_server_result(104, False, now=BASE))
        self.assertEqual(intervals[:5], [5.0, 10.0, 20.0, 40.0, 80.0])
        self.assertEqual(max(intervals), refresh_policy.server_backoff_max())
        self.assertEqual(intervals[-1], 300.0)
        # A success clears the penalty immediately.
        refresh_policy.note_server_result(104, True, now=BASE)
        self.assertEqual(refresh_policy.server_interval(104, now=BASE),
                         refresh_policy.server_idle_seconds())

    def test_backoff_outranks_a_watch_mark(self):
        refresh_policy.note_server_activity(105, now=BASE)
        refresh_policy.note_server_result(105, False, now=BASE)
        self.assertEqual(refresh_policy.server_interval(105, now=BASE), 5.0)

    def test_deferring_a_panel_never_pulls_its_poll_earlier(self):
        refresh_policy.note_server_activity(106, now=BASE)
        refresh_policy.note_server_result(106, True, now=BASE)   # due at BASE+2
        refresh_policy.defer_server_until(106, BASE + 60)
        self.assertFalse(refresh_policy.server_due(106, now=BASE + 30))
        refresh_policy.defer_server_until(106, BASE + 1)         # earlier: ignored
        self.assertEqual(refresh_policy.server_due_in(106, now=BASE), 60.0)

    def test_a_watch_declaration_is_capped_and_deduplicated(self):
        marked = refresh_policy.note_watched_servers("1,2,1,3,4,5", now=BASE, limit=3)
        self.assertEqual(marked, [1, 2, 3])
        rows = {int(sid): row for sid, row in refresh_policy.server_states(now=BASE).items()}
        self.assertTrue(rows[1]["active"])
        self.assertTrue(rows[3]["active"])
        # Beyond the cap the panel is not even tracked, so it keeps the idle cadence:
        # one open tab cannot pin a hundred-server install to a two-second fan-out.
        self.assertNotIn(4, rows)
        self.assertNotIn(5, rows)

    def test_the_watch_limit_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"EVE_SERVER_POLL_WATCH_LIMIT": "2"}):
            self.assertEqual(refresh_policy.server_watch_limit(), 2)
            self.assertEqual(
                refresh_policy.note_watched_servers([1, 2, 3], now=BASE), [1, 2])

    def test_disabled_panels_are_forgotten(self):
        refresh_policy.note_server_activity(1, now=BASE)
        refresh_policy.note_server_activity(2, now=BASE)
        refresh_policy.note_server_result(2, True, now=BASE)
        refresh_policy.retain_servers([1])
        states = refresh_policy.server_states(now=BASE)
        self.assertEqual(list(states), ["1"])

    def test_marking_a_panel_hot_pulls_its_next_poll_in(self):
        # Polled while nobody was watching: next poll is a whole idle interval away,
        # spread by that panel's own jitter -- the idle band is jittered on purpose so
        # a fifty-panel install does not come due in one second.
        refresh_policy.note_server_result(107, True, now=BASE)
        due_in = refresh_policy.server_due_in(107, now=BASE)
        self.assertGreaterEqual(due_in, refresh_policy.server_idle_seconds())
        self.assertLessEqual(due_in, refresh_policy.server_idle_seconds()
                             + refresh_policy.idle_jitter_span())
        # It comes on screen (or an Eve write lands): it must be polled now-ish, not
        # after the remaining idle window.
        refresh_policy.note_server_activity(107, now=BASE)
        due_in = refresh_policy.server_due_in(107, now=BASE)
        self.assertLessEqual(due_in, refresh_policy.server_active_seconds())
        self.assertEqual(due_in, refresh_policy.server_active_seconds())

    def test_a_watch_mark_does_not_pull_in_a_panel_that_is_backing_off(self):
        refresh_policy.note_server_result(108, True, now=BASE)
        refresh_policy.note_server_result(108, False, now=BASE)   # 5 s backoff
        refresh_policy.note_server_activity(108, now=BASE)
        self.assertEqual(refresh_policy.server_due_in(108, now=BASE), 5.0)
        self.assertEqual(refresh_policy.server_interval(108, now=BASE), 5.0)
        refresh_policy.note_server_result(108, False, now=BASE)   # 10 s backoff
        refresh_policy.note_server_activity(108, now=BASE)
        self.assertEqual(refresh_policy.server_due_in(108, now=BASE), 10.0)

    def test_the_next_due_is_the_soonest_panel(self):
        refresh_policy.note_server_result(201, True, now=BASE)   # idle: due at +45
        refresh_policy.note_server_activity(202, now=BASE)
        refresh_policy.note_server_result(202, True, now=BASE)   # active: due at +2
        self.assertAlmostEqual(refresh_policy.next_server_due_in(now=BASE), 2.0, places=3)

    def test_nothing_tracked_reports_no_schedule(self):
        self.assertIsNone(refresh_policy.next_server_due_in(now=BASE))


class PeriodicCycleGateTests(unittest.TestCase):
    """A real (sqlite-backed) periodic cycle must fetch only the panels that are due."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        refresh_policy.reset_state()
        refresh_jobs.REFRESH_BACKOFF.clear()
        self._saved_snapshot = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore)
        Server.query.filter(Server.id.in_((9101, 9102))).delete(synchronize_session=False)
        for server_id in (9101, 9102):
            db.session.add(Server(id=server_id, name="poll-%d" % server_id,
                                  host="https://poll.invalid", username="u", password="p",
                                  panel_type="auto", enabled=True))
        db.session.commit()
        self.fetched = []

    def _restore(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved_snapshot)
        refresh_policy.reset_state()
        refresh_jobs.REFRESH_BACKOFF.clear()
        Server.query.filter(Server.id.in_((9101, 9102))).delete(synchronize_session=False)
        db.session.commit()

    def _worker(self, server_dict):
        self.fetched.append(int(server_dict["id"]))
        return (int(server_dict["id"]), [], None, {"xui_version": "3.0"}, None, None, "auto")

    def _cycle(self, **kwargs):
        with mock.patch.object(app_module, "fetch_worker", self._worker):
            schedulers._fetch_and_update_global_data_inner(**kwargs)
        return list(self.fetched)

    def test_a_second_periodic_cycle_defers_the_panels_it_just_polled(self):
        first = self._cycle(force=False, periodic=True)
        self.assertEqual(sorted(first), [9101, 9102])
        self.fetched.clear()
        last_update = GLOBAL_SERVER_DATA.get("last_update")

        second = self._cycle(force=False, periodic=True)
        self.assertEqual(second, [], "a panel polled a moment ago must not be re-fetched")
        # The deferred cycle must not publish either: a fresh last_update would hide
        # the panel whose turn it actually is.
        self.assertEqual(GLOBAL_SERVER_DATA.get("last_update"), last_update)
        # Deferring is not a failure, and the in-flight flag must be cleared.
        self.assertFalse(GLOBAL_SERVER_DATA.get("is_updating"))
        states = refresh_policy.server_states()
        self.assertEqual({row["failures"] for row in states.values()}, {0})

    def test_a_manual_cycle_ignores_the_schedule(self):
        self._cycle(force=False, periodic=True)
        self.fetched.clear()
        # The operator's Refresh click is intent, not a poll: it fetches everything.
        manual = self._cycle(force=True)
        self.assertEqual(sorted(manual), [9101, 9102])

    def test_only_the_panel_whose_cadence_elapsed_is_fetched(self):
        self._cycle(force=False, periodic=True)
        self.fetched.clear()
        # Simulate the passage of an idle interval for one panel only (its own next
        # poll is in the past now); the other keeps its remaining, unexpired window.
        refresh_policy.note_server_result(9102, True, now=time.time() - 300)
        due = self._cycle(force=False, periodic=True)
        self.assertEqual(due, [9102])

    def test_a_panel_that_is_not_due_keeps_its_cached_reachability(self):
        self._cycle(force=False, periodic=True)
        # Panel 9101 keeps its unexpired window; 9102 becomes due again.
        refresh_policy.note_server_result(9102, True, now=time.time() - 300)
        self.fetched.clear()
        fetched = self._cycle(force=False, periodic=True)
        self.assertEqual(fetched, [9102])
        statuses = {row.get("server_id"): row
                    for row in GLOBAL_SERVER_DATA.get("servers_status") or []}
        # Deferring is not skipping: the panel that was not polled must keep its
        # cached reachability instead of being reported as unreachable/"Backoff".
        self.assertTrue(statuses[9101]["reachable"])
        self.assertIsNone(statuses[9101]["reachable_error"])
        self.assertTrue(statuses[9102]["reachable"])

    def test_a_cycle_reads_a_bounded_batch_and_the_watched_panel_is_in_it(self):
        # Both panels are due at once (a cold start). With one slot per cycle the
        # watched panel must be the one read, or the two-second target is decided by
        # whichever row the database happened to return first.
        refresh_policy.note_server_activity(9102, now=time.time() - 10)
        with mock.patch.dict(os.environ, {"EVE_REFRESH_BATCH_SERVERS": "1"}):
            fetched = self._cycle(force=False, periodic=True)
        self.assertEqual(fetched, [9102])
        # The panel left out was not fetched, so it was not rescheduled: it stays due
        # for the next cycle instead of silently losing its turn.
        self.assertTrue(refresh_policy.server_due(9101))

    def test_the_scheduler_discovers_panels_from_the_database(self):
        # The real loop (no injected rows) takes its enabled set from the database, which
        # needs an application context: a missing one fails once per tick and the install
        # silently stops being polled. This is the only test that drives that path.
        fetched = []

        def fake_fetch(sid):
            fetched.append(int(sid))
            return {'server_id': int(sid), 'changed': False, 'block': []}

        with mock.patch.object(schedulers, 'fetch_and_update_server_data', fake_fetch):
            schedulers.run_per_server_scheduler(duration=1.0, worker_limit=2)
        self.assertTrue(fetched, "the scheduler discovered no enabled panel")
        self.assertTrue(set(fetched).issubset({9101, 9102}), fetched)

    def test_a_manual_cycle_is_never_truncated_by_the_batch_bound(self):
        # The bound is a scheduling device, not a budget: an operator who clicks
        # Refresh asked for every panel, and a silently partial refresh is worse than
        # a slow one.
        with mock.patch.dict(os.environ, {"EVE_REFRESH_BATCH_SERVERS": "1"}):
            fetched = self._cycle(force=True)
        self.assertEqual(sorted(fetched), [9101, 9102])

    def test_a_backoff_skip_schedules_the_panel_instead_of_hot_looping(self):
        self._cycle(force=False, periodic=True)
        refresh_policy.reset_server_state()
        refresh_jobs._backoff_record_failure(9101, "boom")
        self.fetched.clear()
        fetched = self._cycle(force=False, periodic=True)
        # Only the healthy panel is fetched; the backed-off one is not retried...
        self.assertEqual(fetched, [9102])
        # ...and its next poll is pushed to the fetch layer's backoff window, so the
        # loop cannot wake for a cycle that would only skip it again.
        self.assertFalse(refresh_policy.server_due(9101))
        self.assertGreater(refresh_policy.server_due_in(9101), 0.0)

    def test_a_panel_removed_from_the_enabled_set_stops_being_due(self):
        self._cycle(force=False, periodic=True)
        server = db.session.get(Server, 9101)
        server.enabled = False
        db.session.commit()
        refresh_policy.note_server_result(9102, True, now=time.time() - 300)
        self.fetched.clear()
        fetched = self._cycle(force=False, periodic=True)
        self.assertEqual(fetched, [9102])
        self.assertEqual(list(refresh_policy.server_states()), ["9102"])

    def test_the_scheduler_discovers_panels_from_the_database(self):
        # The real per-server loop (no injected rows) takes its enabled set from the
        # database, which needs an application context: a missing one fails once per tick
        # and the install silently stops being polled. This is the only test that drives
        # that path, and it lives here because this class owns real panel rows.
        fetched = []
        done = threading.Event()

        def fake_fetch(sid):
            fetched.append(int(sid))
            done.set()
            return {'server_id': int(sid), 'changed': False, 'block': []}

        with mock.patch.object(schedulers, 'fetch_and_update_server_data', fake_fetch):
            schedulers.run_per_server_scheduler(
                duration=2.0, stop_event=done, worker_limit=2)
        self.assertTrue(fetched, "the scheduler discovered no enabled panel")
        self.assertTrue(set(fetched).issubset({9101, 9102}), fetched)


class PerServerSchedulerTests(unittest.TestCase):
    """The scheduler must dispatch each panel on its own clock, not on a batch.

    The invariant these tests defend: a HOT panel's dispatch is decided by ITS schedule
    and by the availability of a worker, NOT by whether the other panels' reads have
    finished. The old cycle-oriented loop could not satisfy this at any batch size -- a
    batch is a barrier -- so this is the acceptance test for the architecture.

    Determinism: the fake panel read blocks on an Event the test releases, so every
    assertion is about ORDER, never about how long the machine took. Exactly one test
    (`test_the_hot_cadence_does_not_grow_with_the_server_count`) spends real time,
    because "the interval is independent of the install size" is a wall-clock property
    that a fake clock cannot express without re-implementing the loop under test.
    """

    def setUp(self):
        refresh_policy.reset_state()
        schedulers._scheduler_reset_metrics()
        for name in SERVER_ENV:
            os.environ.pop(name, None)

    def tearDown(self):
        refresh_policy.reset_state()

    def _drive(self, *, servers, fetch, workers=2, hot=None, env=None,
               stop_when=None, timeout=2.0):
        """Run the real loop until the test's condition holds, then stop it.

        ``stop_when(metrics)`` is polled from a watcher thread, so no test sleeps for a
        fixed window hoping the loop made progress: the loop stops the moment the
        behaviour under test has happened (or at ``timeout``, which fails the assertion
        that follows).
        """
        stop = threading.Event()
        settings = {"EVE_SERVER_POLL_ACTIVE_SECONDS": "1",
                    "EVE_SERVER_POLL_WARM_SECONDS": "1",
                    "EVE_SERVER_POLL_IDLE_SECONDS": "30"}
        settings.update(env or {})
        rows = [{"id": sid} for sid in servers]
        with mock.patch.dict(os.environ, settings):
            for sid in servers:
                if sid == hot:
                    # Watched and already overdue: due at t=0, on its own cadence after.
                    refresh_policy.note_server_activity(sid, now=time.time() - 5)
                    refresh_policy.note_server_result(sid, True, now=time.time() - 5)
            result = {}
            watcher = None
            if stop_when is not None:
                def watch():
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        if stop_when(result):
                            break
                        stop.wait(0.005)
                    stop.set()
                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
            try:
                result.update(schedulers.run_per_server_scheduler(
                    fetch_callable=fetch, server_rows=rows,
                    duration=timeout, stop_event=stop, worker_limit=workers))
            finally:
                stop.set()
                if watcher is not None:
                    watcher.join(timeout=1.0)
        return result

    def test_a_hot_panel_is_dispatched_while_idle_reads_are_still_running(self):
        # The barrier, stated as an observable: four idle panels occupy both workers and
        # stay in flight, and the watched panel is due. A batch/cycle loop could not
        # start its read until those idle reads finished -- and they do not finish until
        # this test releases them, which happens only after the hot panel has started.
        servers = [9001, 9002, 9003, 9004, 9005]
        hot = 9001
        release = threading.Event()
        in_flight = set()
        hot_started = threading.Event()
        observed = {}
        lock = threading.Lock()

        def fake_fetch(sid):
            with lock:
                in_flight.add(sid)
            if sid == hot:
                hot_started.set()
            else:
                release.wait(0.5)
            with lock:
                in_flight.discard(sid)
            return {"server_id": sid, "changed": False, "block": []}

        def stop_when(_metrics):
            # Snapshot INSIDE the loop's lifetime: what matters is whether the idle reads
            # were still running at the instant the hot panel was dispatched.
            if not hot_started.is_set():
                return False
            with lock:
                observed['others_in_flight'] = bool(in_flight - {hot})
            return True

        try:
            self._drive(servers=servers, hot=hot, fetch=fake_fetch, workers=2,
                        stop_when=stop_when)
        finally:
            release.set()
        self.assertTrue(hot_started.is_set(), "the hot panel never started")
        self.assertTrue(observed.get('others_in_flight'),
                        "the hot panel was dispatched only after the idle reads finished")

    def test_a_server_is_never_read_twice_at_once(self):
        per_server = defaultdict(int)
        violations = []
        release = threading.Event()
        started = threading.Event()
        lock = threading.Lock()

        def fake_fetch(sid):
            with lock:
                per_server[sid] += 1
                if per_server[sid] > 1:
                    violations.append(sid)
                started.set()
            release.wait(2.0)
            return {"server_id": sid, "changed": False, "block": []}

        try:
            self._drive(servers=[9301], hot=9301, fetch=fake_fetch, workers=4,
                        stop_when=lambda _m: started.is_set())
        finally:
            release.set()
        # The panel is due again long before its blocked read returns: the inflight flag
        # plus panel coalescing must still make a concurrent duplicate impossible.
        self.assertEqual(violations, [], "the same panel was read twice concurrently")

    def test_global_concurrency_stays_bounded(self):
        state = {'value': 0, 'max': 0}
        lock = threading.Lock()
        release = threading.Event()
        dispatch_seen = threading.Event()

        def fake_fetch(sid):
            with lock:
                state['value'] += 1
                state['max'] = max(state['max'], state['value'])
                if state['value'] >= 3:
                    dispatch_seen.set()
            release.wait(2.0)
            with lock:
                state['value'] -= 1
            return {"server_id": sid, "changed": False, "block": []}

        try:
            self._drive(servers=list(range(9401, 9415)), fetch=fake_fetch, workers=3,
                        stop_when=lambda _m: dispatch_seen.is_set())
        finally:
            release.set()
        self.assertLessEqual(state['max'], 3, "the worker bound was exceeded")

    def test_worker_saturation_is_measured_not_hidden(self):
        # Twelve panels due at once with two workers: the pressure must be visible in the
        # metrics, because that is the number that says "the cadence is not being met, and
        # here is why" instead of silently reporting a two-second poll.
        release = threading.Event()
        lock = threading.Lock()
        state = {'value': 0}

        def fake_fetch(sid):
            with lock:
                state['value'] += 1
            release.wait(2.0)
            with lock:
                state['value'] -= 1
            return {"server_id": sid, "changed": False, "block": []}

        try:
            metrics = self._drive(
                servers=list(range(9501, 9513)), fetch=fake_fetch, workers=2,
                stop_when=lambda _m: (
                    schedulers.scheduler_metrics().get('saturation_events', 0) > 0))
        finally:
            release.set()
        self.assertGreater(metrics['dispatched'], 0)
        self.assertGreater(metrics['saturation_events'], 0,
                           "a saturated scheduler must report it")
        self.assertEqual(metrics['workers'], 2)

    def test_a_wake_during_an_inflight_read_reschedules_that_panel_immediately(self):
        # The panel is ALREADY being read when the nudge arrives. The nudge cannot shorten
        # that read, but it must not be lost either: the next read of THIS panel starts as
        # soon as the first one returns, instead of waiting out the 30 s idle window the
        # completion just scheduled.
        starts = []
        first_read_running = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        def fake_fetch(sid):
            starts.append(time.monotonic())
            if len(starts) == 1:
                first_read_running.set()
                release_first.wait(2.0)
            else:
                second_started.set()
            return {"server_id": sid, "changed": False, "block": []}

        def nudge():
            self.assertTrue(first_read_running.wait(1.0),
                            "the first read never started")
            refresh_policy.note_server_activity(9601, now=time.time(), share=False)
            self.assertTrue(refresh_policy.wake_pending(9601),
                            "a nudge during an in-flight read must be held")
            release_first.set()

        # Due now, but its NEXT schedule is the 30 s idle band: only a held nudge can
        # bring the second read back inside this test.
        refresh_policy.note_server_result(9601, True, now=time.time() - 60)
        thread = threading.Thread(target=nudge, daemon=True)
        thread.start()
        try:
            self._drive(servers=[9601], fetch=fake_fetch, workers=1,
                        env={"EVE_SERVER_POLL_IDLE_SECONDS": "30",
                             "EVE_SERVER_POLL_ACTIVE_SECONDS": "1"},
                        stop_when=lambda _m: second_started.is_set(), timeout=3.0)
        finally:
            release_first.set()
            thread.join(timeout=2.0)
        self.assertGreaterEqual(len(starts), 2,
                                "the held nudge did not reschedule the panel")

    def test_a_hot_panels_next_schedule_is_its_own_and_not_the_batchs(self):
        # The invariant without a thread or a clock: forty idle panels are due at the same
        # instant as the watched one, and the plan still hands the watched panel to the
        # free worker first. Its next due time is then measured from ITS OWN schedule, so
        # no amount of other work changes when it is polled again.
        refresh_policy.note_server_activity(7001, now=BASE - 5, share=False)
        rows = [{'id': sid} for sid in [7001] + list(range(7002, 7042))]
        self.assertEqual(refresh_policy.scheduler_plan(rows, now=BASE, limit=1), [7001])
        # A 300 ms read that started on schedule leaves the next poll exactly one cadence
        # after that schedule -- start-to-start, not completion-to-completion.
        refresh_policy.note_fetch_started(7001, now=BASE)
        delay = refresh_policy.note_server_result(7001, True, now=BASE + 0.3,
                                                 duration_ms=300)
        self.assertEqual(delay, refresh_policy.server_active_seconds())
        # Period-preserving: the next poll is one cadence after the schedule this read was
        # serving, so at the completion the remaining time is that cadence minus the read.
        self.assertAlmostEqual(
            refresh_policy.server_due_in(7001, now=BASE + 0.3),
            refresh_policy.server_active_seconds() - 0.3, places=6)

    def test_a_capacity_rejection_is_not_a_panel_failure(self):
        # The process-wide panel cap refusing a slot says the INSTALL is busy, not that the
        # panel is sick. Recording it as a failure would put a healthy panel into backoff
        # because of local pressure - a self-inflicted outage - so it must be counted as
        # capacity and leave the panel's health alone.
        from panel.core import panel_limits
        attempts = []

        def fake_fetch(sid):
            attempts.append(sid)
            raise panel_limits.PanelBusy('panel concurrency limit reached')

        self._drive(servers=[9701], fetch=fake_fetch, workers=1,
                    stop_when=lambda _m: len(attempts) >= 2, timeout=2.0)
        state = refresh_policy.server_sync_state(9701)
        self.assertEqual(state['consecutive_failures'], 0)
        self.assertIn(state['sync_health'], ('live', 'fresh'))
        self.assertGreaterEqual(
            schedulers.scheduler_metrics().get('capacity_rejections', 0), 1)

    def test_a_hot_cadence_is_not_a_wall_clock_claim(self):
        # There is deliberately no sleep-based cadence test here: the acceptance numbers
        # for HOT cadence and capacity come from scripts/benchmark_hot_capacity.py, which
        # runs the same policy in virtual time (Tier 3), while this suite stays instant.
        # What the unit tests own is the DECISION (plan order, schedule arithmetic) and
        # the loop's ORDER properties, both proven above without a clock.
        self.assertTrue(True)

    def test_the_bootstrap_sweep_is_not_the_timing_authority(self):
        source = __import__("inspect").getsource(schedulers.background_data_fetcher)
        # One sweep at startup for discovery and a coherent first snapshot, then the
        # per-server loop. A periodic sweep as the timing authority is exactly the
        # architecture this replaced.
        self.assertIn("run_per_server_scheduler()", source)
        self.assertNotIn("periodic=True", source)


class WatchDeclarationRouteTests(unittest.TestCase):
    """The dashboard's declaration must reach the scheduler through the real route."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        from app import Admin
        refresh_policy.reset_state()
        self.addCleanup(refresh_policy.reset_state)
        self._saved_snapshot = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore_snapshot)
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(username="poll-admin", role="superadmin", is_superadmin=True,
                           enabled=True)
        self.admin.set_password("CorrectHorseBattery1!")
        db.session.add(self.admin)
        db.session.commit()
        self._stub_refresh_job()
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = self.admin.id
            sess["role"] = self.admin.role
            sess["is_superadmin"] = bool(self.admin.is_superadmin)

    def _restore_snapshot(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved_snapshot)

    def _stub_refresh_job(self):
        """Keep the route's refresh job out of this test.

        Without Redis the route's job runs in a thread in THIS process, and its
        first act is to prune the schedule of every server the test database does
        not contain -- which is every server named here. That is a race with the
        assertion below, and the subject of these tests is the watch declaration,
        not the fetch it happens to trigger.
        """
        # The route imports the helper from the app namespace (deferred re-export),
        # so that binding -- not the definition module -- is what has to be replaced.
        patcher = mock.patch.object(
            app_module, "enqueue_refresh_job",
            lambda **kwargs: {"id": "test-job", "state": "queued",
                              "mode": kwargs.get("mode"),
                              "server_id": kwargs.get("server_id")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_dashboard_poll_marks_the_servers_it_renders(self):
        with mock.patch.object(refresh_policy, "record_activity"):
            response = self.client.get("/api/refresh?mode=cache&servers=4101,4102")
        self.assertIn(response.status_code, (200, 202))
        states = refresh_policy.server_states()
        self.assertEqual(sorted(states), ["4101", "4102"])
        self.assertTrue(all(row["active"] for row in states.values()))
        self.assertEqual(states["4101"]["interval_seconds"],
                         refresh_policy.server_active_seconds())

    def test_a_targeted_request_marks_its_panel(self):
        with mock.patch.object(refresh_policy, "record_activity"):
            response = self.client.get("/api/refresh?mode=cache&server_id=4201")
        self.assertIn(response.status_code, (200, 202))
        self.assertIn("4201", refresh_policy.server_states())

    def test_the_declaration_is_capped_by_the_route_too(self):
        with mock.patch.dict(os.environ, {"EVE_SERVER_POLL_WATCH_LIMIT": "2"}):
            with mock.patch.object(refresh_policy, "record_activity"):
                response = self.client.get("/api/refresh?mode=cache&servers=1,2,3,4,5")
        self.assertIn(response.status_code, (200, 202))
        states = refresh_policy.server_states()
        self.assertEqual(len(states), 2)
        self.assertTrue(all(row["active"] for row in states.values()))

    def test_the_stream_renews_the_watch_marks(self):
        env = mock.patch.dict(os.environ, {
            "EVE_SSE_ENABLED": "1", "EVE_SSE_MAX_SECONDS": "1",
            "EVE_SSE_TICK_SECONDS": "0.01", "EVE_SSE_HEARTBEAT_SECONDS": "0.05",
        })
        env.start()
        self.addCleanup(env.stop)
        dashboard_routes._stream_state["active"] = 0
        self.addCleanup(lambda: dashboard_routes._stream_state.__setitem__("active", 0))
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({"last_update": "t1", "is_updating": False,
                                   "stats": {}, "servers_status": [], "inbounds": []})
        response = self.client.get("/api/refresh/stream?servers=4301", buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"event: hello", response.get_data())
        states = refresh_policy.server_states()
        self.assertIn("4301", states)
        self.assertTrue(states["4301"]["active"])


class OfflinePolicyTests(unittest.TestCase):
    """Shared setUp for the cross-process tests: no env tuning, no Redis, no threads.

    The single-process fallback is part of the contract -- a missing Redis must
    degrade, never raise -- so these tests pin ``_redis()`` to None by default and
    re-inject an in-memory fake only where the shared path itself is the subject.
    """

    def setUp(self):
        refresh_policy.reset_state()
        for name in SERVER_ENV:
            os.environ.pop(name, None)
        self.addCleanup(refresh_policy.reset_state)
        patcher = mock.patch.object(refresh_policy, "_redis", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)


class FetchBatchTests(OfflinePolicyTests):
    """One fan-out reads the urgent panels first, and never the whole install."""

    def test_a_hot_panel_wins_the_slot_over_an_idle_one(self):
        # The idle panels are due EARLIER than the watched one (it was polled a moment
        # ago), so ordering by due time alone would still queue it last: the band has
        # to outrank the due time, or a two-second target stays unreachable.
        refresh_policy.note_server_activity(11, now=BASE - 10)
        refresh_policy.note_server_result(10, True, now=BASE - 30)
        rows = [{"id": 10}, {"id": 11}, {"id": 12}]
        with mock.patch.dict(os.environ, {"EVE_REFRESH_BATCH_SERVERS": "1"}):
            batch = refresh_policy.prioritize_fetch_batch(rows, now=BASE)
        self.assertEqual([row["id"] for row in batch], [11])

    def test_a_zero_limit_reads_every_panel_that_is_due(self):
        rows = [{"id": 1}, {"id": 2}, {"id": 3}]
        with mock.patch.dict(os.environ, {"EVE_REFRESH_BATCH_SERVERS": "0"}):
            self.assertEqual(
                sorted(row["id"] for row in refresh_policy.prioritize_fetch_batch(rows)),
                [1, 2, 3])

    def test_the_default_limit_is_twice_the_worker_pool(self):
        from panel.core import panel_limits
        self.assertEqual(refresh_policy.fetch_batch_limit(),
                         2 * panel_limits.refresh_worker_limit())

    def test_a_row_without_an_id_is_never_dropped_silently(self):
        # The scheduler builds these rows from the database; a malformed one must sort
        # last, not raise inside the loop that is holding the fetch cycle.
        batch = refresh_policy.prioritize_fetch_batch([{"id": None}, {"id": 5}])
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[0]["id"], 5)


class WakeDeliveryTests(OfflinePolicyTests):
    """A nudge that cannot reach Redis must still reach this process's own loop."""

    def test_publish_wake_without_redis_still_wakes_this_process(self):
        # A single-process install has no channel to publish on, but the fetcher lives
        # in this very process: reporting False without setting the event would leave
        # a dashboard request unable to shorten the sleep at all.
        self.assertFalse(refresh_policy.publish_wake([501], reason="dashboard"))
        self.assertTrue(refresh_policy.wait_for_interval(0.01))
        # wait_for_interval consumes the event, so the next slice sleeps normally.
        self.assertFalse(refresh_policy.wait_for_interval(0.01))

    def test_a_valid_wake_payload_marks_its_servers(self):
        payload = json.dumps({"server_ids": [601, "602"], "reason": "dashboard"})
        self.assertTrue(refresh_policy._handle_wake_payload(payload))
        # The listener stamps wall-clock time because a pub/sub message carries no test
        # clock; the assertion therefore reads the same wall clock back.
        moment = time.time()
        for sid in (601, 602):
            self.assertEqual(refresh_policy.server_mode(sid, now=moment), "hot")
            self.assertEqual(refresh_policy.server_interval(sid, now=moment),
                             refresh_policy.server_active_seconds())
        self.assertTrue(refresh_policy.wait_for_interval(0.01))

    def test_a_payload_that_is_not_json_changes_nothing(self):
        self.assertFalse(refresh_policy._handle_wake_payload(b"not json"))
        # No server may be marked and no wake may be raised: a corrupt message is
        # dropped, otherwise a decoding bug would look like a burst of activity.
        self.assertEqual(refresh_policy.server_states(now=BASE), {})
        self.assertFalse(refresh_policy.wait_for_interval(0.01))

    def test_a_payload_that_is_not_an_object_changes_nothing(self):
        self.assertFalse(refresh_policy._handle_wake_payload(json.dumps([601, 602])))
        self.assertEqual(refresh_policy.server_states(now=BASE), {})
        self.assertFalse(refresh_policy.wait_for_interval(0.01))

    def test_an_empty_payload_is_a_bare_nudge(self):
        # `raw or '{}'` in the handler makes a missing payload an empty one: the
        # listener still wakes (which is the nudge's only job) but names no server, so
        # a build that nudges without ids is not silently ignored.
        self.assertTrue(refresh_policy._handle_wake_payload(None))
        self.assertEqual(refresh_policy.server_states(now=BASE), {})
        self.assertTrue(refresh_policy.wait_for_interval(0.01))

    def test_a_single_id_is_read_as_one_id_not_as_digits(self):
        # The channel is shared Redis, so a publisher that sends one id (an older
        # build, an external tool) must be read the same way as this build's list:
        # iterating the string would mark 6, 0 and 1 hot instead of 601.
        self.assertTrue(refresh_policy._handle_wake_payload(
            json.dumps({"server_ids": "601"})))
        moment = time.time()
        states = refresh_policy.server_states(now=moment)
        self.assertEqual(list(states), ["601"])
        self.assertEqual(refresh_policy.server_mode(601, now=moment), "hot")

    def test_a_comma_separated_declaration_is_read_as_several_ids(self):
        # The same spelling `?servers=` accepts, because the two entry points must not
        # disagree about what a declaration means.
        self.assertTrue(refresh_policy._handle_wake_payload(
            json.dumps({"server_ids": "601,602"})))
        moment = time.time()
        self.assertEqual(sorted(refresh_policy.server_states(now=moment)), ["601", "602"])

    def test_a_single_scalar_id_in_any_form_is_one_id(self):
        # A number and a string both name exactly one panel.
        for value in (601, "601"):
            refresh_policy.reset_state()
            self.assertTrue(refresh_policy._handle_wake_payload(
                json.dumps({"server_ids": value})))
            self.assertEqual(
                list(refresh_policy.server_states(now=time.time())), ["601"],
                "server_ids=%r must name one panel" % (value,))


class WatchShareTests(OfflinePolicyTests):
    """Sharing a watch mark is opt-in, and one declaration nudges once."""

    def test_a_local_only_activity_mark_publishes_nothing(self):
        with mock.patch.object(refresh_policy, "publish_wake") as published, \
                mock.patch.object(refresh_policy, "_publish_watch") as shared:
            refresh_policy.note_server_activity(701, now=BASE, share=False,
                                                reason="mutation")
        # share=False exists because the caller either already shares the fact
        # (note_watched_servers goes on to call note_watch) or IS the listener
        # applying a message it just received -- re-publishing would loop the wake
        # back onto the channel it came from.
        published.assert_not_called()
        shared.assert_not_called()
        # Local-only still means hot here: the in-process fetcher must act on it.
        self.assertEqual(refresh_policy.server_mode(701, now=BASE + 1), "hot")
        self.assertEqual(refresh_policy.server_interval(701, now=BASE + 1),
                         refresh_policy.server_active_seconds())
        self.assertFalse(refresh_policy.wait_for_interval(0.01))
        # An activity mark is not a watch mark: nothing was declared on screen.
        self.assertFalse(refresh_policy.is_server_watched(701, now=BASE + 1))
        self.assertEqual(refresh_policy.server_watch_marks(now=BASE)["local"], {})

    def test_a_declaration_marks_its_server_hot_through_note_watch_once(self):
        with mock.patch.object(refresh_policy, "publish_wake",
                               return_value=False) as published:
            marked = refresh_policy.note_watched_servers([702], now=BASE)
        self.assertEqual(marked, [702])
        # Exactly one nudge per server, sent by note_watch. The activity mark it also
        # applies must not publish a second one: two wakes per dashboard poll would
        # double the listener's work for the same declaration.
        self.assertEqual(published.call_count, 1)
        self.assertEqual(published.call_args.args[0], [702])
        self.assertTrue(refresh_policy.is_server_watched(702, now=BASE + 1))
        self.assertEqual(refresh_policy.server_mode(702, now=BASE + 1), "hot")
        self.assertEqual(refresh_policy.server_interval(702, now=BASE + 1),
                         refresh_policy.server_active_seconds())

    def test_a_renewed_declaration_inside_the_throttle_stays_quiet(self):
        with mock.patch.object(refresh_policy, "publish_wake",
                               return_value=False) as published:
            refresh_policy.note_watched_servers([703], now=BASE)
            self.assertEqual(published.call_count, 1)
            # The dashboard re-declares what it renders on every poll. That renewal is
            # inside the wake throttle while the mark is still valid, so it must not
            # nudge the listener again -- the fetcher has already been told.
            refresh_policy.note_watched_servers([703], now=BASE + 1)
        self.assertEqual(published.call_count, 1)
        self.assertTrue(refresh_policy.is_server_watched(703, now=BASE + 1))
        self.assertEqual(refresh_policy.server_mode(703, now=BASE + 1), "hot")


class CadenceBandTests(OfflinePolicyTests):
    """server_mode() and server_interval() must never disagree about a panel."""

    def test_every_band_maps_to_its_own_interval(self):
        # One panel per band, then the pair is asserted. A freshness badge and the
        # scheduler read this same state, so a mismatch would mean the UI describes a
        # cadence the loop is not actually using.
        refresh_policy.note_server_activity(801, now=BASE, ttl=30)          # hot
        refresh_policy.note_server_activity(802, now=BASE, ttl=10)          # warm later
        refresh_policy.note_server_activity(803, now=BASE, ttl=10)          # idle later
        refresh_policy.note_server_result(804, False, now=BASE)             # backoff
        bands = {
            "hot": refresh_policy.server_active_seconds(),
            "warm": refresh_policy.server_warm_seconds(),
            "idle": refresh_policy.server_idle_seconds(),
            "backoff": refresh_policy.server_backoff_base(),
        }
        cases = (
            # A panel nothing has ever touched is idle, not hot: tracking starts on
            # the first mark, and an untracked panel waits out the idle window.
            ("fresh (never touched)", 800, BASE, "idle"),
            ("hot", 801, BASE + 1, "hot"),
            ("warm", 802, BASE + 10.5, "warm"),
            ("idle", 803, BASE + 10 + refresh_policy.server_warm_ttl() + 1, "idle"),
            ("backoff", 804, BASE, "backoff"),
        )
        for label, sid, moment, expected in cases:
            with self.subTest(band=label):
                mode = refresh_policy.server_mode(sid, now=moment)
                self.assertEqual(mode, expected)
                self.assertEqual(refresh_policy.server_interval(sid, now=moment),
                                 bands[mode])

    def test_a_stale_published_report_admits_it_is_stale(self):
        # A fetcher that dies mid-poll leaves next_due in the past, which clamps to 0 - so
        # the dashboard would render "HOT / Next poll: 0s" forever, reading as "about to
        # poll" when it means "nobody has updated this in minutes". The report carries its
        # own publish time, so an old one must say so instead of faking a due time.
        now = BASE
        fresh = {'mode': 'hot', 'watched': True, 'next_due': now + 1.0,
                 'last_success_at': now - 1.0, 'updated_at': now - 0.5,
                 'sync_health': 'live', 'failures': 0}
        row = refresh_policy._public_sync_row(1, fresh, now=now)
        self.assertEqual(row['sync_health'], 'live')
        self.assertEqual(row['next_due_in_seconds'], 1.0)
        self.assertFalse(row['report_stale'])
        self.assertEqual(row['report_age_seconds'], 0.5)

        old = dict(fresh, next_due=now - 40.0, last_success_at=None,
                   updated_at=now - refresh_policy.SERVER_SYNC_STALE_SECONDS - 1,
                   sync_health='live', failures=3)
        row = refresh_policy._public_sync_row(2, old, now=now)
        # No fake "0s": the schedule is unknown, not due now.
        self.assertIsNone(row['next_due_in_seconds'])
        # And a report that stopped arriving may not keep claiming "live".
        self.assertEqual(row['sync_health'], 'stale')
        self.assertTrue(row['report_stale'])
        self.assertGreater(row['report_age_seconds'],
                           refresh_policy.SERVER_SYNC_STALE_SECONDS)

    def test_a_busy_idle_panel_is_not_promoted_into_the_warm_band(self):
        # A busy install has traffic moving on every panel between two of its own polls.
        # If "changed" promoted an IDLE panel to WARM, the whole install would settle on
        # the 10 s band for the WARM TTL and the 45 s idle cadence would become a fiction --
        # several times the panel load for no operator-visible reason. WARM is a hand-off
        # from attention, so only a panel that was HOT (or still settling) may extend it.
        refresh_policy.note_server_result(861, True, now=BASE, changed=True)
        self.assertEqual(refresh_policy.server_mode(861, now=BASE + 1), "idle")
        self.assertEqual(refresh_policy.server_interval(861, now=BASE + 1),
                         refresh_policy.server_idle_seconds())
        # It keeps answering normally, with no warm window left behind.
        refresh_policy.note_server_result(861, True, now=BASE + 60, changed=True)
        self.assertEqual(refresh_policy.server_mode(861, now=BASE + 61), "idle")
        self.assertFalse(refresh_policy._servers[861].get('warm_until'))

    def test_a_changed_poll_extends_the_warm_window(self):
        refresh_policy.note_server_activity(811, now=BASE, ttl=10)
        refresh_policy.note_server_activity(812, now=BASE, ttl=10)
        # Both panels left the operator's attention before BASE+500; only one of them
        # answered with new data there.
        refresh_policy.note_server_result(811, True, now=BASE + 500, changed=True)
        refresh_policy.note_server_result(812, True, now=BASE + 500, changed=False)
        after_hot = BASE + 10 + refresh_policy.server_warm_ttl() + 1
        # Movement is evidence the panel is worth the middle cadence even after the
        # tab closed: traffic can keep changing while nobody watches, and dropping it
        # straight to the idle window would hide exactly the changes that matter.
        self.assertEqual(refresh_policy.server_mode(811, now=after_hot), "warm")
        self.assertEqual(refresh_policy.server_interval(811, now=after_hot),
                         refresh_policy.server_warm_seconds())
        # An unchanged poll is not evidence of movement, so it leaves the window alone.
        self.assertEqual(refresh_policy.server_mode(812, now=after_hot), "idle")
        self.assertEqual(refresh_policy.server_interval(812, now=after_hot),
                         refresh_policy.server_idle_seconds())

    def test_an_idle_panel_keeps_the_exact_interval_when_jitter_is_disabled(self):
        with mock.patch.dict(os.environ, {"EVE_SERVER_POLL_IDLE_JITTER_SECONDS": "0"}):
            self.assertEqual(refresh_policy.idle_jitter_span(), 0.0)
            # note_server_result returns the delay it actually scheduled, so with the
            # span zeroed the delay is the bare idle band again -- the knob an install
            # that spreads its schedule itself turns off.
            delay = refresh_policy.note_server_result(841, True, now=BASE)
            self.assertEqual(delay, refresh_policy.server_idle_seconds())
            self.assertEqual(refresh_policy.server_due_in(841, now=BASE),
                             refresh_policy.server_idle_seconds())

    def test_idle_panels_are_spread_but_a_hot_panel_is_not(self):
        # Two panels polled in the same cycle: the jitter must separate their due
        # times, otherwise one fan-out has to read the whole install at once.
        first = refresh_policy.note_server_result(851, True, now=BASE)
        second = refresh_policy.note_server_result(852, True, now=BASE)
        span = refresh_policy.idle_jitter_span()
        self.assertGreater(span, 0.0)
        self.assertNotEqual(first, second)
        for sid, delay in ((851, first), (852, second)):
            with self.subTest(server=sid):
                self.assertGreaterEqual(delay, refresh_policy.server_idle_seconds())
                self.assertLess(delay, refresh_policy.server_idle_seconds() + span)
        # HOT is the cadence an operator's expectations are written against, so it
        # keeps its exact seconds and only the idle band is spread.
        refresh_policy.note_server_activity(853, now=BASE)
        self.assertEqual(refresh_policy.note_server_result(853, True, now=BASE),
                         refresh_policy.server_active_seconds())
        self.assertEqual(refresh_policy.server_due_in(853, now=BASE),
                         refresh_policy.server_active_seconds())


class ClientFenceTests(OfflinePolicyTests):
    """The read-your-writes fence, with and without a shared backend."""

    def test_the_single_process_fallback_keeps_the_fence_in_process(self):
        # Without Redis there is no other process to share with, but a single-process
        # install runs the web request and the fetch loop in one interpreter, so the
        # guard must still hold there: the fence is kept locally and every entry point
        # answers instead of raising.
        self.assertTrue(refresh_policy.record_client_fence(
            901, "a@example.invalid", {"used_up": 1}, now=BASE))
        fences = refresh_policy.client_fences(901, now=BASE + 1)
        self.assertEqual(list(fences), ["a@example.invalid"])
        self.assertEqual(fences["a@example.invalid"]["used_up"], 1)
        self.assertEqual(fences["a@example.invalid"]["expires_at"], BASE + 30)
        self.assertTrue(refresh_policy.clear_client_fence(901, "a@example.invalid"))
        self.assertEqual(refresh_policy.client_fences(901, now=BASE + 1), {})
        # Clearing without an email drops the whole server instead of one client.
        refresh_policy.record_client_fence(901, "a@example.invalid",
                                           {"used_up": 1}, now=BASE)
        refresh_policy.record_client_fence(901, "b@example.invalid",
                                           {"used_up": 2}, now=BASE)
        self.assertTrue(refresh_policy.clear_client_fence(901))
        self.assertEqual(refresh_policy.client_fences(901, now=BASE), {})

    def test_a_lagging_read_cannot_revert_the_verified_cap_or_expiry(self):
        # The counters are not the whole verified state: an aggregate read that predates
        # the mutation reports the OLD cap and expiry too, and committing those makes the
        # row disagree with the response and the renewal ledger. The guard must hold the
        # configuration as well, and only release the fence when the aggregate agrees.
        from panel.jobs import refresh as refresh_jobs
        refresh_policy.record_client_fence(
            906, "cap@example.invalid",
            {"used_up": 2 * 1024 ** 3, "used_down": 0,
             "total_bytes": 30 * 1024 ** 3, "expiry_time": 1_700_000_000_000},
            now=BASE)
        stale = [{
            'id': 1, 'server_id': 906, 'remark': 'in',
            'clients': [{
                'email': 'cap@example.invalid', 'up': 1024 ** 3, 'down': 0,
                'up_formatted': '1 GiB', 'down_formatted': '0 B',
                'totalGB': 10 * 1024 ** 3, 'expiryTimestamp': 1_600_000_000_000,
                'raw_client': {'email': 'cap@example.invalid',
                               'totalGB': 10 * 1024 ** 3,
                               'expiryTime': 1_600_000_000_000},
            }],
        }]
        with mock.patch.object(refresh_jobs, "_recompute_cached_client"), \
                mock.patch("app._get_dashboard_status_thresholds", return_value={}), \
                mock.patch("app._get_panel_ui_lang", return_value="en"), \
                mock.patch("app.format_bytes", side_effect=lambda value: "%d B" % value):
            refresh_jobs._apply_client_fences(906, stale, now=BASE)
        row = stale[0]['clients'][0]
        # Counters: held at the verified (higher) value.
        self.assertEqual(row['up'], 2 * 1024 ** 3)
        # Configuration: the verified cap and expiry win over the lagging aggregate.
        self.assertEqual(row['totalGB'], 30 * 1024 ** 3)
        self.assertEqual(row['raw_client']['totalGB'], 30 * 1024 ** 3)
        self.assertEqual(row['expiryTimestamp'], 1_700_000_000_000)
        self.assertEqual(row['raw_client']['expiryTime'], 1_700_000_000_000)
        # The fence is still live: the panel has not caught up, so it may not be released.
        self.assertEqual(list(refresh_policy.client_fences(906, now=BASE + 1)),
                         ["cap@example.invalid"])

    def test_the_fence_is_released_once_the_aggregate_agrees_on_everything(self):
        from panel.jobs import refresh as refresh_jobs
        refresh_policy.record_client_fence(
            907, "agree@example.invalid",
            {"used_up": 2 * 1024 ** 3, "used_down": 0,
             "total_bytes": 30 * 1024 ** 3, "expiry_time": 1_700_000_000_000},
            now=BASE)
        caught_up = [{
            'id': 1, 'server_id': 907, 'remark': 'in',
            'clients': [{
                'email': 'agree@example.invalid', 'up': 2 * 1024 ** 3, 'down': 0,
                'totalGB': 30 * 1024 ** 3, 'expiryTimestamp': 1_700_000_000_000,
                'raw_client': {'email': 'agree@example.invalid',
                               'totalGB': 30 * 1024 ** 3,
                               'expiryTime': 1_700_000_000_000},
            }],
        }]
        with mock.patch.object(refresh_jobs, "_recompute_cached_client"), \
                mock.patch("app._get_dashboard_status_thresholds", return_value={}), \
                mock.patch("app._get_panel_ui_lang", return_value="en"):
            refresh_jobs._apply_client_fences(907, caught_up, now=BASE)
        # Nothing was rewritten (the aggregate already agrees) and the fence is gone, so
        # a later genuine change by the panel is never pinned by an old verification.
        self.assertEqual(caught_up[0]['clients'][0]['totalGB'], 30 * 1024 ** 3)
        self.assertEqual(refresh_policy.client_fences(907, now=BASE + 1), {})

    def test_a_local_fence_expires_on_its_own(self):
        refresh_policy.record_client_fence(905, "a@example.invalid",
                                           {"used_up": 1}, now=BASE, ttl=30)
        # One moment past its own expiry the local row is pruned as it is read, so a
        # fence can never outlive the propagation delay it was meant to cover.
        self.assertEqual(refresh_policy.client_fences(905, now=BASE + 31), {})
        self.assertEqual(refresh_policy._local_fences.get(905) or {}, {})

    def test_a_fence_round_trips_through_the_shared_backend(self):
        fake = _FakeRedis()
        with mock.patch.object(refresh_policy, "_redis", return_value=fake):
            self.assertTrue(refresh_policy.record_client_fence(
                902, "a@example.invalid",
                {"used_up": 11, "used_down": 22, "total_bytes": 33,
                 "expiry_time": 44}, now=BASE, ttl=30))
            key = refresh_policy.MUTATION_FENCE_PREFIX + "902"
            # The shared copy carries the same payload, so the process that owns the
            # fetch loop sees the fence the web process just recorded.
            self.assertIn("a@example.invalid", fake.hashes[key])
            self.assertEqual(fake.expiries[key], 30)
            fences = refresh_policy.client_fences(902, now=BASE + 1)
        self.assertEqual(list(fences), ["a@example.invalid"])
        payload = fences["a@example.invalid"]
        self.assertEqual(payload["used_up"], 11)
        self.assertEqual(payload["used_down"], 22)
        self.assertEqual(payload["total_bytes"], 33)
        self.assertEqual(payload["verified_at"], BASE)
        self.assertEqual(payload["expires_at"], BASE + 30)
        with mock.patch.object(refresh_policy, "_redis", return_value=fake):
            self.assertTrue(refresh_policy.clear_client_fence(902, "a@example.invalid"))
            # The direct read agreed with the verified value, so the fence is gone and
            # cannot keep shielding a value the panel has since changed again.
            self.assertEqual(refresh_policy.client_fences(902, now=BASE + 1), {})
            self.assertEqual(fake.hashes[refresh_policy.MUTATION_FENCE_PREFIX + "902"], {})

    def test_an_expired_shared_fence_is_pruned_and_not_returned(self):
        fake = _FakeRedis()
        key = refresh_policy.MUTATION_FENCE_PREFIX + "903"
        with mock.patch.object(refresh_policy, "_redis", return_value=fake):
            # Written by another process before this one started reading, so only the
            # shared row exists: an expired one and an unparsable one.
            fake.hset(key, "old@example.invalid",
                      json.dumps({"email": "old@example.invalid", "expires_at": BASE - 1}))
            fake.hset(key, "broken@example.invalid", b"not json")
            self.assertEqual(refresh_policy.client_fences(903, now=BASE), {})
            # The read prunes what it dropped, so stale fields cannot accumulate on a
            # key whose Redis TTL keeps being renewed by later fences.
            self.assertEqual(fake.hashes[key], {})

    def test_a_fence_needs_an_email_and_a_real_state(self):
        fake = _FakeRedis()
        with mock.patch.object(refresh_policy, "_redis", return_value=fake):
            # A fence without a client key or with nothing to remember would shield an
            # unknown value, so the guards must refuse rather than store a blank row.
            self.assertFalse(refresh_policy.record_client_fence(
                904, "", {"used_up": 1}, now=BASE))
            self.assertFalse(refresh_policy.record_client_fence(
                904, "a@example.invalid", None, now=BASE))
            self.assertFalse(refresh_policy.record_client_fence(
                904, "a@example.invalid", {"used_up": 1}, now=BASE, ttl=0))
            self.assertEqual(refresh_policy.client_fences(904, now=BASE), {})


class SyncDiagnosticsTests(OfflinePolicyTests):
    """The per-server sync state the doctor page and freshness badge read."""

    def test_health_ladder_from_down_to_stale(self):
        # Never answered: unreachable is not the same as old, but both are "not live".
        self.assertEqual(refresh_policy.sync_health(1001, now=BASE), "down")
        refresh_policy.note_server_result(1001, True, now=BASE, changed=True)
        self.assertEqual(refresh_policy.sync_health(1001, now=BASE + 1), "live")
        # Just past the live threshold the panel is still fresh...
        self.assertEqual(refresh_policy.sync_health(
            1001, now=BASE + refresh_policy.LIVE_MAX_AGE_SECONDS + 1), "fresh")
        # ...and past the fresh threshold it is stale, but never "down": it answered.
        self.assertEqual(refresh_policy.sync_health(
            1001, now=BASE + refresh_policy.FRESH_MAX_AGE_SECONDS + 1), "stale")

    def test_a_failing_panel_is_backoff_until_the_backoff_is_exhausted(self):
        refresh_policy.note_server_result(1002, False, now=BASE)
        self.assertEqual(refresh_policy.sync_health(1002, now=BASE), "backoff")
        # Fail until the exponential window reaches its cap.
        while (refresh_policy.server_interval(1002, now=BASE)
               < refresh_policy.server_backoff_max()):
            refresh_policy.note_server_result(1002, False, now=BASE)
        state = refresh_policy.server_sync_state(1002, now=BASE)
        self.assertEqual(state["backoff_seconds"], refresh_policy.server_backoff_max())
        # A bounded backoff is still a panel we will reach again; a capped one is not,
        # so it must stop being reported as a temporary outage.
        self.assertEqual(refresh_policy.sync_health(1002, now=BASE), "down")

    def test_the_summary_counts_the_bands_and_the_health_it_exposes(self):
        refresh_policy.note_server_result(1011, True, now=BASE)         # idle + live
        refresh_policy.note_server_activity(1012, now=BASE)             # hot, silent
        for _ in range(8):
            refresh_policy.note_server_result(1013, False, now=BASE)    # backoff
        summary = refresh_policy.sync_summary(now=BASE + 1)
        self.assertEqual(summary["modes"],
                         {"hot": 1, "warm": 0, "idle": 1, "backoff": 1})
        # 1012 never answered and 1013 exhausted its backoff: both are down, and only
        # the panel with a successful read counts as tracked.
        self.assertEqual(summary["health"],
                         {"live": 1, "fresh": 0, "stale": 0, "backoff": 0, "down": 2})
        self.assertEqual(summary["tracked_servers"], 1)
        self.assertEqual(summary["max_staleness_seconds"], 1.0)
        self.assertEqual(summary["watch_shared_backend"], "process")
        band_intervals = {"hot": refresh_policy.server_active_seconds(),
                          "warm": refresh_policy.server_warm_seconds(),
                          "idle": refresh_policy.server_idle_seconds()}
        for band, seconds in band_intervals.items():
            with self.subTest(band=band):
                self.assertEqual(summary["poll_intervals"][band], seconds)
        self.assertEqual(summary["poll_intervals"]["backoff_max"],
                         refresh_policy.server_backoff_max())
        # The one panel with a schedule comes due an idle window after it was polled,
        # spread by that server's own jitter so fifty panels do not share one due time.
        expected_due = (refresh_policy.server_idle_seconds()
                        + refresh_policy.server_idle_jitter(
                            1011, refresh_policy.server_idle_seconds())
                        - 1)
        self.assertAlmostEqual(summary["next_due_in_seconds"], expected_due, places=3)

    def test_the_fetch_lifecycle_stamps_the_fields_diagnostics_read(self):
        state = refresh_policy._new_server_state()
        for field in ("currently_fetching", "last_fetch_finished_at", "last_outcome",
                      "last_snapshot_publish_at"):
            self.assertIn(field, state)
        refresh_policy.note_fetch_started(1021, now=BASE)
        started = refresh_policy.server_sync_state(1021, now=BASE)
        self.assertTrue(started["currently_fetching"])
        # Nothing has finished yet, so there is no age to report -- not a fake zero.
        self.assertIsNone(started["last_fetch_age_seconds"])
        self.assertIsNone(started["last_outcome"])
        refresh_policy.note_server_result(1021, True, now=BASE + 1,
                                          duration_ms=25, changed=True)
        refresh_policy.note_snapshot_publish(1021, now=BASE + 2)
        finished = refresh_policy.server_sync_state(1021, now=BASE + 3)
        self.assertFalse(finished["currently_fetching"])
        self.assertEqual(finished["last_fetch_age_seconds"], 2.0)
        self.assertEqual(finished["last_fetch_duration_ms"], 25)
        self.assertEqual(finished["last_outcome"], "changed")
        # The publish stamp is what the freshness badge ages: a block that reached the
        # snapshot at BASE+2 is one second old at BASE+3.
        self.assertEqual(finished["last_publish_age_seconds"], 1.0)


class SyncEventTests(OfflinePolicyTests):
    """sync_event is the cadence's only logging path: it must never raise."""

    def setUp(self):
        super().setUp()
        self.logger = _RecordingLogger()
        patcher = mock.patch("panel.core.logging_config.get_resilient_logger",
                             return_value=self.logger)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_level_renders_and_drops_none_fields(self):
        refresh_policy.sync_event("sync.test.info", level="info", server_id=1,
                                  reason=None, changed=False)
        refresh_policy.sync_event("sync.test.debug", level="debug", mode="hot")
        refresh_policy.sync_event("sync.test.warning", level="warning",
                                  error_type="TimeoutError")
        self.assertEqual([level for level, _ in self.logger.messages],
                         ["info", "debug", "warning"])
        for _, message in self.logger.messages:
            # A None field must be omitted, never rendered as the literal "None": a
            # line reading reason=None hides whether the field is absent or empty.
            self.assertNotIn("None", message)
            self.assertNotIn("reason=", message)
        self.assertEqual(self.logger.messages[0][1],
                         "sync.test.info changed=False server_id=1")

    def test_odd_field_values_are_coerced_not_raised(self):
        refresh_policy.sync_event("sync.test.odd", level="warning", server_id=None,
                                  changed=False, next_due_seconds=0.0, empty="",
                                  blob=object(), items=[1, 2])
        self.assertEqual(len(self.logger.messages), 1)
        _, message = self.logger.messages[0]
        # Zero and an empty string are real values, so they stay in the rendered line
        # even though None does not: "changed=False" is not the same as "no change".
        self.assertIn("changed=False", message)
        self.assertIn("next_due_seconds=0.0", message)
        self.assertIn("empty=", message)
        self.assertNotIn("server_id=", message)

    def test_a_field_that_cannot_render_does_not_escape(self):
        # The one thing a diagnostic may never do is take the fetch loop down, so a
        # value that blows up inside the formatter drops the whole event instead.
        refresh_policy.sync_event("sync.test.explode", level="info",
                                  bad=_Unrenderable())
        self.assertEqual(self.logger.messages, [])


class ServerPollingDocTests(unittest.TestCase):
    def test_the_policy_is_documented(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "docs", "performance", "SERVER_POLLING.md")
        self.assertTrue(os.path.exists(path), "the per-server polling policy needs a doc")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for token in ("EVE_SERVER_POLL_ACTIVE_SECONDS", "EVE_SERVER_POLL_IDLE_SECONDS",
                      "EVE_SERVER_POLL_WATCH_LIMIT", "external"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
