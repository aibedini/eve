"""The static version is a content identity, and only a matching version is immutable.

This is the fix for the intermittent Subscription layout bug: the version is now the sha256 of
the file's bytes, so every node computes the same key for the same stylesheet, and ``immutable``
is granted only when the requested key equals the file's current content hash - a stale or
unknown key revalidates instead of pinning new bytes under an old name.

The earlier version of this file documented the weakness (mtime+size fingerprint, immutable for
any ``?v=``); the assertions below are the guarantee that replaced it.
"""
import os
import tempfile
import time
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import STATIC_IMMUTABLE_SECONDS, _static_asset_version, app  # noqa: E402


class StaticVersionIdentityTests(unittest.TestCase):
    """The fingerprint identifies content, not a timestamp."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._patch = mock.patch.object(app, '_static_folder', self.folder)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, name, payload, mtime=None):
        path = os.path.join(self.folder, name)
        with open(path, 'wb') as handle:
            handle.write(payload)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_the_same_bytes_keep_one_version_whatever_the_mtime_says(self):
        first = self._write('same.css', b'body{color:red}')
        version_one = _static_asset_version('same.css')
        os.utime(first, (2_000_000_000, 2_000_000_000))
        self.assertEqual(_static_asset_version('same.css'), version_one)
        # A deploy that rewrites the same bytes (or a node whose checkout timestamps differ)
        # therefore keeps every cache valid instead of invalidating it for no reason.
        self.assertRegex(version_one, r'^[0-9a-f]{16}$')

    def test_different_stylesheets_of_the_same_size_get_different_versions(self):
        stamp = 1_900_000_000
        self._write('a.css', b'a{width:24px}', mtime=stamp)
        self._write('b.css', b'b{width:99px}', mtime=stamp)
        self.assertNotEqual(_static_asset_version('a.css'), _static_asset_version('b.css'))

    def test_a_replaced_file_changes_its_version_inside_the_running_process(self):
        path = self._write('live.css', b'.x{width:1px}')
        before = _static_asset_version('live.css')
        time.sleep(0.01)
        with open(path, 'wb') as handle:
            handle.write(b'.x{width:2px}')
        self.assertNotEqual(_static_asset_version('live.css'), before)

    def test_a_missing_or_directory_entry_has_no_version(self):
        self.assertIsNone(_static_asset_version('nope.css'))
        os.makedirs(os.path.join(self.folder, 'sub'), exist_ok=True)
        self.assertIsNone(_static_asset_version('sub'))   # empty: nothing to version

    def test_a_grouped_directory_gets_a_content_version(self):
        """flags/ URLs are built from a directory, so the group needs a version too."""
        group = os.path.join(self.folder, 'flags')
        os.makedirs(group, exist_ok=True)
        with open(os.path.join(group, 'de.svg'), 'wb') as handle:
            handle.write(b'<svg id="de"/>')
        first = _static_asset_version('flags')
        self.assertRegex(first or '', r'^[0-9a-f]{16}$')
        # A child's content change and a freshly deployed directory both move the key.
        time.sleep(0.01)
        with open(os.path.join(group, 'de.svg'), 'wb') as handle:
            handle.write(b'<svg id="de2"/>')
        self.assertNotEqual(_static_asset_version('flags'), first)


class StaleVersionQueryTests(unittest.TestCase):
    """The served bytes are still the file's, but a stale key can never be pinned."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._patch = mock.patch.object(app, '_static_folder', self.folder)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.client = app.test_client()

    def _write(self, payload):
        path = os.path.join(self.folder, 'probe.css')
        with open(path, 'wb') as handle:
            handle.write(payload)
        return path

    def test_the_matching_version_is_immutable(self):
        self._write(b'.probe{width:24px}')
        version = _static_asset_version('probe.css')
        response = self.client.get('/static/probe.css?v=' + version)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get('Cache-Control'),
            'public, max-age=%d, immutable' % STATIC_IMMUTABLE_SECONDS)
        self.assertIsNone(response.headers.get('X-Eve-Stale-Asset-Version'))

    def test_a_stale_version_revalidates_instead_of_being_pinned(self):
        path = self._write(b'.probe{width:24px}')
        stale_version = _static_asset_version('probe.css')
        with open(path, 'wb') as handle:
            handle.write(b'.probe{width:96px}')
        response = self.client.get('/static/probe.css?v=' + stale_version)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), b'.probe{width:96px}')
        # The bytes are still served (Flask ignores the query string), but no cache may pin
        # them under the stale key: the response must revalidate every time.
        cache_control = response.headers.get('Cache-Control') or ''
        self.assertNotIn('immutable', cache_control)
        self.assertIn('must-revalidate', cache_control)
        self.assertEqual(response.headers.get('X-Eve-Stale-Asset-Version'), '1')

    def test_an_unknown_version_is_not_treated_as_fingerprinted(self):
        self._write(b'.probe{width:24px}')
        response = self.client.get('/static/probe.css?v=whatever')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('immutable', response.headers.get('Cache-Control') or '')
        self.assertEqual(response.headers.get('X-Eve-Stale-Asset-Version'), '1')

    def test_the_current_version_still_matches_what_the_templates_emit(self):
        from flask import url_for
        self._write(b'.probe{width:24px}')
        with app.test_request_context('/'):
            url = url_for('static', filename='probe.css')
        version = url.split('v=', 1)[1]
        response = self.client.get(url)
        self.assertIn('immutable', response.headers.get('Cache-Control') or '')
        self.assertIsNone(response.headers.get('X-Eve-Stale-Asset-Version'))
        _ = version


if __name__ == '__main__':
    unittest.main()
