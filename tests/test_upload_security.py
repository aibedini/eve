"""Phase 25 tests: upload content validation and the sandboxed serving policy."""
import base64
import io
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from werkzeug.datastructures import FileStorage  # noqa: E402

from app import Admin, app, db  # noqa: E402
from panel.security.uploads import (  # noqa: E402
    MAX_IMAGE_PIXELS,
    extension_of,
    is_upload_path,
    serve_policy,
    sniff_kind,
    validate_extension_content,
    validate_image_upload,
)

# 1x1 transparent PNG.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/wD/AL0AAAAASUVORK5CYII=")
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 20


def _storage(filename, data):
    return FileStorage(stream=io.BytesIO(data), filename=filename)


class SniffTests(unittest.TestCase):
    def test_magic_bytes_identify_the_container(self):
        self.assertEqual(sniff_kind(PNG_BYTES), "png")
        self.assertEqual(sniff_kind(JPEG_HEAD), "jpeg")
        self.assertEqual(sniff_kind(b"GIF89a" + b"\x00" * 10), "gif")
        self.assertEqual(sniff_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")
        self.assertEqual(sniff_kind(b"<html><body>hi</body></html>"), "")
        self.assertEqual(sniff_kind(b""), "")

    def test_extension_parsing_is_lowercase_and_safe(self):
        self.assertEqual(extension_of("Photo.PNG"), "png")
        self.assertEqual(extension_of("../../etc/passwd"), "")
        self.assertEqual(extension_of(None), "")

    def test_upload_paths_are_recognised(self):
        self.assertTrue(is_upload_path("/static/uploads/a.png"))
        self.assertTrue(is_upload_path("/static/app-files/a.apk"))
        self.assertFalse(is_upload_path("/static/style.css"))
        self.assertFalse(is_upload_path("/api/upload"))


class ValidateImageUploadTests(unittest.TestCase):
    def test_a_real_png_is_accepted(self):
        ok, reason = validate_image_upload(_storage("logo.png", PNG_BYTES))
        self.assertTrue(ok, reason)

    def test_html_disguised_as_png_is_rejected(self):
        ok, reason = validate_image_upload(_storage("logo.png", b"<html>evil</html>"))
        self.assertFalse(ok)
        self.assertIn("not a recognised image", reason)

    def test_extension_whitelist_rejects_documents_and_binaries(self):
        for name in ("evil.html", "evil.svg", "evil.php", "evil.exe", "evil.js"):
            ok, reason = validate_image_upload(_storage(name, PNG_BYTES))
            self.assertFalse(ok, name)
            self.assertIn("not allowed", reason)

    def test_missing_extension_is_rejected(self):
        ok, reason = validate_image_upload(_storage("noextension", PNG_BYTES))
        self.assertFalse(ok)
        self.assertIn("no extension", reason)

    def test_content_must_match_the_extension(self):
        ok, reason = validate_image_upload(_storage("logo.gif", PNG_BYTES))
        self.assertFalse(ok)
        self.assertIn("does not match", reason)

    def test_file_is_rewound_after_validation(self):
        storage = _storage("logo.png", PNG_BYTES)
        validate_image_upload(storage)
        self.assertEqual(storage.stream.read(), PNG_BYTES)

    def test_dimension_bomb_is_rejected(self):
        storage = _storage("huge.png", PNG_BYTES)
        with mock.patch("panel.security.uploads.image_dimensions",
                        return_value=(60_000, 60_000)):
            ok, reason = validate_image_upload(storage)
        self.assertFalse(ok)
        self.assertIn("too large", reason)
        self.assertGreater(60_000 * 60_000, MAX_IMAGE_PIXELS)

    def test_svg_is_not_an_inline_image(self):
        policy = serve_policy("/static/uploads/icon.svg")
        self.assertEqual(policy["Content-Disposition"], "attachment")
        self.assertIn("sandbox", policy["Content-Security-Policy"])
        self.assertEqual(policy["X-Content-Type-Options"], "nosniff")
        self.assertEqual(serve_policy("/static/uploads/photo.png")["Content-Disposition"],
                         "inline")


class UploadEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = app.test_client()
        cls.created = []

    @classmethod
    def tearDownClass(cls):
        for path in cls.created:
            try:
                os.remove(path)
            except OSError:
                pass
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        admin = Admin(username="upload-root-%d" % id(self), role="superadmin",
                      is_superadmin=True, enabled=True)
        admin.set_password("CorrectHorseBattery1!")
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = admin_id
            sess["role"] = "superadmin"
            sess["is_superadmin"] = True

    def _uploads_dir(self):
        return os.path.join(app.static_folder, "uploads")

    def _post(self, filename, data):
        return self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(data), filename)},
            content_type="multipart/form-data")

    def test_valid_image_is_stored_and_returns_a_url(self):
        response = self._post("logo.png", PNG_BYTES)
        self.assertEqual(response.status_code, 200, response.data)
        url = response.get_json()["url"]
        self.assertTrue(url.startswith("/static/uploads/"), url)
        self.assertNotIn("..", url)
        stored = os.path.join(self._uploads_dir(), os.path.basename(url))
        self.created.append(stored)
        self.assertTrue(os.path.isfile(stored))

    def test_uploaded_document_is_rejected_before_it_is_written(self):
        before = set(os.listdir(self._uploads_dir())) if os.path.isdir(self._uploads_dir()) else set()
        for name in ("evil.html", "evil.svg", "shell.php"):
            response = self._post(name, PNG_BYTES)
            self.assertEqual(response.status_code, 415, (name, response.data))
        after = set(os.listdir(self._uploads_dir())) if os.path.isdir(self._uploads_dir()) else set()
        self.assertEqual(before, after)

    def test_content_mismatch_is_rejected(self):
        response = self._post("logo.png", b"<html>evil</html>")
        self.assertEqual(response.status_code, 415)
        self.assertIn("not a recognised image", response.get_json()["error"])

    def test_oversized_upload_is_rejected(self):
        with mock.patch("app.MAX_FILE_SIZE", 16):
            response = self._post("logo.png", PNG_BYTES)
        self.assertEqual(response.status_code, 413)

    def test_traversal_filename_stays_inside_the_upload_directory(self):
        response = self._post("../../evil.png", PNG_BYTES)
        self.assertEqual(response.status_code, 200, response.data)
        url = response.get_json()["url"]
        self.assertNotIn("/uploads/../", url)
        self.assertNotIn(os.sep, os.path.basename(url))
        self.assertNotIn("/", os.path.basename(url))
        stored = os.path.join(self._uploads_dir(), os.path.basename(url))
        self.created.append(stored)
        self.assertTrue(os.path.realpath(stored).startswith(os.path.realpath(self._uploads_dir())))

    def test_served_upload_is_sandboxed(self):
        probe = os.path.join(self._uploads_dir(), "phase25-serve-probe.svg")
        os.makedirs(self._uploads_dir(), exist_ok=True)
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
        self.created.append(probe)
        response = self.client.get("/static/uploads/phase25-serve-probe.svg")
        self.assertEqual(response.status_code, 200)
        self.assertIn("sandbox", response.headers.get("Content-Security-Policy") or "")
        self.assertEqual(response.headers.get("Content-Disposition"), "attachment")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")

    def test_served_image_is_inline_without_sandbox_leak(self):
        response_upload = self._post("inline.png", PNG_BYTES)
        url = response_upload.get_json()["url"]
        stored = os.path.join(self._uploads_dir(), os.path.basename(url))
        self.created.append(stored)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Disposition"), "inline")


