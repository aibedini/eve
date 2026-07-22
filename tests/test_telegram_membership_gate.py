import os
import tempfile
import time
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    Admin,
    TelegramBotInstance,
    app,
    db,
)
from telegram_bot_runtime import TelegramApiError  # noqa: E402
from telegram_bot_worker import _CHANNEL_MEMBER_CACHE, _channel_member  # noqa: E402


class FakeApi:
    def __init__(self, member_status='administrator', error=None):
        self.member_status = member_status
        self.error = error
        self.calls = []

    def call(self, method, payload=None, **kwargs):
        self.calls.append((method, dict(payload or {})))
        if method == 'getMe':
            return ({'id': 777000111, 'username': 'eve_gate_bot'}, 'direct')
        if method == 'getChatMember':
            if self.error:
                raise self.error
            return ({'status': self.member_status}, 'direct')
        return ({}, 'direct')


class RequiredChannelVerificationTests(unittest.TestCase):
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

    def tearDown(self):
        db.session.rollback()
        TelegramBotInstance.query.delete()
        Admin.query.filter(Admin.username.like('gate-test-%')).delete(
            synchronize_session=False)
        db.session.commit()
        db.session.remove()

    def _superadmin(self):
        admin = Admin(username='gate-test-super', role='superadmin',
                      is_superadmin=True, enabled=True)
        admin.set_password('StrongGatePassword123!')
        db.session.add(admin)
        db.session.commit()
        return admin

    def _client(self, admin):
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = admin.id
        return client

    def _bot(self, bot_user_id=777000111):
        bot = TelegramBotInstance(scope_key='system', owner_type='system',
                                  display_name='Gate test bot',
                                  bot_user_id=bot_user_id,
                                  bot_username='eve_gate_bot')
        db.session.add(bot)
        db.session.commit()
        return bot

    def _save(self, client, channels):
        return client.post('/api/settings/telegram-bots', json={
            'display_name': 'Gate test bot',
            'required_channels': channels,
            'require_membership_on_start': True,
        })

    _CHANNEL = {
        'chat_id': -1002256258079,
        'title': 'TNTvpnima',
        'invite_url': 'https://t.me/TNTvpnima',
    }

    def _assert_rejected(self, response):
        # The CDN shim downgrades JSON 4xx bodies to HTTP 200 and keeps the real
        # code in X-Eve-Status; the view itself returned 400.
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')
        payload = response.get_json()
        self.assertFalse(payload['success'])
        return payload['error']

    def test_admin_bot_channel_saves_successfully(self):
        admin = self._superadmin()
        bot = self._bot()
        api = FakeApi(member_status='administrator')
        with mock.patch('app._telegram_bot_api_client', return_value=api):
            response = self._save(self._client(admin), [self._CHANNEL])
        self.assertEqual(response.status_code, 200, response.get_json())
        db.session.expire_all()
        stored = db.session.get(TelegramBotInstance, bot.id)
        self.assertEqual(stored.required_channels()[0]['title'], 'TNTvpnima')
        self.assertTrue(stored.require_membership_on_start)
        member_calls = [call for call in api.calls if call[0] == 'getChatMember']
        self.assertEqual(member_calls, [('getChatMember', {
            'chat_id': -1002256258079, 'user_id': 777000111,
        })])
        self.assertNotIn('getMe', [call[0] for call in api.calls])

    def test_non_admin_status_rejects_with_clear_message(self):
        admin = self._superadmin()
        self._bot()
        api = FakeApi(member_status='member')
        with mock.patch('app._telegram_bot_api_client', return_value=api):
            response = self._save(self._client(admin), [self._CHANNEL])
        error = self._assert_rejected(response)
        self.assertIn('TNTvpnima', error)
        self.assertIn('admin', error)

    def test_chat_not_found_rejects_with_clear_message(self):
        admin = self._superadmin()
        self._bot()
        api = FakeApi(error=TelegramApiError('Bad Request: chat not found'))
        with mock.patch('app._telegram_bot_api_client', return_value=api):
            response = self._save(self._client(admin), [self._CHANNEL])
        error = self._assert_rejected(response)
        self.assertIn('TNTvpnima', error)
        self.assertIn('admin', error)
        self.assertIn('chat not found', error)

    def test_network_failure_rejects_with_soft_message(self):
        admin = self._superadmin()
        self._bot()
        api = FakeApi(error=TelegramApiError('direct: connect timeout'))
        with mock.patch('app._telegram_bot_api_client', return_value=api):
            response = self._save(self._client(admin), [self._CHANNEL])
        self.assertIn('Could not verify', self._assert_rejected(response))

    def test_getme_called_and_identity_persisted_when_missing(self):
        admin = self._superadmin()
        bot = self._bot(bot_user_id=None)
        api = FakeApi(member_status='administrator')
        with mock.patch('app._telegram_bot_api_client', return_value=api):
            response = self._save(self._client(admin), [self._CHANNEL])
        self.assertEqual(response.status_code, 200, response.get_json())
        methods = [call[0] for call in api.calls]
        self.assertEqual(methods[0], 'getMe')
        db.session.expire_all()
        stored = db.session.get(TelegramBotInstance, bot.id)
        self.assertEqual(int(stored.bot_user_id), 777000111)
        self.assertEqual(stored.bot_username, 'eve_gate_bot')

    def test_verification_skipped_without_token(self):
        admin = self._superadmin()
        self._bot()
        # No token configured: the real client builder raises ValueError and
        # verification is skipped instead of blocking the save.
        response = self._save(self._client(admin), [self._CHANNEL])
        self.assertEqual(response.status_code, 200, response.get_json())


class ChannelMemberGateTests(unittest.TestCase):
    def setUp(self):
        _CHANNEL_MEMBER_CACHE.clear()

    def tearDown(self):
        _CHANNEL_MEMBER_CACHE.clear()

    def test_chat_access_error_fails_open_with_short_cache(self):
        api = FakeApi(error=TelegramApiError('Bad Request: chat not found'))
        with app.app_context():
            self.assertTrue(_channel_member(api, -100111, 42))
        expiry, ok = _CHANNEL_MEMBER_CACHE[(-100111, 42)]
        self.assertTrue(ok)
        self.assertLessEqual(expiry - time.monotonic(), 61)

    def test_network_error_fails_open_with_short_cache(self):
        api = FakeApi(error=ConnectionError('proxy is down'))
        with app.app_context():
            self.assertTrue(_channel_member(api, -100222, 42))
        expiry, ok = _CHANNEL_MEMBER_CACHE[(-100222, 42)]
        self.assertTrue(ok)
        self.assertLessEqual(expiry - time.monotonic(), 61)

    def test_left_status_blocks_with_normal_cache(self):
        api = FakeApi(member_status='left')
        with app.app_context():
            self.assertFalse(_channel_member(api, -100333, 42))
        expiry, ok = _CHANNEL_MEMBER_CACHE[(-100333, 42)]
        self.assertFalse(ok)
        self.assertGreater(expiry - time.monotonic(), 61)

    def test_member_status_passes(self):
        api = FakeApi(member_status='member')
        with app.app_context():
            self.assertTrue(_channel_member(api, -100444, 42))
        expiry, ok = _CHANNEL_MEMBER_CACHE[(-100444, 42)]
        self.assertTrue(ok)
        self.assertGreater(expiry - time.monotonic(), 61)


if __name__ == '__main__':
    unittest.main()
