import os
import tempfile
import unittest
import zipfile
from unittest import mock


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
import panel.routes.clients as clients_module  # noqa: E402
import panel.services.backup as backup_service  # noqa: E402
from app import Admin, GLOBAL_SERVER_DATA, Server, app, db  # noqa: E402


class SecurityHardeningTests(unittest.TestCase):
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
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.reseller = Admin(
            username='security-reseller', password_hash='x', role='reseller',
            is_superadmin=False, enabled=True,
        )
        self.superadmin = Admin(
            username='security-root', password_hash='x', role='superadmin',
            is_superadmin=True, enabled=True,
        )
        self.server = Server(
            name='security-panel', host='https://panel.invalid', username='u',
            password='p', panel_type='auto',
        )
        db.session.add_all([self.reseller, self.superadmin, self.server])
        db.session.commit()
        self.client = app.test_client()

    def _login(self, admin):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = admin.id
            sess['role'] = admin.role
            sess['is_superadmin'] = bool(admin.is_superadmin)

    def test_reseller_cannot_access_backup_or_ssl_secrets(self):
        self._login(self.reseller)
        for method, path in (
            ('get', '/api/backups'),
            ('post', '/api/backups/upload'),
            ('get', '/api/settings/backup'),
            ('get', '/api/settings/ssl/export'),
            ('post', '/api/settings/ssl/upload'),
            ('post', '/api/settings/ssl/apply'),
        ):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 403, path)

    def test_restore_stream_is_not_a_destructive_get(self):
        self._login(self.superadmin)
        response = self.client.get('/api/backups/example.db/restore/stream')
        self.assertEqual(response.status_code, 405)

    def test_disabled_admin_session_is_revoked(self):
        self._login(self.reseller)
        self.reseller.enabled = False
        db.session.commit()
        response = self.client.get('/api/clients/search?email=test')
        self.assertEqual(response.status_code, 401)
        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_id', sess)

    def test_traffic_check_iterates_clients_inside_inbounds(self):
        self._login(self.superadmin)
        original = dict(GLOBAL_SERVER_DATA)
        try:
            GLOBAL_SERVER_DATA.update({
                'inbounds': [{
                    'server_id': self.server.id,
                    'id': 10,
                    'clients': [{
                        'email': 'nested-client',
                        'enable': True,
                        'remaining_bytes': 2048,
                        'expiryTimestamp': 0,
                        'expiryTime': 'Unlimited',
                        'totalGB_formatted': '4.00 KB',
                        'up': 512,
                        'down': 256,
                    }],
                }],
                'servers_status': [],
                'stats': {},
            })
            with mock.patch.object(
                app_module, 'get_accessible_servers', return_value=[self.server],
            ):
                response = self.client.get('/api/traffic_check')
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['client_count'], 1)
            self.assertEqual(payload['clients'][0]['email'], 'nested-client')
        finally:
            GLOBAL_SERVER_DATA.clear()
            GLOBAL_SERVER_DATA.update(original)

    def test_reseller_cannot_probe_unassigned_server(self):
        self.reseller.allowed_servers = '[]'
        db.session.commit()
        self._login(self.reseller)
        for method, path in (
            ('post', f'/api/servers/{self.server.id}/test'),
            ('get', f'/api/servers/{self.server.id}/panel-info'),
            ('get', f'/api/server/{self.server.id}/refresh?mode=cache&enqueue=false'),
        ):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 403, path)

    def test_regular_admin_refresh_access_matches_dashboard_access(self):
        regular = Admin(
            username='security-admin', password_hash='x', role='admin',
            is_superadmin=False, enabled=True, allowed_servers='[]',
        )
        db.session.add(regular)
        db.session.commit()
        self._login(regular)
        response = self.client.get(
            f'/api/server/{self.server.id}/refresh?mode=cache&enqueue=false',
        )
        self.assertEqual(response.status_code, 200)

    def test_migration_zip_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = os.path.join(tmp, 'migration.zip')
            upload_root = os.path.join(tmp, 'uploads')
            escaped = os.path.join(tmp, 'escaped.txt')
            with zipfile.ZipFile(bundle, 'w') as archive:
                archive.writestr('database/servers.db', b'database')
                archive.writestr('static_uploads/../escaped.txt', b'owned')

            with (
                mock.patch.object(backup_service, '_is_sqlite_db', return_value=True),
                mock.patch.object(backup_service, '_migration_file_dirs', return_value={
                    'static_uploads': upload_root,
                }),
                mock.patch.object(backup_service.shutil, 'copy2'),
            ):
                with self.assertRaisesRegex(RuntimeError, 'Unsafe archive path'):
                    backup_service._restore_full_migration_zip(bundle)
            self.assertFalse(os.path.exists(escaped))

    def test_renew_unlock_uses_owner_token(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}

            def set(self, key, value, nx=False, ex=None):
                if nx and key in self.values:
                    return False
                self.values[key] = value
                return True

            def eval(self, script, _count, key, *args):
                token = args[0]
                if self.values.get(key) != token:
                    return 0
                if "redis.call('del'" in script:
                    del self.values[key]
                    return 1
                return 1

        redis = FakeRedis()
        with mock.patch.object(app_module, 'get_redis', return_value=redis):
            token_a = clients_module._acquire_renew_lock('renew:test', ttl=45)
            self.assertTrue(token_a)
            redis.values['renew:test'] = 'owner-b'
            clients_module._release_renew_lock('renew:test', token_a)
        self.assertEqual(redis.values['renew:test'], 'owner-b')


if __name__ == '__main__':
    unittest.main()
