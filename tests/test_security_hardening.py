import os
import base64
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
from panel.services.client_operations import (  # noqa: E402
    begin_client_operation, complete_client_operation, fail_client_operation,
    mark_client_operation_applied, resolve_client_operation,
)
from app import Admin, ClientOperation, GLOBAL_SERVER_DATA, Server, app, db  # noqa: E402
from panel.core.redis_client import _decode_snapshot, _encode_snapshot  # noqa: E402
from panel.security.backup_crypto import (  # noqa: E402
    MAGIC as BACKUP_MAGIC,
    decrypt_backup_file,
    encrypt_backup_file,
)
from panel.security.secrets import (  # noqa: E402
    _fernet, decrypt_secret, encrypt_secret, protect_system_setting,
)
from panel.security.tls import outbound_tls_verify  # noqa: E402


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
        ClientOperation.query.delete()
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

    def test_superadmin_cannot_export_tls_private_key(self):
        self._login(self.superadmin)
        response = self.client.get('/api/settings/ssl/export')
        self.assertEqual(response.status_code, 410)
        self.assertNotIn(b'privkey', response.data.lower())

    def test_secret_envelope_and_backup_file_round_trip(self):
        old_server_key = os.environ.get('SERVER_PASSWORD_KEY')
        old_backup_key = os.environ.get('EVE_BACKUP_KEY')
        key = base64.urlsafe_b64encode(os.urandom(32)).decode('ascii')
        os.environ['SERVER_PASSWORD_KEY'] = key
        os.environ['EVE_BACKUP_KEY'] = key
        _fernet.cache_clear()
        source = tempfile.NamedTemporaryFile(delete=False)
        encrypted = f'{source.name}.eveenc'
        restored = f'{source.name}.restored'
        try:
            source.write(b'sensitive-backup-content')
            source.close()
            protected = encrypt_secret('top-secret')
            self.assertTrue(protected.startswith('enc:v1:'))
            self.assertEqual(decrypt_secret(protected), 'top-secret')
            self.assertTrue(protect_system_setting('telegram_backup_bot_token', 'token').startswith('enc:v1:'))
            encrypt_backup_file(source.name, encrypted)
            with open(encrypted, 'rb') as handle:
                self.assertNotIn(b'sensitive-backup-content', handle.read())
            decrypt_backup_file(encrypted, restored)
            with open(restored, 'rb') as handle:
                self.assertEqual(handle.read(), b'sensitive-backup-content')
        finally:
            for path in (source.name, encrypted, restored):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            if old_server_key is None:
                os.environ.pop('SERVER_PASSWORD_KEY', None)
            else:
                os.environ['SERVER_PASSWORD_KEY'] = old_server_key
            if old_backup_key is None:
                os.environ.pop('EVE_BACKUP_KEY', None)
            else:
                os.environ['EVE_BACKUP_KEY'] = old_backup_key
            _fernet.cache_clear()

    def test_tampered_backup_never_reaches_destination(self):
        old_backup_key = os.environ.get('EVE_BACKUP_KEY')
        os.environ['EVE_BACKUP_KEY'] = base64.urlsafe_b64encode(os.urandom(32)).decode('ascii')
        source = tempfile.NamedTemporaryFile(delete=False)
        source.write(b'authentic-backup-content')
        source.close()
        encrypted = f'{source.name}.eveenc'
        restored = f'{source.name}.restored'
        try:
            encrypt_backup_file(source.name, encrypted)
            with open(encrypted, 'r+b') as handle:
                handle.seek(len(BACKUP_MAGIC) + 12)  # flip one ciphertext byte
                byte = handle.read(1)
                handle.seek(-1, os.SEEK_CUR)
                handle.write(bytes([byte[0] ^ 0x01]))
            with self.assertRaises(Exception):
                decrypt_backup_file(encrypted, restored)
            # Authentication failed, so no plaintext (partial or full) is published.
            self.assertFalse(os.path.exists(restored))
            leftovers = [name for name in os.listdir(os.path.dirname(restored) or '.')
                         if name.startswith(f'.{os.path.basename(restored)}.part-')]
            self.assertEqual(leftovers, [])
        finally:
            for path in (source.name, encrypted, restored):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            if old_backup_key is None:
                os.environ.pop('EVE_BACKUP_KEY', None)
            else:
                os.environ['EVE_BACKUP_KEY'] = old_backup_key

    def test_redis_snapshot_uses_safe_json_serialization(self):
        payload = {'items': [{'id': 1, 'name': 'ایمن'}], 'ok': True}
        encoded = _encode_snapshot(payload)
        self.assertEqual(_decode_snapshot(encoded), payload)

    def test_tls_policy_uses_verification_or_custom_ca(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('EVE_XUI_CA_BUNDLE', None)
            os.environ.pop('EVE_OUTBOUND_CA_BUNDLE', None)
            self.assertIs(outbound_tls_verify('EVE_XUI_CA_BUNDLE'), True)
        ca_file = tempfile.NamedTemporaryFile(delete=False)
        ca_file.close()
        try:
            with mock.patch.dict(os.environ, {'EVE_XUI_CA_BUNDLE': ca_file.name}):
                self.assertEqual(outbound_tls_verify('EVE_XUI_CA_BUNDLE'), ca_file.name)
        finally:
            os.remove(ca_file.name)

    def test_restore_stream_is_not_a_destructive_get(self):
        self._login(self.superadmin)
        response = self.client.get('/api/backups/example.db/restore/stream')
        self.assertEqual(response.status_code, 405)

    def test_raw_xui_backup_requires_superadmin(self):
        regular = Admin(
            username='security-admin', password_hash='x', role='admin',
            is_superadmin=False, enabled=True,
        )
        db.session.add(regular)
        db.session.commit()
        self._login(regular)
        response = self.client.get(f'/api/servers/{self.server.id}/xui-backup')
        self.assertEqual(response.status_code, 403)

    def test_session_authority_is_refreshed_from_admin_record(self):
        self._login(self.reseller)
        with self.client.session_transaction() as sess:
            sess['role'] = 'superadmin'
            sess['is_superadmin'] = True
        response = self.client.get('/api/backups')
        self.assertEqual(response.status_code, 403)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['role'], 'reseller')
            self.assertFalse(sess['is_superadmin'])

    def test_credit_reservation_is_atomic_and_idempotent(self):
        self.reseller.credit = 100
        db.session.commit()
        first, disposition, data = begin_client_operation(
            idempotency_key='renew-atomic-1', action='renew', admin=self.reseller,
            server_id=self.server.id, inbound_id=1, client_email='alice',
            amount=80, payload={'days': 30},
        )
        self.assertEqual(disposition, 'new')
        self.assertEqual(data['remaining_credit'], 20)

        _second, disposition, _data = begin_client_operation(
            idempotency_key='renew-atomic-2', action='renew', admin=self.reseller,
            server_id=self.server.id, inbound_id=1, client_email='bob',
            amount=80, payload={'days': 30},
        )
        self.assertEqual(disposition, 'insufficient_credit')

        complete_client_operation(first, {'success': True, 'marker': 'once'})
        replay, disposition, payload = begin_client_operation(
            idempotency_key='renew-atomic-1', action='renew', admin=self.reseller,
            server_id=self.server.id, inbound_id=1, client_email='alice',
            amount=80, payload={'days': 30},
        )
        self.assertEqual(replay.id, first.id)
        self.assertEqual(disposition, 'replay')
        self.assertEqual(payload['marker'], 'once')

        # A definitive pre-panel failure refunds exactly once.
        third, disposition, _data = begin_client_operation(
            idempotency_key='renew-atomic-3', action='renew', admin=self.reseller,
            server_id=self.server.id, inbound_id=1, client_email='carol',
            amount=10, payload={'days': 1},
        )
        self.assertEqual(disposition, 'new')
        fail_client_operation(third, 'panel rejected request')
        fail_client_operation(third, 'duplicate cleanup')
        db.session.refresh(self.reseller)
        self.assertEqual(self.reseller.credit, 20)

        ambiguous, disposition, _data = begin_client_operation(
            idempotency_key='renew-atomic-4', action='renew', admin=self.reseller,
            server_id=self.server.id, inbound_id=1, client_email='dave',
            amount=10, payload={'days': 1},
        )
        self.assertEqual(disposition, 'new')
        mark_client_operation_applied(ambiguous, {'expiryTime': 123})
        fail_client_operation(ambiguous, 'worker crashed after panel write', uncertain=True)
        resolved, error = resolve_client_operation(
            ambiguous.id, 'refund', self.superadmin.id,
        )
        self.assertIsNone(error)
        self.assertEqual(resolved.state, 'failed')
        db.session.refresh(self.reseller)
        self.assertEqual(self.reseller.credit, 20)

    def test_receipt_credit_claim_is_idempotent(self):
        from app import ManualReceipt, apply_receipt_credit
        self.reseller.credit = 0
        db.session.commit()
        receipt = ManualReceipt(admin_id=self.reseller.id, amount=5_000, status='pending')
        db.session.add(receipt)
        db.session.commit()

        ok, error = apply_receipt_credit(receipt)
        self.assertTrue(ok, error)
        db.session.commit()
        self.assertEqual(db.session.get(Admin, self.reseller.id).credit, 5_000)

        # A repeated approval (double click, overlapping auto scan) must not credit twice.
        ok_again, error_again = apply_receipt_credit(receipt)
        self.assertFalse(ok_again)
        self.assertIn('already', error_again)
        db.session.commit()
        self.assertEqual(db.session.get(Admin, self.reseller.id).credit, 5_000)

    def test_repeated_rejection_reverses_credit_once(self):
        from app import ManualReceipt
        self._login(self.superadmin)
        self.reseller.credit = 10_000
        receipt = ManualReceipt(admin_id=self.reseller.id, amount=4_000, status='approved')
        db.session.add(receipt)
        db.session.commit()

        first = self.client.post(f'/api/receipts/{receipt.id}/reject', json={'reason': 'dup'})
        self.assertEqual(first.status_code, 200, first.data)
        second = self.client.post(f'/api/receipts/{receipt.id}/reject', json={'reason': 'dup'})
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(db.session.get(Admin, self.reseller.id).credit, 6_000)

    def test_qrcode_endpoint_requires_a_login(self):
        # Only the authenticated dashboard uses this; it must not stay open.
        response = self.client.get('/api/client/qrcode?link=https://example.com')
        self.assertEqual(response.status_code, 401)

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
