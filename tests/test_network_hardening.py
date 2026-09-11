"""Phase 7 tests: inbound proxy trust, client identity and panel transport."""
import ast
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_FILE.name.replace(os.sep, chr(47))}")
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import Admin, AuditLog, Server, app, db  # noqa: E402
from panel.security import network as net  # noqa: E402
from panel.security.network import (  # noqa: E402
    InsecurePanelTransportError, TrustedProxyMiddleware, enforce_panel_transport,
    inspect_panel_url, is_loopback_host, peer_is_trusted, resolve_client_ip,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_PEER = "8.8.8.8"
CLIENT_IP = "1.2.3.4"


class EnvIsolatedTestCase(unittest.TestCase):
    """Reset the two network policy variables around every test."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(net.TRUSTED_PROXIES_ENV, None)
        os.environ.pop(net.INSECURE_PANEL_ENV, None)


class PanelTransportPolicyTests(EnvIsolatedTestCase):
    def test_https_is_always_allowed(self):
        info = enforce_panel_transport("https://panel.example.com:2053/base")
        self.assertEqual(info["scheme"], "https")
        self.assertFalse(info["loopback"])

    def test_loopback_plaintext_is_allowed(self):
        for url in ("http://127.0.0.1:2053", "http://localhost:2053/x", "http://[::1]:2053"):
            info = enforce_panel_transport(url)
            self.assertTrue(info["loopback"], url)

    def test_remote_plaintext_is_refused(self):
        for url in ("http://panel.example.com", "http://10.0.0.5:2053", "http://192.168.1.9"):
            with self.assertRaises(InsecurePanelTransportError):
                enforce_panel_transport(url)

    def test_opt_in_allows_remote_plaintext(self):
        with mock.patch.dict(os.environ, {net.INSECURE_PANEL_ENV: "1"}):
            info = enforce_panel_transport("http://10.0.0.5:2053")
            self.assertFalse(info["loopback"])

    def test_missing_scheme_is_refused(self):
        with self.assertRaises(InsecurePanelTransportError):
            enforce_panel_transport("panel.example.com:2053")

    def test_error_message_is_actionable_and_has_no_credentials(self):
        with self.assertRaises(InsecurePanelTransportError) as caught:
            enforce_panel_transport("http://panel.example.com")
        message = str(caught.exception)
        self.assertIn("https://", message)
        self.assertIn(net.INSECURE_PANEL_ENV, message)

    def test_inspect_handles_bad_port_without_raising(self):
        info = inspect_panel_url("https://panel.example.com:not-a-port")
        self.assertIsNone(info["port"])
        self.assertEqual(info["host"], "panel.example.com")

    def test_loopback_hosts(self):
        for host in ("127.0.0.1", "127.9.9.9", "::1", "localhost", "app.localhost"):
            self.assertTrue(is_loopback_host(host), host)
        for host in ("panel.example.com", "10.0.0.5", "", None):
            self.assertFalse(is_loopback_host(host), host)


class TrustedProxyPolicyTests(EnvIsolatedTestCase):
    def test_private_and_loopback_peers_are_trusted_by_default(self):
        for peer in ("127.0.0.1", "::1", "172.18.0.1", "10.1.2.3", "192.168.0.7"):
            self.assertTrue(peer_is_trusted(peer), peer)
        self.assertFalse(peer_is_trusted(PUBLIC_PEER))
        self.assertFalse(peer_is_trusted("not-an-ip"))

    def test_forwarded_for_is_used_only_from_a_trusted_peer(self):
        self.assertEqual(resolve_client_ip("127.0.0.1", CLIENT_IP), CLIENT_IP)
        self.assertEqual(resolve_client_ip(PUBLIC_PEER, CLIENT_IP), PUBLIC_PEER)

    def test_the_chain_is_walked_right_to_left_skipping_proxies(self):
        chain = f"{CLIENT_IP}, 10.0.0.5"
        self.assertEqual(resolve_client_ip("172.18.0.1", chain), CLIENT_IP)
        self.assertEqual(resolve_client_ip("127.0.0.1", "10.0.0.5"), "10.0.0.5")

    def test_explicit_proxy_list_narrows_trust(self):
        with mock.patch.dict(os.environ, {net.TRUSTED_PROXIES_ENV: "10.9.9.9"}):
            self.assertTrue(peer_is_trusted("10.9.9.9"))
            self.assertFalse(peer_is_trusted("127.0.0.1"))
            self.assertEqual(resolve_client_ip("10.9.9.9", CLIENT_IP), CLIENT_IP)
            self.assertEqual(resolve_client_ip("127.0.0.1", CLIENT_IP), "127.0.0.1")

    def test_trust_any_peer_restores_the_legacy_behaviour(self):
        with mock.patch.dict(os.environ, {net.TRUSTED_PROXIES_ENV: "*"}):
            self.assertTrue(peer_is_trusted(PUBLIC_PEER))
            self.assertEqual(resolve_client_ip(PUBLIC_PEER, CLIENT_IP), CLIENT_IP)

    def test_invalid_proxy_entry_is_ignored(self):
        with mock.patch.dict(os.environ, {net.TRUSTED_PROXIES_ENV: "not-a-cidr"}):
            self.assertEqual(net.trusted_proxy_policy(), ())
            self.assertFalse(peer_is_trusted("127.0.0.1"))

    def _run(self, environ):
        captured = {}

        def inner(env, start_response):
            captured.update(env)
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        body = TrustedProxyMiddleware(inner)(environ, lambda *args: None)
        self.assertEqual(list(body), [b"ok"])
        return captured

    def test_untrusted_peer_forwarded_headers_are_stripped(self):
        captured = self._run({
            "REMOTE_ADDR": PUBLIC_PEER,
            "HTTP_X_FORWARDED_FOR": CLIENT_IP,
            "HTTP_X_FORWARDED_HOST": "evil.example",
            "HTTP_X_FORWARDED_PROTO": "https",
        })
        self.assertEqual(captured["eve.client_ip"], PUBLIC_PEER)
        self.assertEqual(captured["eve.peer_addr"], PUBLIC_PEER)
        for header in net.FORWARDED_HEADERS:
            self.assertNotIn(header, captured)

    def test_trusted_peer_keeps_forwarded_headers_for_proxyfix(self):
        captured = self._run({
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_X_FORWARDED_FOR": CLIENT_IP,
            "HTTP_X_FORWARDED_HOST": "panel.example",
            "HTTP_X_FORWARDED_PROTO": "https",
        })
        self.assertEqual(captured["eve.client_ip"], CLIENT_IP)
        self.assertEqual(captured["HTTP_X_FORWARDED_HOST"], "panel.example")


class ServerHostValidationTests(unittest.TestCase):
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
        AuditLog.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(username="net-admin", role="admin", enabled=True)
        self.admin.set_password("CorrectHorseBattery1!")
        db.session.add(self.admin)
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = self.admin.id
            sess["role"] = self.admin.role
            sess["is_superadmin"] = False

    def _payload(self, host):
        return {"name": "Panel", "host": host, "username": "u", "password": "p",
                "panel_type": "auto"}

    def test_remote_plaintext_server_is_rejected(self):
        response = self.client.post("/api/servers", json=self._payload("http://panel.example.com"))
        body = response.get_json()
        self.assertFalse(body["success"])
        self.assertEqual(response.headers.get("X-Eve-Status"), "400")
        self.assertIn("https://", body["error"])
        self.assertEqual(Server.query.count(), 0)

    def test_loopback_plaintext_server_is_accepted(self):
        response = self.client.post("/api/servers", json=self._payload("http://127.0.0.1:2053"))
        self.assertTrue(response.get_json()["success"], response.data)
        self.assertEqual(Server.query.count(), 1)

    def test_https_server_is_accepted(self):
        response = self.client.post("/api/servers", json=self._payload("https://panel.example.com"))
        self.assertTrue(response.get_json()["success"], response.data)

    def test_update_rejects_a_plaintext_host_and_keeps_the_old_value(self):
        server = Server(name="Keep", host="https://panel.example.com", username="u",
                        password="p", panel_type="auto")
        db.session.add(server)
        db.session.commit()
        response = self.client.put(f"/api/servers/{server.id}",
                                   json={"host": "http://remote.example.com"})
        self.assertFalse(response.get_json()["success"])
        db.session.expire_all()
        self.assertEqual(db.session.get(Server, server.id).host, "https://panel.example.com")

    def test_update_without_a_host_still_succeeds(self):
        server = Server(name="Keep", host="http://10.0.0.5:2053", username="u",
                        password="p", panel_type="auto")
        db.session.add(server)
        db.session.commit()
        response = self.client.put(f"/api/servers/{server.id}", json={"name": "Renamed"})
        self.assertTrue(response.get_json()["success"], response.data)
        db.session.expire_all()
        self.assertEqual(db.session.get(Server, server.id).name, "Renamed")


class ClientIdentityRequestTests(EnvIsolatedTestCase):
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
        super().setUp()
        AuditLog.query.delete()
        db.session.commit()
        self.client = app.test_client()

    def _failed_login(self, **kwargs):
        response = self.client.post("/login", json={"username": "ghost", "password": "nope"},
                                    **kwargs)
        self.assertNotEqual(response.status_code, 500)
        row = AuditLog.query.filter_by(action="auth.login.failed").order_by(
            AuditLog.id.desc()).first()
        self.assertIsNotNone(row)
        return json.loads(row.meta_json or "{}")

    def test_untrusted_peer_cannot_spoof_its_address_in_the_audit_trail(self):
        meta = self._failed_login(headers={"X-Forwarded-For": CLIENT_IP},
                                  environ_base={"REMOTE_ADDR": PUBLIC_PEER})
        self.assertEqual(meta.get("ip"), PUBLIC_PEER)

    def test_forwarded_for_is_honoured_for_the_local_proxy(self):
        meta = self._failed_login(headers={"X-Forwarded-For": CLIENT_IP})
        self.assertEqual(meta.get("ip"), CLIENT_IP)


class XuiTransportEnforcementTests(unittest.TestCase):
    def test_plaintext_remote_panel_never_gets_a_credential_session(self):
        from panel.adapters.xui import get_xui_cookie_session, get_xui_session
        server = SimpleNamespace(id=9901, host="http://panel.example.com", panel_type="auto")
        session_obj, error = get_xui_session(server)
        self.assertIsNone(session_obj)
        self.assertIn("plaintext", error.lower())
        self.assertIsNone(get_xui_cookie_session(
            "http://panel.example.com", "u", "p"))

    def test_loopback_plaintext_panel_passes_the_policy(self):
        self.assertTrue(inspect_panel_url("http://127.0.0.1:2053")["loopback"])


class OutboundTlsGuardTests(unittest.TestCase):
    """Keep the documented egress property true by construction."""

    HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}

    ROOT_MODULES = (
        "app.py", "pulse.py", "pulse_agent.py", "telegram_bot_worker.py",
        "telegram_bot_runtime.py", "telegram_diagnostics.py", "telegram_xray.py",
        "maintenance.py", "migrate_db.py",
    )

    def _app_files(self):
        for name in self.ROOT_MODULES:
            path = os.path.join(REPO_ROOT, name)
            if os.path.isfile(path):
                yield path
        for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_ROOT, "panel")):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)

    def test_no_verification_is_ever_disabled_and_every_call_times_out(self):
        offenders = []
        for path in self._app_files():
            rel = os.path.relpath(path, REPO_ROOT)
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if (keyword.arg == "verify"
                                and isinstance(keyword.value, ast.Constant)
                                and keyword.value.value is False):
                            offenders.append(f"{rel}:{node.lineno} verify=False")
                    func = node.func
                    if (isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "requests"
                            and func.attr in self.HTTP_METHODS):
                        if not any(k.arg == "timeout" for k in node.keywords):
                            offenders.append(
                                f"{rel}:{node.lineno} requests.{func.attr} without timeout")
                if isinstance(node, ast.Attribute) and node.attr in (
                        "CERT_NONE", "_create_unverified_context"):
                    offenders.append(f"{rel}:{node.lineno} ssl.{node.attr}")
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Attribute)
                        and node.targets[0].attr == "verify"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is False):
                    offenders.append(f"{rel}:{node.lineno} .verify = False")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
