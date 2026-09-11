"""Phase 4 tests: permission catalog, role defaults, overrides and enforcement."""
import os
import tempfile
import unittest

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import Admin, AdminPermission, AdminSession, app, db  # noqa: E402
from panel.services import permissions as perms  # noqa: E402


class PermissionPolicyTests(unittest.TestCase):
    def _admin(self, role, superadmin=False):
        return Admin(role=role, is_superadmin=superadmin, enabled=True)

    def test_superadmin_has_every_permission_and_no_lockout(self):
        admin = self._admin('superadmin', superadmin=True)
        self.assertEqual(perms.permissions_for(admin), perms.PERMISSIONS)
        self.assertTrue(perms.has_permission(admin, 'secrets.manage'))
        self.assertTrue(perms.has_permission(admin, 'admins.manage'))

    def test_admin_defaults_match_the_historical_user_management_surface(self):
        admin = self._admin('admin')
        granted = perms.permissions_for(admin)
        for permission in ('servers.read', 'servers.write', 'clients.write',
                           'finance.write', 'settings.write', 'finance.manage',
                           'admins.write', 'bank.reveal'):
            self.assertIn(permission, granted, permission)
        for permission in ('secrets.manage', 'backups.run', 'backups.restore', 'admins.manage'):
            self.assertNotIn(permission, granted, permission)

    def test_reseller_defaults_are_least_privilege(self):
        admin = self._admin('reseller')
        granted = perms.permissions_for(admin)
        self.assertIn('clients.write', granted)
        self.assertIn('finance.read', granted)
        for permission in ('servers.write', 'settings.read', 'settings.write',
                           'backups.run', 'backups.restore', 'admins.read',
                           'admins.write', 'admins.manage', 'secrets.manage',
                           'bank.reveal'):
            self.assertNotIn(permission, granted, permission)

    def test_unknown_role_falls_back_to_reseller(self):
        admin = self._admin('auditor')
        self.assertEqual(perms.normalize_role(admin), 'reseller')

    def test_disabled_admin_has_no_permissions(self):
        admin = Admin(role='superadmin', is_superadmin=True, enabled=False)
        self.assertFalse(perms.has_permission(admin, 'servers.read'))

    def test_unknown_permission_is_never_granted(self):
        admin = self._admin('superadmin', superadmin=True)
        self.assertFalse(perms.has_permission(admin, 'root.everything'))


class PermissionEnforcementTests(unittest.TestCase):
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
        AdminPermission.query.delete()
        AdminSession.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.superadmin = Admin(
            username='rbac-root', role='superadmin', is_superadmin=True, enabled=True,
        )
        self.superadmin.set_password('CorrectHorseBattery1!')
        self.admin = Admin(username='rbac-admin', role='admin', is_superadmin=False, enabled=True)
        self.admin.set_password('CorrectHorseBattery1!')
        self.reseller = Admin(username='rbac-reseller', role='reseller', is_superadmin=False,
                              enabled=True, allowed_servers='[]')
        self.reseller.set_password('CorrectHorseBattery1!')
        db.session.add_all([self.superadmin, self.admin, self.reseller])
        db.session.commit()
        self.client = app.test_client()

    def _login(self, admin):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = admin.id
            sess['role'] = admin.role
            sess['is_superadmin'] = bool(admin.is_superadmin)

    def test_me_permissions_reflects_the_role(self):
        self._login(self.reseller)
        payload = self.client.get('/api/me/permissions').get_json()
        self.assertEqual(payload['role'], 'reseller')
        self.assertIn('clients.write', payload['permissions'])
        self.assertNotIn('settings.write', payload['permissions'])

    def test_admin_management_is_gated_by_permission(self):
        self._login(self.admin)
        self.assertEqual(self.client.get('/api/admins').status_code, 200)
        self._login(self.reseller)
        forbidden = self.client.get('/api/admins')
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.get_json()['code'], 'forbidden')

    def test_permission_override_delegates_backup_access(self):
        self._login(self.reseller)
        self.assertEqual(self.client.get('/api/backups').status_code, 403)
        perms.set_permission(self.reseller.id, 'backups.run', True)
        db.session.commit()
        self.assertEqual(self.client.get('/api/backups').status_code, 200)
        # A different permission is not implied by the grant.
        listing = self.client.get('/api/backups').get_json()
        self.assertIn('success', listing)
        self.assertFalse(perms.has_permission(self.reseller, 'backups.restore'))

    def test_deny_override_removes_a_role_default(self):
        self._login(self.admin)
        self.assertEqual(self.client.get('/api/admins').status_code, 200)
        perms.set_permission(self.admin.id, 'admins.read', False)
        db.session.commit()
        self.assertEqual(self.client.get('/api/admins').status_code, 403)

    def test_superadmin_can_read_and_write_overrides(self):
        self._login(self.superadmin)
        catalog = self.client.get(f'/api/admins/{self.admin.id}/permissions').get_json()
        self.assertIn('servers.write', catalog['catalog'])
        updated = self.client.put(
            f'/api/admins/{self.admin.id}/permissions',
            json={'permissions': {'secrets.manage': True}},
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertIn('secrets.manage', updated.get_json()['permissions'])
        cleared = self.client.put(
            f'/api/admins/{self.admin.id}/permissions',
            json={'permissions': {'secrets.manage': None}},
        )
        self.assertNotIn('secrets.manage', cleared.get_json()['permissions'])

    def test_unknown_permission_override_is_rejected(self):
        self._login(self.superadmin)
        response = self.client.put(
            f'/api/admins/{self.admin.id}/permissions',
            json={'permissions': {'root.everything': True}},
        )
        # The app rewrites API 400s to 200 with X-Eve-Status for the CDN.
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')
        self.assertFalse(response.get_json()['success'])
        self.assertIn('Unknown permission', response.get_json()['error'])

    def test_reseller_cannot_manage_permission_overrides(self):
        self._login(self.reseller)
        response = self.client.get(f'/api/admins/{self.admin.id}/permissions')
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
