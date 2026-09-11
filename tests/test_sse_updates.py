"""Phase 12 tests: server-sent live updates for the dashboard snapshot."""
import json
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import Admin, GLOBAL_SERVER_DATA, app, db  # noqa: E402
from panel.core import snapshot_delta  # noqa: E402
from panel.routes import dashboard as dashboard_routes  # noqa: E402

SSE_ENV_VARS = (
    "EVE_SSE_ENABLED", "EVE_SSE_MAX_STREAMS", "EVE_SSE_MAX_SECONDS",
    "EVE_SSE_TICK_SECONDS", "EVE_SSE_HEARTBEAT_SECONDS",
)


def _inbound(server_id, inbound_id, up=0):
    return {
        "server_id": server_id,
        "id": inbound_id,
        "remark": "in %d/%d" % (server_id, inbound_id),
        "clients": [{"email": "c%d@test" % inbound_id, "up": up, "enable": True,
                     "id": "uuid-%d" % inbound_id}],
    }


class SseEventTests(unittest.TestCase):
    def test_event_formatting(self):
        self.assertEqual(
            dashboard_routes.sse_event("changed", {"a": 1}),
            'event: changed' + '\n' + 'data: {"a": 1}' + '\n\n',
        )

    def test_limits_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {
            "EVE_SSE_ENABLED": "1", "EVE_SSE_MAX_STREAMS": "3",
            "EVE_SSE_MAX_SECONDS": "7", "EVE_SSE_TICK_SECONDS": "0.5",
            "EVE_SSE_HEARTBEAT_SECONDS": "9",
        }):
            limits = dashboard_routes.sse_limits()
        self.assertTrue(limits["enabled"])
        self.assertEqual(limits["max_streams"], 3)
        self.assertEqual(limits["max_seconds"], 7)
        self.assertEqual(limits["tick_seconds"], 0.5)
        self.assertEqual(limits["heartbeat_seconds"], 9.0)

    def test_invalid_numbers_fall_back_to_defaults(self):
        with mock.patch.dict(os.environ, {"EVE_SSE_MAX_STREAMS": "not-a-number"}):
            self.assertEqual(dashboard_routes.sse_limits()["max_streams"], 8)


class SseStreamTests(unittest.TestCase):
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
        snapshot_delta.reset_state()
        self._redis_patch = mock.patch.object(snapshot_delta, "_redis", return_value=None)
        self._redis_patch.start()
        self.addCleanup(self._redis_patch.stop)
        self._saved_snapshot = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore_snapshot)
        env_patch = mock.patch.dict(os.environ, {
            "EVE_SSE_ENABLED": "1",
            "EVE_SSE_MAX_STREAMS": "4",
            "EVE_SSE_MAX_SECONDS": "1",
            "EVE_SSE_TICK_SECONDS": "0.01",
            "EVE_SSE_HEARTBEAT_SECONDS": "0.05",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)
        dashboard_routes._stream_state["active"] = 0
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(username="sse-admin", role="superadmin", is_superadmin=True,
                           enabled=True)
        self.admin.set_password("CorrectHorseBattery1!")
        db.session.add(self.admin)
        db.session.commit()
        self.client = app.test_client()
        self._login(self.admin)

    def _restore_snapshot(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved_snapshot)
        dashboard_routes._stream_state["active"] = 0

    def _login(self, admin):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = admin.id
            sess["role"] = admin.role
            sess["is_superadmin"] = bool(admin.is_superadmin)

    def _seed_snapshot(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({
            "last_update": "t1",
            "is_updating": False,
            "stats": {"total_clients": 1},
            "servers_status": [{"server_id": 1, "name": "one"}],
            "inbounds": [_inbound(1, 1, up=0), _inbound(2, 1, up=0)],
        })
        from app import _ensure_snapshot_enriched
        _ensure_snapshot_enriched()

    def test_disabled_stream_returns_404(self):
        with mock.patch.dict(os.environ, {"EVE_SSE_ENABLED": "0"}):
            response = self.client.get("/api/refresh/stream")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["success"])

    def test_stream_requires_login(self):
        anonymous = app.test_client()
        response = anonymous.get("/api/refresh/stream")
        self.assertEqual(response.status_code, 401)

    def test_stream_cap_returns_503(self):
        dashboard_routes._stream_state["active"] = 4
        response = self.client.get("/api/refresh/stream")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Too many", response.get_json()["error"])

    def test_hello_then_changed_event_on_a_snapshot_update(self):
        self._seed_snapshot()
        first = snapshot_delta.sync(GLOBAL_SERVER_DATA)
        GLOBAL_SERVER_DATA["inbounds"][0]["clients"][0]["up"] = 99
        GLOBAL_SERVER_DATA["last_update"] = "t2"
        snapshot_delta.mark_dirty(server_ids=[1])

        response = self.client.get(
            "/api/refresh/stream?since=%d" % first["revision"], buffered=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue((response.content_type or "").startswith("text/event-stream"))
        self.assertEqual(response.headers.get("X-Accel-Buffering"), "no")
        self.assertIn("no-store", response.headers.get("Cache-Control") or "")
        body = response.get_data()
        self.assertIn(b"retry: 3000", body)
        self.assertIn(b"event: hello", body)
        self.assertIn(b'"revision"', body)
        self.assertIn(b"event: changed", body)
        payload = body.split(b"event: changed")[1].split(b"data: ", 1)[1].split(b"\n", 1)[0]
        self.assertEqual(json.loads(payload)["mode"], "delta")

    def test_connecting_without_a_revision_does_not_announce_the_current_state(self):
        self._seed_snapshot()
        response = self.client.get("/api/refresh/stream", buffered=True)
        body = response.get_data()
        self.assertIn(b"event: hello", body)
        self.assertNotIn(b"event: changed", body)
        self.assertIn(b"event: bye", body)

    def test_unknown_revision_is_announced_as_full(self):
        self._seed_snapshot()
        snapshot_delta.sync(GLOBAL_SERVER_DATA)
        response = self.client.get("/api/refresh/stream?since=999999", buffered=False)
        iterator = response.iter_encoded()
        collected = next(iterator) + next(iterator)
        for chunk in iterator:
            collected += chunk
            if b"event: changed" in collected:
                break
        response.close()
        payload = collected.split(b"event: changed")[1].split(b"data: ", 1)[1].split(b"\n", 1)[0]
        self.assertEqual(json.loads(payload)["mode"], "full")

    def test_stream_ends_at_max_lifetime_and_releases_the_slot(self):
        self._seed_snapshot()
        with mock.patch.dict(os.environ, {"EVE_SSE_MAX_SECONDS": "1",
                                          "EVE_SSE_TICK_SECONDS": "0.02"}):
            response = self.client.get("/api/refresh/stream", buffered=True)
        self.assertIn(b"event: bye", response.get_data())
        self.assertEqual(dashboard_routes._stream_state["active"], 0)

    def test_keep_alive_comment_when_nothing_changes(self):
        self._seed_snapshot()
        with mock.patch.dict(os.environ, {"EVE_SSE_HEARTBEAT_SECONDS": "0.02",
                                          "EVE_SSE_TICK_SECONDS": "0.01"}):
            response = self.client.get("/api/refresh/stream", buffered=True)
        self.assertIn(b": keep-alive", response.get_data())


if __name__ == "__main__":
    unittest.main()
