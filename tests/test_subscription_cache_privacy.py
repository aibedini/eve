"""The tokenized subscription surfaces are private and uncacheable; assets are the opposite.

A CDN or proxy must never store an `/s/<server>/<token>` page, and that is also the reason an
intermittent layout problem cannot be explained by a stale HTML copy at an edge: only the static
stylesheets can arrive from a different build, which is why the build identity and the content
version exist (`docs/performance/STATIC_ASSETS.md`, `docs/operations/BUILD_IDENTITY.md`).
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

from app import Admin, Server, app, db  # noqa: E402


class PrivateSubscriptionCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        Admin.query.delete()
        Server.query.delete()
        db.session.commit()
        cls.admin = Admin(username='cache-admin', role='superadmin', is_superadmin=True,
                          enabled=True)
        cls.admin.set_password('CorrectHorseBattery1!')
        cls.server = Server(name='cache', host='https://cache.invalid', username='u',
                            password='p', sub_path='/sub/', panel_type='auto', enabled=True)
        db.session.add_all([cls.admin, cls.server])
        db.session.commit()
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def _seed_account(self):
        from app import GLOBAL_SERVER_DATA
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server.id, 'id': 1, 'remark': 'in',
            'clients': [{'server_id': self.server.id, 'inbound_id': 1, 'email': 'sub-user',
                         'id': 'sub-token-1', 'up': 0, 'down': 0,
                         'raw_client': {'email': 'sub-user', 'id': 'sub-token-1',
                                        'enable': True, 'totalGB': 0, 'expiryTime': 0}}],
            'client_count': 1, 'active_count': 1}]
        self.addCleanup(GLOBAL_SERVER_DATA.pop, 'inbounds', None)

    def test_a_tokenized_page_is_private_and_never_stored(self):
        self._seed_account()
        response = self.client.get('/s/%d/sub-token-1' % self.server.id,
                                   headers={'User-Agent': 'Mozilla/5.0'})
        cache_control = (response.headers.get('Cache-Control') or '').lower()
        self.assertIn('private', cache_control)
        self.assertIn('no-store', cache_control)
        self.assertNotIn('public', cache_control)
        self.assertNotIn('s-maxage', cache_control)
        self.assertEqual(response.headers.get('Pragma'), 'no-cache')

    def test_static_assets_stay_publicly_cacheable(self):
        # The opposite policy on purpose: assets must be cacheable, and the *matching*
        # content version is immutable.
        from app import _static_asset_version
        current = _static_asset_version('style.css')
        response = self.client.get('/static/style.css?v=' + current)
        cache_control = (response.headers.get('Cache-Control') or '').lower()
        self.assertNotIn('no-store', cache_control)
        self.assertNotIn('private', cache_control)
        self.assertIn('public', cache_control)
        # A stale key still must not be stored as no-store/private: it revalidates instead.
        stale = self.client.get('/static/style.css?v=stale-test')
        stale_control = (stale.headers.get('Cache-Control') or '').lower()
        self.assertNotIn('no-store', stale_control)
        self.assertNotIn('private', stale_control)


if __name__ == '__main__':
    unittest.main()
