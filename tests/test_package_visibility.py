import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import Admin, ClientOwnership, Package, Server, app, db  # noqa: E402
from panel.services.billing import _build_sub_page_packages, package_allowed_in_panel  # noqa: E402
from telegram_bot_worker import _available_packages, _purchase_packages  # noqa: E402


class PackageVisibilityTests(unittest.TestCase):
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
        try:
            os.unlink(_DB_FILE.name)
        except OSError:
            pass

    def setUp(self):
        Package.query.delete()
        Admin.query.filter(Admin.username.like('vis-test-%')).delete(
            synchronize_session=False)
        admin = Admin(username='vis-test-admin', role='superadmin',
                      is_superadmin=True, enabled=True)
        admin.set_password('StrongVisibilityPassword123!')
        db.session.add(admin)
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session_data:
            session_data['admin_id'] = admin.id
            session_data['admin_username'] = admin.username
            session_data['role'] = admin.role
            session_data['is_superadmin'] = True

    def tearDown(self):
        db.session.rollback()
        db.session.remove()

    def _package(self, name, **kwargs):
        kwargs.setdefault('price', 100_000)
        kwargs.setdefault('enabled', True)
        package = Package(name=name, days=30, volume=10, **kwargs)
        db.session.add(package)
        db.session.flush()
        return package

    # ── model defaults ───────────────────────────────────────────────────

    def test_model_defaults_true(self):
        package = self._package('defaults')
        db.session.commit()
        self.assertTrue(package.show_on_create)
        self.assertTrue(package.show_on_renew)
        data = package.to_dict()
        self.assertTrue(data['show_on_create'])
        self.assertTrue(data['show_on_renew'])

    # ── GET /api/packages?purpose=... ────────────────────────────────────

    def test_purpose_create_excludes_hidden_create(self):
        self._package('normal')
        self._package('renew-only', show_on_create=False)
        db.session.commit()
        resp = self.client.get('/api/packages?purpose=create')
        self.assertEqual(resp.status_code, 200)
        names = {p['name'] for p in resp.get_json()}
        self.assertIn('normal', names)
        self.assertNotIn('renew-only', names)

    def test_purpose_renew_excludes_hidden_renew(self):
        self._package('normal')
        self._package('create-only', show_on_renew=False)
        db.session.commit()
        resp = self.client.get('/api/packages?purpose=renew')
        self.assertEqual(resp.status_code, 200)
        names = {p['name'] for p in resp.get_json()}
        self.assertIn('normal', names)
        self.assertNotIn('create-only', names)

    def test_no_purpose_returns_all(self):
        self._package('normal')
        self._package('renew-only', show_on_create=False)
        self._package('create-only', show_on_renew=False)
        db.session.commit()
        resp = self.client.get('/api/packages')
        self.assertEqual(resp.status_code, 200)
        names = {p['name'] for p in resp.get_json()}
        self.assertEqual({'normal', 'renew-only', 'create-only'}, names)

    # ── create / update round-trip ───────────────────────────────────────

    def test_create_and_update_roundtrip(self):
        resp = self.client.post('/admin/packages', json={
            'name': 'trial-style', 'days': 1, 'volume': 1, 'price': 0,
            'show_on_create': True, 'show_on_renew': False,
        })
        self.assertEqual(resp.status_code, 200, resp.get_json())
        pkg_id = resp.get_json()['id']
        package = db.session.get(Package, pkg_id)
        self.assertTrue(package.show_on_create)
        self.assertFalse(package.show_on_renew)

        resp = self.client.put(f'/admin/packages/{pkg_id}', json={
            'show_on_create': False, 'show_on_renew': True,
        })
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        package = db.session.get(Package, pkg_id)
        self.assertFalse(package.show_on_create)
        self.assertTrue(package.show_on_renew)

    def test_create_defaults_to_true_when_omitted(self):
        resp = self.client.post('/admin/packages', json={
            'name': 'plain', 'days': 30, 'volume': 10, 'price': 50_000,
        })
        self.assertEqual(resp.status_code, 200, resp.get_json())
        package = db.session.get(Package, resp.get_json()['id'])
        self.assertTrue(package.show_on_create)
        self.assertTrue(package.show_on_renew)

    def test_staff_personal_package_shows_only_for_system_accounts_and_allowed_actions(self):
        superadmin = Admin.query.filter_by(username='vis-test-admin').one()
        staff = Admin(username='vis-test-staff', role='admin', enabled=True)
        reseller = Admin(username='vis-test-shop', role='reseller', enabled=True)
        server = Server(name='Test', host='https://test.invalid', username='test', password='test')
        for user in (staff, reseller):
            user.set_password('StrongVisibilityPassword123!')
        db.session.add_all([staff, reseller, server])
        db.session.flush()
        ownership = ClientOwnership(reseller_id=reseller.id, server_id=server.id, inbound_id=1,
                                    client_email='shop-client', client_uuid='shop-uuid')
        db.session.add(ownership)
        system = self._package('system-package', scope='personal', created_by=superadmin.id,
                               panel_roles={'create': ['superadmin'], 'renew': ['admin', 'superadmin']})
        private = self._package('shop-package', scope='personal', created_by=reseller.id,
                                panel_roles={'renew': ['admin']})
        db.session.commit()

        def names(key, **params):
            response = self.client.get('/api/packages', query_string={'purpose': key, **params})
            return {package['name'] for package in response.get_json()}

        self.assertEqual(names('renew', server_id=server.id, inbound_id=1, email='system-client'),
                         {'system-package'})
        self.assertEqual(names('renew', server_id=server.id, inbound_id=1, email='shop-client'), set())
        self.assertEqual(names('create'), {'system-package'})
        self.assertFalse(package_allowed_in_panel(system, reseller, None, 'renew'))
        self.assertTrue(package_allowed_in_panel(private, staff, reseller.id, 'renew'))
        self.assertFalse(package_allowed_in_panel(private, staff, None, 'renew'))
        with self.client.session_transaction() as session_data:
            session_data['admin_id'] = staff.id
            session_data['role'] = staff.role
        self.assertEqual(names('renew', server_id=server.id, inbound_id=1, email='system-client'),
                         {'system-package'})
        self.assertEqual(names('create'), set())
        db.session.delete(ownership)
        db.session.delete(server)
        db.session.commit()

    # ── telegram bot listing logic ───────────────────────────────────────

    def test_purchase_packages_skips_hidden_create(self):
        self._package('normal')
        self._package('renew-only', show_on_create=False)
        db.session.commit()
        names = {p.name for p in _purchase_packages(None)}
        self.assertIn('normal', names)
        self.assertNotIn('renew-only', names)

    def test_available_packages_skips_hidden_renew(self):
        self._package('normal')
        self._package('create-only', show_on_renew=False)
        db.session.commit()
        ownership = SimpleNamespace(reseller_id=None)
        names = {p.name for p in _available_packages(ownership)}
        self.assertIn('normal', names)
        self.assertNotIn('create-only', names)

    # ── customer subscription page packages ─────────────────────────────

    def test_sub_page_publication_is_independent_of_panel_renewal(self):
        self._package('normal', scope='global', show_on_sub=True)
        self._package('create-only', scope='global', show_on_sub=True,
                      show_on_renew=False)
        db.session.commit()
        names = {p['name'] for p in _build_sub_page_packages(None)}
        self.assertIn('normal', names)
        self.assertIn('create-only', names)

    def test_reseller_without_override_inherits_published_allowed_packages(self):
        reseller = Admin(
            username='vis-test-reseller-inherit', role='reseller',
            sub_shown_package_ids='[]',
        )
        reseller.set_password('StrongVisibilityPassword123!')
        db.session.add(reseller)
        db.session.flush()

        self._package('published-global', scope='global', show_on_sub=True)
        self._package('unpublished-global', scope='global', show_on_sub=False)
        self._package(
            'published-assigned', scope='assigned', show_on_sub=True,
            assigned_reseller_ids=f'[{reseller.id}]',
        )
        self._package(
            'foreign-assigned', scope='assigned', show_on_sub=True,
            assigned_reseller_ids='[]',
        )
        self._package(
            'create-only', scope='global', show_on_sub=True,
            show_on_renew=False,
        )
        db.session.commit()

        names = {p['name'] for p in _build_sub_page_packages(reseller)}
        self.assertEqual({'published-global', 'published-assigned', 'create-only'}, names)

    def test_reseller_override_replaces_inherited_package_selection(self):
        reseller = Admin(
            username='vis-test-reseller-override', role='reseller',
            sub_shown_package_ids='[]',
        )
        reseller.set_password('StrongVisibilityPassword123!')
        db.session.add(reseller)
        db.session.flush()

        selected = self._package(
            'selected-unpublished', scope='global', show_on_sub=False,
        )
        self._package('published-not-selected', scope='global', show_on_sub=True)
        db.session.flush()
        reseller.sub_shown_package_ids = f'[{selected.id}]'
        db.session.commit()

        names = {p['name'] for p in _build_sub_page_packages(reseller)}
        self.assertEqual({'selected-unpublished'}, names)

    def test_sub_page_package_query_failure_is_logged_and_propagated(self):
        failing_query = SimpleNamespace(
            filter_by=mock.Mock(side_effect=RuntimeError('package query failed')),
        )
        with mock.patch.object(Package, 'query', failing_query):
            with self.assertLogs('panel.services.billing', level='ERROR'):
                with self.assertRaisesRegex(RuntimeError, 'package query failed'):
                    _build_sub_page_packages(None)


if __name__ == '__main__':
    unittest.main()
