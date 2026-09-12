"""Phase 18 tests: adaptive per-server polling (external X-UI change latency).

The cycle cadence in refresh_policy decides how often the fetcher wakes; this suite
covers the layer under it -- which panel's turn it is inside a cycle, how a watched or
just-mutated panel gets polled every couple of seconds while the rest of the install
keeps its idle cadence, and how a failing panel backs off.
"""
import os
import tempfile
import time
import unittest
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
    "EVE_SERVER_POLL_ACTIVE_TTL_SECONDS", "EVE_SERVER_POLL_BACKOFF_BASE_SECONDS",
    "EVE_SERVER_POLL_BACKOFF_MAX_SECONDS", "EVE_SERVER_POLL_WATCH_LIMIT",
)

BASE = 5_000_000.0


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

    def test_an_unwatched_panel_falls_back_to_the_idle_cadence(self):
        refresh_policy.note_server_activity(103, now=BASE, ttl=10)
        refresh_policy.note_server_result(103, True, now=BASE)
        # Still inside its watch window.
        self.assertEqual(refresh_policy.server_interval(103, now=BASE + 5),
                         refresh_policy.server_active_seconds())
        # Window elapsed: nobody renewed the mark, so it drops to the idle cadence.
        self.assertEqual(refresh_policy.server_interval(103, now=BASE + 10.001),
                         refresh_policy.server_idle_seconds())
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
        # Polled while nobody was watching: next poll is a whole idle interval away.
        refresh_policy.note_server_result(107, True, now=BASE)
        self.assertEqual(refresh_policy.server_due_in(107, now=BASE),
                         refresh_policy.server_idle_seconds())
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


class FetcherLoopWakeTests(unittest.TestCase):
    """The loop must wake for the earliest due panel, not for the whole cycle."""

    class _Stop(BaseException):
        pass

    def setUp(self):
        refresh_policy.reset_state()
        for name in SERVER_ENV:
            os.environ.pop(name, None)
        self._saved_last_update = GLOBAL_SERVER_DATA.get("last_update")
        self.addCleanup(self._restore)

    def _restore(self):
        GLOBAL_SERVER_DATA["last_update"] = self._saved_last_update
        refresh_policy.reset_state()

    def test_the_sleep_is_bounded_by_the_earliest_due_panel(self):
        stop = self._Stop
        # A fresh snapshot (nothing to do at cycle level) plus one panel whose own
        # cadence elapses in two seconds.
        GLOBAL_SERVER_DATA["last_update"] = datetime.now(timezone.utc).isoformat()
        refresh_policy.note_server_activity(301)
        refresh_policy.note_server_result(301, True)
        fetches, waits = [], []

        def fake_fetch(force=False, **kwargs):
            fetches.append(force)
            raise stop()

        def fake_wait(seconds):
            waits.append(seconds)
            raise stop()

        with (
            mock.patch.object(schedulers, "ensure_background_threads_started"),
            mock.patch.object(schedulers, "load_snapshot_from_redis"),
            mock.patch.object(schedulers, "fetch_and_update_global_data", fake_fetch),
            mock.patch.object(refresh_policy, "wait_for_interval", fake_wait),
            app.app_context(),
        ):
            with self.assertRaises(stop):
                schedulers.background_data_fetcher()

        self.assertEqual(fetches, [], "nothing was due yet")
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0], refresh_policy.server_active_seconds() + 0.5)
        self.assertGreaterEqual(waits[0], 0.25)

    def test_a_due_panel_wakes_the_loop_even_when_the_snapshot_looks_fresh(self):
        stop = self._Stop
        GLOBAL_SERVER_DATA["last_update"] = datetime.now(timezone.utc).isoformat()
        # Polled long ago (its window has long since elapsed), so it is due now.
        refresh_policy.note_server_result(302, True, now=time.time() - 300)
        calls = []

        def fake_fetch(force=False, **kwargs):
            calls.append((force, kwargs.get("periodic")))
            raise stop()

        with (
            mock.patch.object(schedulers, "ensure_background_threads_started"),
            mock.patch.object(schedulers, "load_snapshot_from_redis"),
            mock.patch.object(schedulers, "fetch_and_update_global_data", fake_fetch),
            mock.patch.object(refresh_policy, "wait_for_interval", lambda seconds: None),
            app.app_context(),
        ):
            with self.assertRaises(stop):
                schedulers.background_data_fetcher()
        self.assertEqual(calls, [(False, True)])

    def test_the_loop_asks_for_the_periodic_path(self):
        source = __import__("inspect").getsource(schedulers.background_data_fetcher)
        self.assertIn("periodic=True", source)


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
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = self.admin.id
            sess["role"] = self.admin.role
            sess["is_superadmin"] = bool(self.admin.is_superadmin)

    def _restore_snapshot(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved_snapshot)

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
