"""Phase 0 backup policy tests: X-UI backups are transient and unencrypted.

Eve database backups stay AES-GCM encrypted. See docs/security/BACKUP_POLICY.md.
"""
import base64
import os
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402,F401
import panel.services.backup as backup_service  # noqa: E402
from panel.security.backup_crypto import MAGIC as BACKUP_MAGIC, decrypt_backup_file  # noqa: E402


def _ok_document(message_id=7, file_id='file-id'):
    return {'ok': True, 'result': {'message_id': message_id, 'document': {'file_id': file_id}}}


class BackupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = os.path.join(self.tmp.name, 'xui-spool')
        self._env = mock.patch.dict(os.environ, {'EVE_XUI_BACKUP_DIR': self.spool})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _server(self):
        return SimpleNamespace(id=11, name='panel-one', host='https://panel.invalid')

    def test_xui_backup_is_unencrypted_and_unlinked_after_send(self):
        payload = b'SQLite format 3\x00' + b'panel-db-bytes' * 16
        captured = {}

        def fake_send(token, chat_id, file_path, caption, proxies=None, document_name=None):
            captured['path'] = file_path
            captured['existed'] = os.path.exists(file_path)
            captured['data'] = open(file_path, 'rb').read()
            captured['document_name'] = document_name
            return mock.Mock(status_code=200)

        with mock.patch.object(backup_service, '_telegram_send_document', side_effect=fake_send), \
                mock.patch.object(backup_service, '_safe_response_json', return_value=(_ok_document(), None)):
            ok, err = backup_service._send_xui_backup_to_telegram(
                self._server(), payload, '.db', 'bot-token', 'chat', None, datetime.utcnow())
        self.assertTrue(ok, err)
        self.assertTrue(captured['existed'])
        self.assertEqual(captured['data'], payload)  # byte-for-byte, no encryption envelope
        self.assertFalse(captured['data'].startswith(BACKUP_MAGIC))
        self.assertNotIn(b'enc:v1:', captured['data'])
        self.assertFalse(os.path.exists(captured['path']))  # deleted in finally
        self.assertEqual(os.listdir(self.spool), [])
        self.assertTrue(captured['document_name'].startswith('panel-one_'))

    @unittest.skipUnless(os.name == 'posix', 'POSIX file modes only')
    def test_spool_directory_and_file_permissions(self):
        payload = b'x' * 32
        captured = {}

        def fake_send(token, chat_id, file_path, caption, proxies=None, document_name=None):
            captured['path'] = file_path
            captured['file_mode'] = os.stat(file_path).st_mode & 0o777
            return mock.Mock(status_code=200)

        with mock.patch.object(backup_service, '_telegram_send_document', side_effect=fake_send), \
                mock.patch.object(backup_service, '_safe_response_json', return_value=(_ok_document(), None)):
            ok, err = backup_service._send_xui_backup_to_telegram(
                self._server(), payload, '.db', 'bot-token', 'chat', None, datetime.utcnow())
        self.assertTrue(ok, err)
        self.assertEqual(os.stat(self.spool).st_mode & 0o777, 0o700)
        self.assertEqual(captured['file_mode'], 0o600)

    def test_failed_upload_still_deletes_the_local_copy(self):
        payload = b'x' * 32
        captured = {}

        def boom(token, chat_id, file_path, caption, proxies=None, document_name=None):
            captured['path'] = file_path
            raise ConnectionError('Max retries exceeded /bot123456:SECRET-TOKEN/sendDocument')

        with mock.patch.object(backup_service, '_telegram_send_document', side_effect=boom):
            ok, err = backup_service._send_xui_backup_to_telegram(
                self._server(), payload, '.db', '123456:SECRET-TOKEN', 'chat', None, datetime.utcnow())
        self.assertFalse(ok)
        self.assertIn('Max retries', err)
        self.assertNotIn('SECRET-TOKEN', err)  # token redacted
        self.assertFalse(os.path.exists(captured['path']))
        self.assertEqual(os.listdir(self.spool), [])

    def test_telegram_success_requires_document_metadata(self):
        self.assertFalse(backup_service._telegram_document_delivered(None))
        self.assertFalse(backup_service._telegram_document_delivered({'ok': True}))
        self.assertFalse(backup_service._telegram_document_delivered({'ok': True, 'result': {'message_id': 3}}))
        self.assertFalse(backup_service._telegram_document_delivered({'ok': True, 'result': {'message_id': 3, 'document': {}}}))
        self.assertFalse(backup_service._telegram_document_delivered({'ok': True, 'result': {'message_id': 0, 'document': {'file_id': 'x'}}}))
        self.assertTrue(backup_service._telegram_document_delivered({'ok': True, 'result': {'message_id': 3, 'document': {'file_id': 'x'}}}))

    def test_eve_backup_is_encrypted_before_upload(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode('ascii')
        plaintext = os.path.join(self.tmp.name, 'eve.db')
        with open(plaintext, 'wb') as handle:
            handle.write(b'eve-db-secret-bytes')
        captured = {}

        def fake_send(token, chat_id, file_path, caption, proxies=None, document_name=None):
            captured['path'] = file_path
            captured['data'] = open(file_path, 'rb').read()
            return mock.Mock(status_code=200)

        with mock.patch.dict(os.environ, {'EVE_BACKUP_KEY': key}), \
                mock.patch.object(backup_service, '_telegram_send_document', side_effect=fake_send), \
                mock.patch.object(backup_service, '_safe_response_json', return_value=(_ok_document(), None)):
            ok, err = backup_service._send_eve_backup_to_telegram(
                plaintext, 'bot-token', 'chat', None, datetime.utcnow(), work_dir=self.tmp.name)
            self.assertTrue(ok, err)
            self.assertTrue(captured['data'].startswith(BACKUP_MAGIC))
            self.assertNotIn(b'eve-db-secret-bytes', captured['data'])
            cipher = os.path.join(self.tmp.name, 'captured.eveenc')
            with open(cipher, 'wb') as handle:
                handle.write(captured['data'])
            restored = os.path.join(self.tmp.name, 'restored.db')
            decrypt_backup_file(cipher, restored)
            with open(restored, 'rb') as handle:
                self.assertEqual(handle.read(), b'eve-db-secret-bytes')
        self.assertFalse(os.path.exists(captured['path']))

    def test_spool_janitor_removes_stale_files_and_keeps_fresh(self):
        backup_service._xui_backup_spool_dir()
        stale = os.path.join(self.spool, f'{os.getpid()}-stale.db')
        fresh = os.path.join(self.spool, f'{os.getpid()}-fresh.db')
        for path in (stale, fresh):
            with open(path, 'wb') as handle:
                handle.write(b'x')
        past = time.time() - 3600
        os.utime(stale, (past, past))
        removed = backup_service.prune_xui_backup_spool(stale_seconds=60)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))

    @unittest.skipUnless(os.name == 'posix', 'PID probe is POSIX-only')
    def test_spool_janitor_removes_dead_owner_pid_immediately(self):
        backup_service._xui_backup_spool_dir()
        dead = os.path.join(self.spool, '2147483646-dead.db')
        with open(dead, 'wb') as handle:
            handle.write(b'x')
        removed = backup_service.prune_xui_backup_spool(stale_seconds=10 ** 9)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(dead))


