"""Phase 5 tests: versioned, domain-separated secret encryption and rotation."""
import base64
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from cryptography.fernet import Fernet  # noqa: E402

from app import Server, app, db, decrypt_server_password, encrypt_server_password  # noqa: E402
from panel.security import keyring  # noqa: E402
from panel.security import decrypt_secret, encrypt_secret, rotate_secret  # noqa: E402
from panel.services import secret_rotation  # noqa: E402

MASTER = base64.urlsafe_b64encode(os.urandom(32)).decode('ascii')
V2_FINANCE = Fernet.generate_key().decode('ascii')


def _clear_caches():
    keyring._key_b64.cache_clear()


class KeyringTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            'SERVER_PASSWORD_KEY': MASTER,
            'EVE_KEY_FINANCE_V2': '',
        }, clear=False)
        self._env.start()
        os.environ.pop('EVE_KEY_FINANCE_V2', None)
        _clear_caches()

    def tearDown(self):
        self._env.stop()
        _clear_caches()

    def test_domains_derive_independent_keys(self):
        self.assertEqual(keyring.current_version('finance'), 2)
        finance = keyring._key_b64('finance', 2)
        subscriptions = keyring._key_b64('subscriptions', 2)
        legacy = keyring._key_b64('finance', 1)
        self.assertNotEqual(finance, subscriptions)
        self.assertNotEqual(finance, legacy)
        self.assertEqual(legacy, MASTER)  # v1 is the historical key

    def test_envelope_carries_the_current_version(self):
        protected = encrypt_secret('4111-1111-1111-1111', 'finance')
        self.assertTrue(protected.startswith('enc:v2:'))
        self.assertEqual(decrypt_secret(protected, 'finance'), '4111-1111-1111-1111')

    def test_ciphertext_is_not_decryptable_in_another_domain(self):
        protected = encrypt_secret('card-number', 'finance')
        with self.assertRaises(RuntimeError):
            decrypt_secret(protected, 'subscriptions')

    def test_legacy_v1_and_unversioned_values_still_decrypt(self):
        v1_cipher = keyring.fernet('finance', 1)
        versioned = 'enc:v1:' + v1_cipher.encrypt(b'legacy-secret').decode()
        unversioned = 'enc:' + v1_cipher.encrypt(b'older-secret').decode()
        self.assertEqual(decrypt_secret(versioned, 'finance'), 'legacy-secret')
        self.assertEqual(decrypt_secret(unversioned, 'finance'), 'older-secret')

    def test_rotation_reencrypts_to_the_current_version(self):
        legacy = 'enc:v1:' + keyring.fernet('mfa', 1).encrypt(b'totp-seed').decode()
        rotated, changed = rotate_secret(legacy, 'mfa')
        self.assertTrue(changed)
        self.assertTrue(rotated.startswith('enc:v2:'))
        self.assertEqual(decrypt_secret(rotated, 'mfa'), 'totp-seed')

    def test_explicit_domain_key_overrides_derivation(self):
        with mock.patch.dict(os.environ, {'EVE_KEY_FINANCE_V2': V2_FINANCE}):
            _clear_caches()
            self.assertEqual(keyring._key_b64('finance', 2), V2_FINANCE)
            protected = encrypt_secret('explicit-key', 'finance')
            self.assertEqual(decrypt_secret(protected, 'finance'), 'explicit-key')
        _clear_caches()

    def test_undecryptable_value_does_not_leak_the_token(self):
        token = 'super-secret-ciphertext-value'
        with self.assertRaises(RuntimeError) as caught:
            decrypt_secret(f'enc:v2:{token}', 'finance')
        self.assertNotIn(token, str(caught.exception))

    def test_redaction_never_returns_the_value(self):
        self.assertEqual(keyring.redact('secret'), '***')
        self.assertEqual(keyring.redact(''), '')

    def test_development_without_master_key_passes_through(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SERVER_PASSWORD_KEY', None)
            os.environ.pop('EVE_MASTER_KEY', None)
            _clear_caches()
            self.assertEqual(keyring.current_version('finance'), 1)
            self.assertEqual(encrypt_secret('plain', 'finance'), 'plain')
        _clear_caches()


class BackupKeyVersionTests(unittest.TestCase):
    def test_v2_magic_and_round_trip(self):
        from panel.security import backup_crypto
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, 'source.bin')
            encrypted = os.path.join(tmp, 'enc.eveenc')
            restored = os.path.join(tmp, 'restored.bin')
            with open(source, 'wb') as handle:
                handle.write(b'backup-payload')
            legacy_key = Fernet.generate_key().decode('ascii')
            v2_key = Fernet.generate_key().decode('ascii')
            with mock.patch.dict(os.environ, {
                    'EVE_BACKUP_KEY': legacy_key, 'EVE_KEY_BACKUP_V2': v2_key}):
                backup_crypto.encrypt_backup_file(source, encrypted)
                with open(encrypted, 'rb') as handle:
                    self.assertTrue(handle.read(len(backup_crypto.MAGIC_V2)) == backup_crypto.MAGIC_V2)
                backup_crypto.decrypt_backup_file(encrypted, restored)
                with open(restored, 'rb') as handle:
                    self.assertEqual(handle.read(), b'backup-payload')
            # A v1 archive stays readable once v2 is configured.
            v1_encrypted = os.path.join(tmp, 'v1.eveenc')
            v1_restored = os.path.join(tmp, 'v1.restored')
            with mock.patch.dict(os.environ, {'EVE_BACKUP_KEY': legacy_key}):
                os.environ.pop('EVE_KEY_BACKUP_V2', None)
                backup_crypto.encrypt_backup_file(source, v1_encrypted)
            # The v1 key must stay configured while a v1 archive is still read.
            with mock.patch.dict(os.environ, {
                    'EVE_BACKUP_KEY': legacy_key, 'EVE_KEY_BACKUP_V2': v2_key}):
                backup_crypto.decrypt_backup_file(v1_encrypted, v1_restored)
                with open(v1_restored, 'rb') as handle:
                    self.assertEqual(handle.read(), b'backup-payload')


class RotationServiceTests(unittest.TestCase):
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

    def test_rotation_moves_legacy_values_to_the_current_version(self):
        from panel.models import SystemMigration
        with mock.patch.dict(os.environ, {'SERVER_PASSWORD_KEY': MASTER}):
            _clear_caches()
            Server.query.delete()
            SystemMigration.query.filter_by(
                migration_id=secret_rotation.ROTATION_MIGRATION_ID).delete()
            db.session.commit()
            legacy = 'enc:v1:' + keyring.fernet('xui_credentials', 1).encrypt(b'panel-pw').decode()
            server = Server(name='rotate-me', host='https://panel.invalid',
                            username='u', password=legacy, panel_type='auto')
            db.session.add(server)
            db.session.commit()
            self.assertTrue(secret_rotation.rotation_needed())
            result = secret_rotation.run_rotation(batch_size=50)
            self.assertEqual(result['status'], 'complete')
            db.session.expire_all()
            refreshed = Server.query.filter_by(name='rotate-me').one()
            self.assertTrue(refreshed.password.startswith('enc:v2:'))
            self.assertEqual(decrypt_server_password(refreshed.password), 'panel-pw')
            Server.query.delete()
            SystemMigration.query.filter_by(
                migration_id=secret_rotation.ROTATION_MIGRATION_ID).delete()
            db.session.commit()
        _clear_caches()


if __name__ == '__main__':
    unittest.main()
