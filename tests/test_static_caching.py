"""Phase 21 tests: static asset versioning and cache policy."""
import os
import tempfile
import unittest

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from flask import url_for  # noqa: E402

from app import (  # noqa: E402
    Admin,
    STATIC_IMMUTABLE_SECONDS,
    STATIC_LONG_LIVED_SECONDS,
    _env_int_or,
    _static_asset_version,
    app,
    db,
)


class StaticVersionFunctionTests(unittest.TestCase):
    def test_url_for_appends_a_content_version(self):
        with app.test_request_context("/"):
            value = url_for("static", filename="style.css")
        self.assertTrue(value.startswith("/static/style.css?v="), value)
        self.assertEqual(_static_asset_version("style.css"), value.split("v=", 1)[1])

    def test_version_is_stable_between_calls(self):
        self.assertEqual(
            _static_asset_version("style.css"), _static_asset_version("style.css"))

    def test_missing_file_has_no_version(self):
        self.assertIsNone(_static_asset_version("definitely-missing.js"))

    def test_an_explicit_version_is_not_overwritten(self):
        with app.test_request_context("/"):
            value = url_for("static", filename="style.css", v="custom")
        self.assertIn("v=custom", value)
        self.assertNotIn("v=" + str(_static_asset_version("style.css")), value)

    def test_env_override_parsing(self):
        self.assertEqual(_env_int_or("EVE_TEST_MISSING_INT", 42), 42)
        os.environ["EVE_TEST_POSITIVE_INT"] = "7"
        self.addCleanup(os.environ.pop, "EVE_TEST_POSITIVE_INT", None)
        self.assertEqual(_env_int_or("EVE_TEST_POSITIVE_INT", 42), 7)
        os.environ["EVE_TEST_NEGATIVE_INT"] = "-3"
        self.addCleanup(os.environ.pop, "EVE_TEST_NEGATIVE_INT", None)
        self.assertEqual(_env_int_or("EVE_TEST_NEGATIVE_INT", 42), 42)
        os.environ["EVE_TEST_BAD_INT"] = "not-a-number"
        self.addCleanup(os.environ.pop, "EVE_TEST_BAD_INT", None)
        self.assertEqual(_env_int_or("EVE_TEST_BAD_INT", 42), 42)


class StaticCacheHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_versioned_asset_is_immutable_for_a_year(self):
        version = _static_asset_version("style.css")
        response = self.client.get("/static/style.css?v=" + version)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("Cache-Control"),
            "public, max-age=%d, immutable" % STATIC_IMMUTABLE_SECONDS)

    def test_unversioned_stylesheet_keeps_revalidating(self):
        response = self.client.get("/static/style.css")
        self.assertEqual(response.status_code, 200)
        cache_control = response.headers.get("Cache-Control") or ""
        self.assertNotIn("immutable", cache_control)
        self.assertNotIn("no-store", cache_control)

    def test_unversioned_font_gets_a_long_lived_ttl(self):
        response = self.client.get("/static/fonts/Inter-400.ttf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("Cache-Control"),
            "public, max-age=%d" % STATIC_LONG_LIVED_SECONDS)

    def test_static_assets_are_never_marked_no_store(self):
        for path in ("/static/style.css", "/static/jquery-3.6.0.min.js",
                     "/static/fonts/fonts.css"):
            cache_control = self.client.get(path).headers.get("Cache-Control") or ""
            self.assertNotIn("no-store", cache_control, path)

    def test_missing_asset_does_not_get_the_immutable_policy(self):
        response = self.client.get("/static/nope.css?v=1")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("immutable", response.headers.get("Cache-Control") or "")


class RenderedAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username="asset-admin", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        db.session.add(cls.admin)
        db.session.commit()
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_dashboard_references_versioned_static_urls(self):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = self.admin.id
            sess["role"] = "admin"
            sess["is_superadmin"] = False
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_data(as_text=True)
        self.assertIn("/static/style.css?v=", body)
        self.assertIn("/static/jquery-3.6.0.min.js?v=", body)


if __name__ == "__main__":
    unittest.main()
