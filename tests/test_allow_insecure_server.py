"""Tests for the per-server allow_insecure transport opt-in.

The feature must never downgrade security globally: a server that opted in is
reachable over plaintext or with an unverified certificate, and every other
server keeps full validation.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import Admin, app, db  # noqa: E402
from panel.models import AuditLog, Server  # noqa: E402
from panel.security import (  # noqa: E402
    InsecurePanelTransportError,
    enforce_panel_transport,
    panel_tls_verify,
)
from panel.services import backup as backup_service  # noqa: E402


class _FakePanelResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    content = b'{"success": true}'

    def json(self):
        return {"success": True}


class _FakeBackupResponse:
    status_code = 404
    headers = {"Content-Type": "application/json"}
    content = b"{}"

    def json(self):
        return {}


class _RecordingSession:
    """Minimal requests.Session stand-in that records the calls made."""

    def __init__(self, response=None):
        self.calls = []
        self.verify = True
        self._response = response or _FakeBackupResponse()

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._response


class TransportPolicyTests(unittest.TestCase):
    def test_https_with_full_verification_is_allowed(self):
        info = enforce_panel_transport("https://panel.example.com:8443",
                                       allow_insecure=False)
        self.assertEqual(info["scheme"], "https")
        self.assertTrue(panel_tls_verify(SimpleNamespace(allow_insecure=False)))

    def test_https_with_opt_in_skips_certificate_verification(self):
        enforce_panel_transport("https://self-signed.example.com",
                                allow_insecure=True)
        self.assertIs(panel_tls_verify(SimpleNamespace(allow_insecure=True)), False)

    def test_plaintext_remote_is_refused_without_the_opt_in(self):
        with self.assertRaises(InsecurePanelTransportError):
            enforce_panel_transport("http://31.14.115.171:2053",
                                    allow_insecure=False)

    def test_plaintext_remote_is_allowed_with_the_opt_in(self):
        info = enforce_panel_transport("http://31.14.115.171:2053",
                                       allow_insecure=True)
        self.assertEqual(info["scheme"], "http")
        self.assertIs(panel_tls_verify(SimpleNamespace(allow_insecure=True)), False)

    def test_loopback_plaintext_keeps_working_without_the_opt_in(self):
        info = enforce_panel_transport("http://127.0.0.1:2053",
                                       allow_insecure=False)
        self.assertTrue(info["loopback"])

    def test_the_legacy_env_flag_cannot_downgrade_a_per_server_decision(self):
        with mock.patch.dict(os.environ, {"EVE_ALLOW_INSECURE_PANEL": "1"}):
            # An explicit per-server policy always wins over the process fallback.
            with self.assertRaises(InsecurePanelTransportError):
                enforce_panel_transport("http://31.14.115.171:2053",
                                        allow_insecure=False)
            self.assertTrue(panel_tls_verify(SimpleNamespace(allow_insecure=False)))
            self.assertIs(panel_tls_verify(SimpleNamespace(allow_insecure=True)), False)


class ServerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username="insecure-admin", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        db.session.add(cls.admin)
        db.session.commit()
        cls.admin_id = cls.admin.id
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        AuditLog.query.delete()
        Server.query.delete()
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = self.admin_id
            sess["role"] = "admin"
            sess["is_superadmin"] = False

    def _add(self, host, **extra):
        payload = {"name": "s", "host": host, "username": "u",
                   "password": "panel-secret-123"}
        payload.update(extra)
        return self.client.post("/api/servers", json=payload)

    def test_adding_an_insecure_server_stores_and_reports_the_flag(self):
        response = self._add("http://31.14.115.171:2053", allow_insecure=True)
        self.assertEqual(response.status_code, 200, response.data)
        server = Server.query.one()
        self.assertTrue(bool(server.allow_insecure))
        payload = server.to_dict()
        self.assertTrue(payload["allow_insecure"])
        self.assertTrue(payload["has_password"])
        self.assertNotIn("password", payload)
        self.assertNotIn(server.password, json.dumps(payload))

    def test_adding_a_plaintext_server_without_the_flag_is_refused(self):
        response = self._add("http://31.14.115.171:2053")
        # API business errors answer HTTP 200 with the real status in a header.
        body = response.get_json()
        self.assertFalse(body["success"])
        self.assertEqual(response.headers.get("X-Eve-Status"), "400")
        self.assertIn("plaintext", body["error"].lower())
        self.assertIsNone(Server.query.first())

    def test_adding_https_stays_secure_by_default(self):
        response = self._add("https://panel.example.com:8443")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(bool(Server.query.one().allow_insecure))

    def test_an_empty_password_keeps_the_stored_one(self):
        self._add("https://panel.example.com")
        server = Server.query.one()
        stored = server.password
        response = self.client.put("/api/servers/%d" % server.id,
                                   json={"name": "renamed", "password": "", "api_token": ""})
        self.assertEqual(response.status_code, 200, response.data)
        db.session.refresh(server)
        self.assertEqual(server.password, stored)
        self.assertEqual(server.name, "renamed")

    def test_a_new_password_replaces_the_stored_one(self):
        self._add("https://panel.example.com")
        server = Server.query.one()
        stored = server.password
        response = self.client.put("/api/servers/%d" % server.id,
                                   json={"password": "new-secret"})
        self.assertEqual(response.status_code, 200, response.data)
        db.session.refresh(server)
        self.assertNotEqual(server.password, stored)
        from app import get_server_password
        self.assertEqual(get_server_password(server), "new-secret")

    def test_an_empty_api_token_keeps_the_stored_token(self):
        self._add("https://panel.example.com", api_token="token-one")
        server = Server.query.one()
        stored = server.api_token
        self.assertTrue(stored)
        response = self.client.put("/api/servers/%d" % server.id,
                                   json={"name": "kept", "api_token": ""})
        self.assertEqual(response.status_code, 200, response.data)
        db.session.refresh(server)
        self.assertEqual(server.api_token, stored)

    def test_a_new_api_token_replaces_the_stored_one(self):
        self._add("https://panel.example.com", api_token="token-one")
        server = Server.query.one()
        stored = server.api_token
        self.client.put("/api/servers/%d" % server.id, json={"api_token": "token-two"})
        db.session.refresh(server)
        self.assertNotEqual(server.api_token, stored)
        from app import get_server_api_token
        self.assertEqual(get_server_api_token(server), "token-two")

    def test_the_token_is_removed_only_by_the_explicit_action(self):
        self._add("https://panel.example.com", api_token="token-one")
        server = Server.query.one()
        self.assertTrue(server.api_token)
        response = self.client.put("/api/servers/%d" % server.id,
                                   json={"clear_api_token": True})
        self.assertEqual(response.status_code, 200, response.data)
        db.session.refresh(server)
        self.assertIsNone(server.api_token)

    def test_flipping_the_flag_is_persisted_and_audited_without_secrets(self):
        self._add("https://panel.example.com", password="top-secret-999",
                  api_token="secret-token-999")
        server = Server.query.one()
        response = self.client.put("/api/servers/%d" % server.id,
                                   json={"allow_insecure": True})
        self.assertEqual(response.status_code, 200, response.data)
        db.session.refresh(server)
        self.assertTrue(bool(server.allow_insecure))
        row = AuditLog.query.filter_by(action="server.allow_insecure").order_by(
            AuditLog.id.desc()).first()
        self.assertIsNotNone(row, "allow_insecure change was not audited")
        meta = json.loads(row.meta_json or "{}")
        self.assertEqual(meta.get("old"), False)
        self.assertEqual(meta.get("new"), True)
        self.assertNotIn("top-secret-999", row.meta_json or "")
        self.assertNotIn("secret-token-999", row.meta_json or "")

    def test_turning_the_flag_off_for_a_plaintext_host_is_refused(self):
        self._add("http://31.14.115.171:2053", allow_insecure=True)
        server = Server.query.one()
        response = self.client.put("/api/servers/%d" % server.id,
                                   json={"allow_insecure": False})
        body = response.get_json()
        self.assertFalse(body["success"])
        self.assertEqual(response.headers.get("X-Eve-Status"), "400")
        self.assertIn("plaintext", body["error"].lower())
        db.session.refresh(server)
        self.assertTrue(bool(server.allow_insecure))


class SessionPolicyTests(unittest.TestCase):
    def setUp(self):
        from panel.adapters import xui
        self.xui = xui
        xui.XUI_SESSION_CACHE.clear()
        xui.XUI_CAPABILITY_CACHE.clear()
        xui.XUI_COOKIE_SESSION_CACHE.clear()

    def test_each_server_session_carries_its_own_policy(self):
        from panel.adapters import xui
        with mock.patch.object(xui.requests.Session, "post",
                               return_value=_FakePanelResponse()), \
                mock.patch.object(xui, "_fetch_csrf_token", lambda *a, **k: None):
            insecure = xui.get_xui_cookie_session(
                "http://31.14.115.171:2053", "u", "p", allow_insecure=True)
            secure = xui.get_xui_cookie_session(
                "https://panel.example.com", "u", "p", allow_insecure=False)
        self.assertIsNotNone(insecure)
        self.assertIsNotNone(secure)
        self.assertIs(insecure.verify, False)
        self.assertTrue(secure.verify)
        # The insecure session must not have relaxed anything for the other one.
        self.assertTrue(panel_tls_verify(SimpleNamespace(allow_insecure=False)))

    def test_the_cookie_cache_key_separates_the_two_policies(self):
        from panel.adapters import xui
        with mock.patch.object(xui.requests.Session, "post",
                               return_value=_FakePanelResponse()), \
                mock.patch.object(xui, "_fetch_csrf_token", lambda *a, **k: None):
            xui.get_xui_cookie_session("http://31.14.115.171:2053", "u", "p",
                                       allow_insecure=True)
        keys = list(xui.XUI_COOKIE_SESSION_CACHE)
        self.assertTrue(any("insecure" in str(key) for key in keys), keys)

    def test_configuration_changes_drop_every_cache_for_the_server(self):
        from panel.adapters import xui
        xui.XUI_SESSION_CACHE[7] = {"session": object()}
        xui.XUI_CAPABILITY_CACHE[7] = {"v3_clients": True}
        xui.XUI_COOKIE_SESSION_CACHE["https://panel.example.com|u|verified"] = {"session": object()}
        xui.XUI_COOKIE_SESSION_CACHE["https://other.example.com|u|verified"] = {"session": object()}
        xui.invalidate_xui_caches(server_id=7, host="https://panel.example.com",
                                  username="u")
        self.assertNotIn(7, xui.XUI_SESSION_CACHE)
        self.assertNotIn(7, xui.XUI_CAPABILITY_CACHE)
        self.assertNotIn("https://panel.example.com|u|verified",
                         xui.XUI_COOKIE_SESSION_CACHE)
        self.assertIn("https://other.example.com|u|verified",
                      xui.XUI_COOKIE_SESSION_CACHE)

    def test_a_flag_flip_invalidates_the_session_on_the_next_call(self):
        from panel.adapters import xui
        server = SimpleNamespace(id=11, host="http://31.14.115.171:2053",
                                 allow_insecure=True, api_token=None)
        xui.XUI_SESSION_CACHE[11] = {"session": object(), "expiry": 9e18,
                                     "auth_key": "|insecure"}
        xui.invalidate_xui_caches(server_id=11, host=server.host)
        self.assertNotIn(11, xui.XUI_SESSION_CACHE)


class BackupTransportTests(unittest.TestCase):
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

    def _server(self, host, allow_insecure):
        return SimpleNamespace(id=1, host=host, allow_insecure=allow_insecure,
                               panel_type="auto", name="s")

    def test_backup_is_refused_for_plaintext_without_the_opt_in(self):
        server = self._server("http://31.14.115.171:2053", False)
        session = _RecordingSession()
        payload, ext, error = backup_service._fetch_xui_backup(session, server)
        self.assertIsNone(payload)
        self.assertIn("plaintext", (error or "").lower())
        self.assertEqual(session.calls, [], "no request may be made")

    def test_backup_uses_the_opt_in_for_plaintext(self):
        server = self._server("http://31.14.115.171:2053", True)
        session = _RecordingSession()
        backup_service._fetch_xui_backup(session, server)
        self.assertTrue(session.calls, "the backup should attempt a download")
        self.assertIs(session.calls[0][2]["verify"], False)

    def test_backup_skips_certificate_verification_only_for_the_opt_in(self):
        server = self._server("https://self-signed.example.com", True)
        session = _RecordingSession()
        backup_service._fetch_xui_backup(session, server)
        self.assertTrue(session.calls)
        self.assertIs(session.calls[0][2]["verify"], False)

        strict = self._server("https://panel.example.com", False)
        strict_session = _RecordingSession()
        backup_service._fetch_xui_backup(strict_session, strict)
        self.assertTrue(strict_session.calls)
        self.assertTrue(strict_session.calls[0][2]["verify"])


if __name__ == "__main__":
    unittest.main()
