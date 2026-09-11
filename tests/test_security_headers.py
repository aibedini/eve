"""Phase 9 tests: response security headers and the authenticated cache policy."""
import os
import re
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import Admin, app, db  # noqa: E402


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_baseline_headers_are_present_on_html(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertTrue((response.content_type or "").startswith("text/html"))
        headers = response.headers
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "same-origin")
        self.assertEqual(headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(headers.get("X-Permitted-Cross-Domain-Policies"), "none")
        self.assertEqual(headers.get("Cross-Origin-Opener-Policy"), "same-origin")
        self.assertEqual(headers.get("X-XSS-Protection"), "0")

    def test_permissions_policy_denies_unused_features_and_allows_panel_ones(self):
        policy = self.client.get("/login").headers.get("Permissions-Policy") or ""
        for feature in ("camera=()", "microphone=()", "geolocation=()", "usb=()",
                        "payment=()", "display-capture=()"):
            self.assertIn(feature, policy)
        for allowed in ("clipboard-write=(self)", "fullscreen=(self)",
                        "publickey-credentials-get=(self)",
                        "publickey-credentials-create=(self)"):
            self.assertIn(allowed, policy)

    def test_csp_is_nonce_based_and_locked_to_self(self):
        csp = self.client.get("/login").headers.get("Content-Security-Policy") or ""
        for directive in ("default-src 'self'", "base-uri 'self'", "object-src 'none'",
                          "frame-ancestors 'self'", "form-action 'self'",
                          "img-src 'self' data:", "font-src 'self' data:",
                          "manifest-src 'self'"):
            self.assertIn(directive, csp)
        self.assertRegex(csp, r"script-src 'self' 'nonce-[A-Za-z0-9_\-]+'")
        self.assertNotIn("script-src 'unsafe-inline'", csp)

    def test_csp_nonce_matches_the_rendered_document(self):
        response = self.client.get("/login")
        csp = response.headers.get("Content-Security-Policy") or ""
        match = re.search(r"'nonce-([A-Za-z0-9_\-]+)'", csp)
        self.assertIsNotNone(match, csp)
        self.assertIn('nonce="' + match.group(1) + '"', response.get_data(as_text=True))

    def test_json_responses_have_no_csp_but_keep_nosniff(self):
        response = self.client.get("/api/me/permissions")
        self.assertEqual(response.status_code, 401)
        self.assertTrue((response.content_type or "").startswith("application/json"))
        self.assertNotIn("Content-Security-Policy", response.headers)
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")

    def test_404_html_still_gets_the_baseline_headers(self):
        response = self.client.get("/definitely-not-a-route")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "SAMEORIGIN")

    def test_hsts_only_on_secure_production_requests(self):
        client = app.test_client()
        with mock.patch.dict(os.environ, {"FLASK_ENV": "production"}):
            secure = client.get("/login", base_url="https://panel.example.com")
            self.assertIn("max-age=31536000",
                          secure.headers.get("Strict-Transport-Security") or "")
            plain = client.get("/login", base_url="http://panel.example.com")
            self.assertIsNone(plain.headers.get("Strict-Transport-Security"))
            with mock.patch.dict(os.environ, {"EVE_HSTS_PRELOAD": "1"}):
                preload = client.get("/login", base_url="https://panel.example.com")
                self.assertIn("preload",
                              preload.headers.get("Strict-Transport-Security") or "")

    def test_hsts_is_absent_in_development(self):
        response = self.client.get("/login", base_url="https://panel.example.com")
        self.assertIsNone(response.headers.get("Strict-Transport-Security"))


class AuthenticatedCachePolicyTests(unittest.TestCase):
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
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(username="headers-admin", role="admin", enabled=True)
        self.admin.set_password("CorrectHorseBattery1!")
        db.session.add(self.admin)
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = self.admin.id
            sess["role"] = self.admin.role
            sess["is_superadmin"] = False

    def test_authenticated_api_is_private_and_not_stored(self):
        response = self.client.get("/api/me/permissions")
        self.assertEqual(response.status_code, 200, response.data)
        cache = response.headers.get("Cache-Control") or ""
        self.assertIn("no-store", cache)
        self.assertIn("private", cache)
        self.assertIn("Cookie", response.headers.get("Vary") or "")

    def test_authenticated_dashboard_is_not_stored(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("no-store", response.headers.get("Cache-Control") or "")

    def test_unauthenticated_responses_are_not_forced_private(self):
        anonymous = app.test_client()
        response = anonymous.get("/login")
        self.assertNotIn("no-store", response.headers.get("Cache-Control") or "")

    def test_static_assets_keep_their_own_cache_policy(self):
        response = self.client.get("/static/jquery-3.6.0.min.js")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("no-store", response.headers.get("Cache-Control") or "")


if __name__ == "__main__":
    unittest.main()