class ExtensionContentTests(unittest.TestCase):
    def test_image_extension_requires_image_bytes(self):
        ok, reason = validate_extension_content(_storage("icon.png", b"<html/>"), "png")
        self.assertFalse(ok)
        self.assertIn("not a recognised image", reason)

    def test_matching_image_container_passes(self):
        ok, reason = validate_extension_content(_storage("icon.png", PNG_BYTES), ".png")
        self.assertTrue(ok, reason)

    def test_unknown_extensions_are_left_to_the_serving_policy(self):
        for ext in (".exe", ".zip", ".mp4", ".svg", ".deb"):
            ok, reason = validate_extension_content(_storage("app" + ext, b"MZ\x00\x00"), ext)
            self.assertTrue(ok, (ext, reason))


class AppFileUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = app.test_client()
        cls.created = []

    @classmethod
    def tearDownClass(cls):
        for path in cls.created:
            try:
                os.remove(path)
            except OSError:
                pass
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        admin = Admin(username="appfile-root-%d" % id(self), role="superadmin",
                      is_superadmin=True, enabled=True)
        admin.set_password("CorrectHorseBattery1!")
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = admin_id
            sess["role"] = "superadmin"
            sess["is_superadmin"] = True

    def _app_files_dir(self):
        return os.path.join(app.static_folder, "app-files")

    def test_disguised_image_is_rejected(self):
        response = self.client.post(
            "/api/app-files/upload",
            data={"file": (io.BytesIO(b"<html>evil</html>"), "icon.png")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 415, response.data)
        self.assertIn("not a recognised image", response.get_json()["error"])

    def test_real_image_is_stored_and_served_as_an_attachment(self):
        response = self.client.post(
            "/api/app-files/upload",
            data={"file": (io.BytesIO(PNG_BYTES), "icon.png")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200, response.data)
        url = response.get_json()["file"]["url"]
        self.assertTrue(url.startswith("/static/app-files/"), url)
        self.created.append(os.path.join(self._app_files_dir(), os.path.basename(url)))
        served = self.client.get(url)
        self.assertEqual(served.status_code, 200)
        self.assertIn("sandbox", served.headers.get("Content-Security-Policy") or "")

    def test_stored_app_file_that_is_not_an_image_downloads(self):
        probe = os.path.join(self._app_files_dir(), "phase25-probe.zip")
        os.makedirs(self._app_files_dir(), exist_ok=True)
        with open(probe, "wb") as handle:
            handle.write(b"PK\x03\x04")
        self.created.append(probe)
        response = self.client.get("/static/app-files/phase25-probe.zip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Disposition"), "attachment")


if __name__ == "__main__":
    unittest.main()
