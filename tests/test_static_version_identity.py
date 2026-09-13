"""Evidence for the intermittent Subscription layout bug: the static version is not an identity.

The subscription page depends on four static stylesheets (tailwind.generated.css, style.css,
fonts.css, phosphor-regular.css); when one of them arrives from a different build than the HTML
the layout collapses and bare SVGs render huge. This file pins down *why* that can happen with
the current asset pipeline, as executable evidence rather than a theory:

1. the cache key is an ``mtime+size`` fingerprint, so the same bytes can carry two different
   keys (every deploy, every node) and two different byte streams of the same size can share
   one key;
2. Flask's static route ignores the query string, so a *stale* ``?v=`` still returns whatever
   bytes are on disk - and the response is still marked ``immutable, max-age=31536000``, which
   lets a browser or CDN keep the new bytes under the old key.

The fix (content-hashed identity, and ``immutable`` only when the requested version matches the
file's current content) lands in the following commits; these tests will flip from documenting
the weakness to enforcing the guarantee.
"""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import STATIC_IMMUTABLE_SECONDS, _static_asset_version, app  # noqa: E402


class StaticVersionIdentityTests(unittest.TestCase):
    """The fingerprint's properties, against a scratch static folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._patch = mock.patch.object(app, '_static_folder', self.folder)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(_static_asset_version.cache_clear)

    def _write(self, name, payload):
        path = os.path.join(self.folder, name)
        with open(path, 'wb') as handle:
            handle.write(payload)
        return path

    def test_the_same_bytes_get_a_new_version_when_only_the_mtime_moves(self):
        path = self._write('same.css', b'body{color:red}')
        _static_asset_version.cache_clear()
        first = _static_asset_version('same.css')
        os.utime(path, (2_000_000_000, 2_000_000_000))
        _static_asset_version.cache_clear()
        second = _static_asset_version('same.css')
        self.assertNotEqual(first, second)
        # Same bytes, two cache keys: every node/deploy can produce its own key for identical
        # content, which is what makes a browser hold a stale stylesheet under a new-looking
        # name (or adopt new bytes under an old one).
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), b'body{color:red}')

    def test_two_different_stylesheets_of_the_same_size_share_one_version(self):
        payload_a = b'a{width:24px}'
        payload_b = b'b{width:99px}'
        self.assertEqual(len(payload_a), len(payload_b))
        path_a = self._write('a.css', payload_a)
        path_b = self._write('b.css', payload_b)
        stamp = 1_900_000_000
        os.utime(path_a, (stamp, stamp))
        os.utime(path_b, (stamp, stamp))
        _static_asset_version.cache_clear()
        # The fingerprint is mtime+size, so identical mtime and size yield identical versions
        # for *different* stylesheets: the key is not a content identity.
        self.assertEqual(_static_asset_version('a.css'), _static_asset_version('b.css'))


class StaleVersionQueryTests(unittest.TestCase):
    """The served bytes are not bound to the requested version (the skew window)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._patch = mock.patch.object(app, '_static_folder', self.folder)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(_static_asset_version.cache_clear)
        self.client = app.test_client()

    def _write(self, payload):
        path = os.path.join(self.folder, 'probe.css')
        with open(path, 'wb') as handle:
            handle.write(payload)
        _static_asset_version.cache_clear()
        return path

    def test_a_stale_version_returns_the_new_bytes_as_immutable(self):
        self._write(b'.probe{width:24px}')
        stale_version = _static_asset_version('probe.css')
        first = self.client.get('/static/probe.css?v=' + stale_version)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_data(), b'.probe{width:24px}')

        # The deploy replaces the stylesheet; the browser (or a CDN edge) still asks for the
        # version it has cached from the previous build.
        self._write(b'.probe{width:96px}')
        second = self.client.get('/static/probe.css?v=' + stale_version)
        self.assertEqual(second.status_code, 200)
        # The answer is the NEW stylesheet under the OLD cache key ...
        self.assertEqual(second.get_data(), b'.probe{width:96px}')
        # ... and it is marked immutable for a year, so the new bytes are now pinned to the old
        # key in every cache on the path. That is the mechanism behind "sometimes the page
        # renders with the old stylesheet and sometimes with the new one".
        self.assertEqual(
            second.headers.get('Cache-Control'),
            'public, max-age=%d, immutable' % STATIC_IMMUTABLE_SECONDS)

    def test_the_version_query_is_not_validated_against_the_file(self):
        self._write(b'.probe{width:24px}')
        response = self.client.get('/static/probe.css?v=whatever')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), b'.probe{width:24px}')
        # No 404/redirect and no revalidation: any value is accepted and cached immutably.
        self.assertIn('immutable', response.headers.get('Cache-Control') or '')


if __name__ == '__main__':
    unittest.main()
