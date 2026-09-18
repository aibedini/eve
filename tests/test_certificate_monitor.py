"""Phase 8 tests: TLS certificate monitoring for the panel and its endpoints."""
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from app import Admin, HealthLog, Server, SystemSetting, app, db  # noqa: E402
from panel.jobs import schedulers  # noqa: E402
from panel.services import certificates  # noqa: E402
from panel.services import xui_compat  # noqa: E402


def _write_cert(directory, name="cert.pem", common_name="localhost", days=90,
                include_ip=True, expired=False):
    """Create a self-signed PEM certificate/key pair and return their paths."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.utcnow()
    if expired:
        not_before = now - timedelta(days=30)
        not_after = now - timedelta(days=1)
    else:
        not_before = now - timedelta(days=1)
        not_after = now + timedelta(days=days)
    sans = [x509.DNSName(common_name)]
    if include_ip:
        sans.append(x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")))
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(directory, name)
    key_path = os.path.join(directory, name.replace(".pem", ".key"))
    with open(cert_path, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _TlsEchoServer:
    """Minimal TLS listener used to probe a real handshake."""

    def __init__(self, cert_path, key_path):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert_path, key_path)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(conn, server_side=True) as tls:
                    tls.settimeout(2)
                    try:
                        tls.recv(1)
                    except Exception:
                        pass
            except Exception:
                pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)


class CertificateThresholdTests(unittest.TestCase):
    def test_state_boundaries(self):
        self.assertEqual(certificates.certificate_state(30, warn=21, crit=7), "ok")
        self.assertEqual(certificates.certificate_state(10, warn=21, crit=7), "warning")
        self.assertEqual(certificates.certificate_state(3, warn=21, crit=7), "critical")
        self.assertEqual(certificates.certificate_state(0, warn=21, crit=7), "critical")
        self.assertEqual(certificates.certificate_state(-1, warn=21, crit=7), "expired")
        self.assertEqual(certificates.certificate_state(None), "unknown")

    def test_aware_values_are_normalised_to_utc_not_local_time(self):
        aware = datetime(2030, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=3, minutes=30)))
        self.assertEqual(certificates._as_utc(aware), datetime(2030, 1, 1, 8, 30))
        naive = datetime(2030, 1, 1, 12, 0)
        self.assertEqual(certificates._as_utc(naive), naive)

    def test_thresholds_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"EVE_CERT_WARN_DAYS": "40", "EVE_CERT_CRIT_DAYS": "10"}):
            self.assertEqual(certificates.warn_days(), 40)
            self.assertEqual(certificates.critical_days(), 10)
            self.assertEqual(certificates.certificate_state(20), "warning")
        with mock.patch.dict(os.environ, {"EVE_CERT_WARN_DAYS": "not-a-number"}):
            self.assertEqual(certificates.warn_days(), certificates.DEFAULT_WARN_DAYS)


class CertificateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_missing_configuration_is_reported(self):
        report = certificates.inspect_certificate_file(None)
        self.assertEqual(report["error_code"], "not_configured")
        self.assertEqual(report["state"], "unknown")

    def test_missing_file_is_reported(self):
        report = certificates.inspect_certificate_file(
            os.path.join(self._tmp.name, "absent.pem"))
        self.assertEqual(report["error_code"], "missing")
        self.assertFalse(report["present"])

    def test_valid_file_is_parsed(self):
        cert_path, _key = _write_cert(self._tmp.name, days=90)
        report = certificates.inspect_certificate_file(cert_path)
        self.assertEqual(report["state"], "ok")
        self.assertTrue(report["readable"])
        self.assertEqual(report["subject_cn"], "localhost")
        self.assertTrue(report["self_signed"])
        self.assertIn("localhost", report["sans"])
        self.assertGreater(report["days_remaining"], 80)
        self.assertEqual(len(report["fingerprint_sha256"]), 64)

    def test_expired_file_is_reported(self):
        cert_path, _key = _write_cert(self._tmp.name, name="expired.pem", expired=True)
        report = certificates.inspect_certificate_file(cert_path)
        self.assertEqual(report["state"], "expired")
        self.assertLess(report["days_remaining"], 0)

    def test_now_injection_shifts_the_state(self):
        cert_path, _key = _write_cert(self._tmp.name, name="soon.pem", days=30)
        baseline = certificates.inspect_certificate_file(cert_path)
        # Move "now" to exactly five days before the parsed expiry.
        later = datetime.fromisoformat(baseline["not_after"]) - timedelta(days=15)
        report = certificates.inspect_certificate_file(cert_path, now=later)
        self.assertEqual(report["days_remaining"], 15)
        self.assertEqual(report["state"], "warning")  # below warn (21), above crit (7)

    def test_garbage_file_is_not_a_certificate(self):
        path = os.path.join(self._tmp.name, "garbage.pem")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not a certificate")
        report = certificates.inspect_certificate_file(path)
        self.assertEqual(report["error_code"], "invalid_pem")
        self.assertEqual(report["state"], "error")

    def test_report_never_contains_private_key_material(self):
        cert_path, _key = _write_cert(self._tmp.name, name="private.pem")
        report = certificates.inspect_certificate_file(cert_path)
        self.assertNotIn("PRIVATE KEY", json.dumps(report))


class ProbeEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_invalid_targets_are_rejected_without_connecting(self):
        self.assertEqual(certificates.probe_tls_endpoint("")["error_code"], "invalid_host")
        self.assertEqual(
            certificates.probe_tls_endpoint("http://example.com")["error_code"], "not_https")

    def test_closed_port_is_a_network_error(self):
        result = certificates.probe_tls_endpoint(f"https://127.0.0.1:{_free_port()}",
                                                 timeout=2.0)
        # A refused connection is 'unreachable'; an OS that silently drops it is
        # 'timeout'. Both are network errors, never a verified certificate.
        self.assertIn(result["error_code"], ("unreachable", "timeout"))
        self.assertEqual(result["state"], "error")
        self.assertFalse(result["verified"])

    def test_verified_handshake_reports_expiry(self):
        cert_path, key_path = _write_cert(self._tmp.name, name="server.pem", days=120)
        server = _TlsEchoServer(cert_path, key_path)
        self.addCleanup(server.close)
        result = certificates.probe_tls_endpoint(
            f"https://localhost:{server.port}", ca_bundle=cert_path, timeout=5.0)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["subject_cn"], "localhost")
        self.assertGreater(result["days_remaining"], 100)

    def test_untrusted_certificate_is_a_verification_failure(self):
        cert_path, key_path = _write_cert(self._tmp.name, name="untrusted.pem")
        server = _TlsEchoServer(cert_path, key_path)
        self.addCleanup(server.close)
        result = certificates.probe_tls_endpoint(f"https://localhost:{server.port}", timeout=5.0)
        self.assertFalse(result["verified"])
        self.assertEqual(result["state"], "error")
        self.assertEqual(result["error_code"], "verification_failed")

    def test_hostname_mismatch_is_reported_as_such(self):
        cert_path, key_path = _write_cert(self._tmp.name, name="nomatch.pem",
                                          include_ip=False)
        server = _TlsEchoServer(cert_path, key_path)
        self.addCleanup(server.close)
        result = certificates.probe_tls_endpoint(
            f"https://127.0.0.1:{server.port}", ca_bundle=cert_path, timeout=5.0)
        self.assertFalse(result["verified"])
        self.assertEqual(result["error_code"], "hostname_mismatch")

    def test_build_report_summarises_every_entry(self):
        cert_path, key_path = _write_cert(self._tmp.name, name="summary.pem", expired=True)
        server = _TlsEchoServer(cert_path, key_path)
        self.addCleanup(server.close)
        report = certificates.build_tls_report(
            cert_path=cert_path, endpoints=[f"https://localhost:{server.port}"],
            ca_bundle=cert_path,
        )
        # The expired cert is counted twice: the local file and the endpoint
        # that presents it both fail verification as expired.
        self.assertEqual(report["summary"]["expired"], 2)
        self.assertFalse(report["healthy"])
        self.assertEqual(report["thresholds"]["warn_days"], certificates.warn_days())

    def test_cache_is_reused_until_refreshed(self):
        certificates.invalidate_cache()
        calls = {"n": 0}

        def fake_build(**kwargs):
            calls["n"] += 1
            return {"checked_at": "x", "summary": {}, "healthy": True}

        with mock.patch.object(certificates, "build_tls_report", fake_build):
            first = certificates.get_tls_report(refresh=True)
            second = certificates.get_tls_report()
            third = certificates.get_tls_report(refresh=True)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(first, second)
        self.assertNotEqual(third, None)
        certificates.invalidate_cache()


class DoctorApiTests(unittest.TestCase):
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
        HealthLog.query.delete()
        SystemSetting.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(username="doctor-admin", role="admin", enabled=True)
        self.admin.set_password("CorrectHorseBattery1!")
        self.reseller = Admin(username="doctor-reseller", role="reseller", enabled=True,
                              allowed_servers="[]")
        self.reseller.set_password("CorrectHorseBattery1!")
        db.session.add_all([self.admin, self.reseller])
        db.session.commit()
        certificates.invalidate_cache()
        xui_compat.COMPAT_CACHE.clear()
        self.client = app.test_client()

    def _login(self, admin):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = admin.id
            sess["role"] = admin.role
            sess["is_superadmin"] = False

    def test_reseller_cannot_read_the_doctor(self):
        self._login(self.reseller)
        self.assertEqual(self.client.get("/api/doctor/tls").status_code, 403)
        self.assertEqual(self.client.get("/api/doctor").status_code, 403)

    def test_tls_report_uses_the_saved_certificate_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, _key = _write_cert(tmp, name="panel.pem", days=10)
            db.session.add(SystemSetting(key="ssl_cert_path", value=cert_path))
            db.session.commit()
            certificates.invalidate_cache()
            self._login(self.admin)
            response = self.client.get("/api/doctor/tls")
            self.assertEqual(response.status_code, 200, response.data)
            body = response.get_json()
            self.assertTrue(body["success"])
            self.assertEqual(body["local"]["state"], "warning")
            self.assertEqual(body["local"]["path"], cert_path)
            self.assertIn("summary", body)
            self.assertNotIn("PRIVATE KEY", response.get_data(as_text=True))
            refreshed = self.client.get("/api/doctor/tls?refresh=1")
            self.assertEqual(refreshed.status_code, 200)

    def test_doctor_summary_shape(self):
        self._login(self.admin)
        response = self.client.get("/api/doctor")
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        self.assertTrue(body["success"])
        self.assertIn(
            body["state"],
            ("ok", "warning", "degraded", "error", "critical", "unknown"),
        )
        for name in ("database", "disk", "secret_key", "tls"):
            self.assertIn(name, body["checks"])
        self.assertIn("certificate_thresholds", body)
        self.assertEqual(body["checks"]["database"]["state"], "ok")

    def test_authenticated_doctor_exposes_compatibility_without_credentials(self):
        server = Server(
            name='Doctor 3.8',
            host='https://panel.example',
            username='panel-admin',
            password='doctor-password-secret',
            api_token='doctor-token-secret',
            sub_path='/subscription-secret-path/',
            panel_type='v3',
        )
        db.session.add(server)
        db.session.commit()
        compat = xui_compat.resolve_compatibility(
            server.id,
            '3.8.0',
            source=xui_compat.SOURCE_SERVER_STATUS,
            confidence=xui_compat.CONF_AUTHORITATIVE,
        )
        xui_compat.remember_compatibility(
            xui_compat.compat_with_warning(
                compat,
                xui_compat.WARN_SCOPE_INSUFFICIENT,
            )
        )

        self._login(self.admin)
        response = self.client.get('/api/doctor')

        self.assertEqual(response.status_code, 200, response.data)
        panel = response.get_json()['checks']['panel_compatibility']['panels'][0]
        self.assertEqual(panel['detected_version'], '3.8.0')
        self.assertEqual(panel['profile'], 'xui_3_8')
        self.assertEqual(panel['detection_source'], 'server_status')
        self.assertEqual(panel['certification'], 'supported')
        self.assertEqual(panel['auth_state'], 'scope_insufficient')
        serialized = response.get_data(as_text=True)
        for secret in (
            'doctor-password-secret',
            'doctor-token-secret',
            'subscription-secret-path',
            'PRIVATE KEY',
        ):
            self.assertNotIn(secret, serialized)


class HealthWatchdogCertificateTests(unittest.TestCase):
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
        HealthLog.query.delete()
        db.session.commit()
        certificates.invalidate_cache()
        schedulers._CERT_CHECK_STATE["last_run"] = 0.0
        schedulers._CERT_CHECK_STATE["summary"] = None

    def test_unhealthy_certificate_writes_a_tls_health_log(self):
        report = {
            "checked_at": "2026-01-01T00:00:00Z",
            "thresholds": {"warn_days": 21, "critical_days": 7},
            "local": {"source": "file", "path": "/etc/ssl/eve/cert.pem", "state": "critical",
                      "days_remaining": 3, "error_code": None},
            "endpoints": [],
            "summary": {"critical": 1},
            "healthy": False,
        }
        with mock.patch.object(certificates, "build_tls_report", lambda **kwargs: report):
            ok, detail = schedulers._health_check_certificates(force=True)
        self.assertFalse(ok)
        rows = HealthLog.query.filter_by(category="tls").all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].level, "critical")
        self.assertIn("/etc/ssl/eve/cert.pem", rows[0].message)
        self.assertNotIn("PRIVATE", rows[0].message)

    def test_healthy_report_writes_nothing(self):
        report = {
            "checked_at": "2026-01-01T00:00:00Z",
            "thresholds": {"warn_days": 21, "critical_days": 7},
            "local": {"source": "file", "path": "/etc/ssl/eve/cert.pem", "state": "ok",
                      "days_remaining": 200, "error_code": None},
            "endpoints": [],
            "summary": {"ok": 1},
            "healthy": True,
        }
        with mock.patch.object(certificates, "build_tls_report", lambda **kwargs: report):
            ok, detail = schedulers._health_check_certificates(force=True)
        self.assertTrue(ok)
        self.assertEqual(HealthLog.query.filter_by(category="tls").count(), 0)

    def test_failure_never_breaks_the_health_cycle(self):
        with mock.patch.object(certificates, "build_tls_report",
                               side_effect=RuntimeError("boom")):
            ok, detail = schedulers._health_check_certificates(force=True)
        self.assertTrue(ok)
        self.assertIn("boom", str(detail))

    def test_cycle_includes_the_tls_check(self):
        with mock.patch.object(schedulers, "_health_check_certificates",
                               lambda force=False: (True, "stub")):
            results = schedulers._run_single_health_cycle()
        self.assertIn("tls", results)


if __name__ == "__main__":
    unittest.main()