class TelegramBackupPipelineTests(unittest.TestCase):
    """End-to-end wiring: the X-UI pipeline streams unencrypted and cleans up."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        app_module.db.create_all()

    @classmethod
    def tearDownClass(cls):
        app_module.db.session.remove()
        app_module.db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        from app import Server
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = os.path.join(self.tmp.name, 'spool')
        self._env = mock.patch.dict(os.environ, {'EVE_XUI_BACKUP_DIR': self.spool})
        self._env.start()
        Server.query.delete()
        app_module.db.session.commit()
        self.server = Server(name='phase0-panel', host='https://panel.invalid',
                             username='u', password='p', panel_type='auto', enabled=True)
        app_module.db.session.add(self.server)
        app_module.db.session.commit()

    def tearDown(self):
        from app import Server
        Server.query.delete()
        app_module.db.session.commit()
        self._env.stop()
        self.tmp.cleanup()

    def test_run_telegram_backup_sends_xui_unencrypted_then_deletes(self):
        payload = b'SQLite format 3\x00' + b'z' * 64
        captured = {}

        def fake_send(token, chat_id, file_path, caption, proxies=None, document_name=None):
            captured['path'] = file_path
            captured['data'] = open(file_path, 'rb').read()
            return mock.Mock(status_code=200)

        settings = {
            'enabled': True, 'send_panel_backup': False,
            'bot_token': 'tok', 'chat_id': 'chat',
        }
        with mock.patch.object(backup_service, '_get_telegram_backup_settings', return_value=settings), \
                mock.patch.object(backup_service, '_telegram_backup_route_proxies', return_value=({}, None)), \
                mock.patch.object(backup_service, 'get_xui_session', return_value=(mock.Mock(), None)), \
                mock.patch.object(backup_service, '_fetch_xui_backup', return_value=(payload, '.db', None)), \
                mock.patch.object(backup_service, '_telegram_send_document', side_effect=fake_send), \
                mock.patch.object(backup_service, '_safe_response_json', return_value=(_ok_document(), None)):
            result = backup_service._run_telegram_backup(trigger='manual')
        self.assertTrue(result['success'], result)
        self.assertEqual(captured['data'], payload)
        self.assertFalse(captured['data'].startswith(BACKUP_MAGIC))
        self.assertFalse(os.path.exists(captured['path']))
        self.assertEqual(os.listdir(self.spool), [])

if __name__ == '__main__':
    unittest.main()
