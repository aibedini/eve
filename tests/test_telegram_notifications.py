import base64
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['SERVER_PASSWORD_KEY'] = base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode()

from app import (  # noqa: E402
    Admin,
    CustomerAccount,
    GLOBAL_SERVER_DATA,
    Package,
    Server,
    ServiceOwnership,
    SystemConfig,
    TelegramBotInstance,
    TelegramIdentity,
    WhatsappBotLog,
    _get_telegram_depletion_settings,
    _notification_bot_for_reseller,
    _run_telegram_depletion_scan,
    app,
    db,
)


class FakeBotApi:
    def __init__(self):
        self.sent = []
        self.extras = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((int(chat_id), text))
        self.extras.append(kwargs)
        return ({'ok': True}, 'direct')


class TelegramNotificationTests(unittest.TestCase):
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
        self._previous_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        self.fake_api = FakeBotApi()
        self.bot_ids = []
        patcher = mock.patch('app._telegram_bot_api_client', side_effect=self._api_for_bot)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        GLOBAL_SERVER_DATA['inbounds'] = self._previous_inbounds
        db.session.rollback()
        WhatsappBotLog.query.delete()
        TelegramIdentity.query.delete()
        TelegramBotInstance.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        Server.query.delete()
        SystemConfig.query.filter(SystemConfig.key.startswith('tg_')).delete(
            synchronize_session=False)
        Package.query.filter(Package.name.like('tg-test-%')).delete(
            synchronize_session=False)
        Admin.query.filter(Admin.username.like('tg-test-%')).delete(
            synchronize_session=False)
        db.session.commit()

    def _api_for_bot(self, bot):
        self.bot_ids.append(bot.id)
        return self.fake_api

    def _admin(self, suffix, role='reseller', **kwargs):
        admin = Admin(username=f'tg-test-{suffix}', role=role, enabled=True, **kwargs)
        admin.set_password('StrongTgPassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _bot(self, owner=None, suffix='bot', enabled=True, archived=False, central=False):
        bot = TelegramBotInstance(
            scope_key='system' if central else f'tg-test-{suffix}-{id(object())}',
            owner_type='reseller' if owner else 'system',
            owner_admin_id=owner.id if owner else None,
            display_name='Notify Bot',
            enabled=enabled,
            token_encrypted='enc-token',
            archived_at=(datetime.utcnow() if archived else None),
        )
        db.session.add(bot)
        db.session.flush()
        return bot

    def _account(self, email, reseller=None, telegram_user_id=8_400_001, chat_id=8_400_001):
        customer = CustomerAccount(primary_phone='989150001111')
        server = Server(
            name='TG Notify Server', host='https://tgnotify.test',
            username='u', password='p')
        db.session.add_all([customer, server])
        db.session.flush()
        ownership = ServiceOwnership(
            customer_id=customer.id,
            server_id=server.id,
            client_uuid=f'tg-test-uuid-{email}',
            client_email_snapshot=email,
            reseller_id=reseller.id if reseller else None,
        )
        identity = TelegramIdentity(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
            customer_id=customer.id,
        )
        db.session.add_all([ownership, identity])
        db.session.flush()
        return server, ownership, identity

    def _enable_scan(self):
        db.session.add(SystemConfig(key='tg_depletion_enabled', value='true'))
        db.session.flush()

    def _package(self, suffix='pkg', volume=10, days=31, price=150_000):
        package = Package(
            name=f'tg-test-{suffix}', days=days, volume=volume, price=price,
            enabled=True, scope='global', show_on_sub=True)
        db.session.add(package)
        db.session.flush()
        return package

    def _inbounds(self, server, email, **client_overrides):
        client = {
            'email': email,
            'id': f'tg-test-uuid-{email}',
            'totalGB': 10 * 1024 ** 3,
            'up': 0, 'down': 0,
            'expiryTimestamp': int((time.time() + 2 * 86400) * 1000),
            'remaining_formatted': '9.5 GB',
        }
        client.update(client_overrides)
        return [{'server_id': server.id, 'server_name': server.name, 'id': 1,
                 'remark': 'main', 'clients': [client]}]

    # ── scan behavior ────────────────────────────────────────────────────

    def test_scan_disabled_by_default(self):
        server, _own, _identity = self._account('disabled@example.com')
        GLOBAL_SERVER_DATA['inbounds'] = self._inbounds(server, 'disabled@example.com')
        result = _run_telegram_depletion_scan()
        self.assertEqual(result.get('reason'), 'disabled')
        self.assertEqual(self.fake_api.sent, [])

    def test_near_expiry_sends_once_per_cooldown(self):
        self._enable_scan()
        central = self._bot(suffix='central', central=True)
        server, _own, _identity = self._account('near@example.com')
        GLOBAL_SERVER_DATA['inbounds'] = self._inbounds(server, 'near@example.com')
        first = _run_telegram_depletion_scan()
        self.assertEqual(first['sent'], 1)
        self.assertEqual(len(self.fake_api.sent), 1)
        chat_id, text = self.fake_api.sent[0]
        self.assertEqual(chat_id, 8_400_001)
        self.assertIn('near@example.com', text)
        self.assertEqual(self.bot_ids, [central.id])
        log = WhatsappBotLog.query.filter_by(event='tg_near_expiry').one()
        self.assertEqual(log.email, 'near@example.com')
        # Second run inside the cooldown window sends nothing.
        second = _run_telegram_depletion_scan()
        self.assertEqual(second['sent'], 0)
        self.assertEqual(len(self.fake_api.sent), 1)

    def test_low_volume_event(self):
        self._enable_scan()
        self._bot(suffix='central-vol', central=True)
        server, _own, _identity = self._account('vol@example.com')
        GLOBAL_SERVER_DATA['inbounds'] = self._inbounds(
            server, 'vol@example.com',
            expiryTimestamp=0,
            remaining_bytes=int(1.0 * 1024 ** 3),
        )
        result = _run_telegram_depletion_scan()
        self.assertEqual(result['sent'], 1)
        self.assertEqual(WhatsappBotLog.query.filter_by(event='tg_low_volume').count(), 1)

    def test_reseller_owned_account_uses_reseller_bot(self):
        self._enable_scan()
        reseller = self._admin('owner')
        reseller_bot = self._bot(owner=reseller, suffix='owned')
        central = self._bot(suffix='central-r', central=True)
        server, _own, _identity = self._account('reseller@example.com', reseller=reseller)
        GLOBAL_SERVER_DATA['inbounds'] = self._inbounds(server, 'reseller@example.com')
        result = _run_telegram_depletion_scan()
        self.assertEqual(result['sent'], 1)
        self.assertEqual(self.bot_ids, [reseller_bot.id])
        self.assertNotIn(central.id, self.bot_ids)

    def test_archived_or_disabled_reseller_bot_falls_back_to_central(self):
        reseller = self._admin('archived')
        self._bot(owner=reseller, suffix='archived-bot', archived=True)
        self._bot(owner=reseller, suffix='disabled-bot', enabled=False)
        central = self._bot(suffix='central-fallback', central=True)
        bot = _notification_bot_for_reseller(reseller.id)
        self.assertEqual(bot.id, central.id)

    def test_no_identity_no_send(self):
        self._enable_scan()
        self._bot(suffix='central-noid', central=True)
        customer = CustomerAccount(primary_phone='989150002222')
        server = Server(
            name='TG NoId Server', host='https://noid.test',
            username='u', password='p')
        db.session.add_all([customer, server])
        db.session.flush()
        db.session.add(ServiceOwnership(
            customer_id=customer.id, server_id=server.id,
            client_uuid='tg-test-uuid-noid', client_email_snapshot='noid@example.com'))
        db.session.flush()
        GLOBAL_SERVER_DATA['inbounds'] = self._inbounds(server, 'noid@example.com')
        result = _run_telegram_depletion_scan()
        self.assertEqual(result['sent'], 0)
        self.assertEqual(self.fake_api.sent, [])

    # ── package recommendation + quick-renew button ──────────────────────

    def _recommendable_inbounds(self, server, email, **overrides):
        """Near-expiry client whose 10 GB limit matches the test package, with
        live usage so the 31-day recommender has evidence."""
        base = {
            'totalGB': 10 * 1024 ** 3,
            'up': 3 * 1024 ** 3, 'down': 2 * 1024 ** 3,
        }
        base.update(overrides)
        return self._inbounds(server, email, **base)

    def test_recommend_adds_line_and_quick_renew_button(self):
        self._enable_scan()
        db.session.add(SystemConfig(key='tg_depletion_recommend', value='true'))
        self._bot(suffix='central-rec', central=True)
        package = self._package()
        server, ownership, _identity = self._account('rec@example.com')
        GLOBAL_SERVER_DATA['inbounds'] = self._recommendable_inbounds(server, 'rec@example.com')
        result = _run_telegram_depletion_scan()
        self.assertEqual(result['sent'], 1)
        chat_id, text = self.fake_api.sent[0]
        self.assertIn('tg-test-pkg', text)
        self.assertIn('💡', text)
        markup = self.fake_api.extras[0].get('reply_markup')
        self.assertIsNotNone(markup)
        buttons = markup['inline_keyboard'][0]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(
            buttons[0]['callback_data'],
            f'renew-pay-card:{ownership.id}:{package.id}')
        self.assertIn('tg-test-pkg', buttons[0]['text'])

    def test_recommend_off_keeps_message_unchanged(self):
        self._enable_scan()
        self._bot(suffix='central-norec', central=True)
        self._package()
        server, _own, _identity = self._account('norec@example.com')
        GLOBAL_SERVER_DATA['inbounds'] = self._recommendable_inbounds(server, 'norec@example.com')
        result = _run_telegram_depletion_scan()
        self.assertEqual(result['sent'], 1)
        _chat_id, text = self.fake_api.sent[0]
        self.assertNotIn('💡', text)
        self.assertNotIn('tg-test-pkg', text)
        self.assertNotIn('reply_markup', self.fake_api.extras[0])

    def test_recommend_without_usage_history_has_no_line_or_button(self):
        self._enable_scan()
        db.session.add(SystemConfig(key='tg_depletion_recommend', value='true'))
        self._bot(suffix='central-nohist', central=True)
        self._package()
        server, _own, _identity = self._account('nohist@example.com')
        # No up/down counters and no UsageDaily rows → no recommendation.
        GLOBAL_SERVER_DATA['inbounds'] = self._inbounds(server, 'nohist@example.com')
        result = _run_telegram_depletion_scan()
        self.assertEqual(result['sent'], 1)
        _chat_id, text = self.fake_api.sent[0]
        self.assertNotIn('💡', text)
        self.assertNotIn('reply_markup', self.fake_api.extras[0])

    # ── settings ─────────────────────────────────────────────────────────

    def test_settings_defaults_and_threshold_fallback(self):
        cfg = _get_telegram_depletion_settings()
        self.assertFalse(cfg['enabled'])
        self.assertFalse(cfg['recommend'])
        # Falls back to the shared WhatsApp/SMS thresholds when unset.
        self.assertEqual(cfg['expiry_days'], 3)
        self.assertEqual(cfg['volume_gb'], 2.0)
        self.assertEqual(cfg['cooldown_days'], 7)
        self.assertTrue(cfg['trigger_renew_success'])
        self.assertIn('{account_name}', cfg['tpl_renew'])
        self.assertIn('{account_name}', cfg['tpl_near_expiry'])
        db.session.add_all([
            SystemConfig(key='tg_depletion_enabled', value='true'),
            SystemConfig(key='tg_trigger_renew_success', value='false'),
            SystemConfig(key='tg_depletion_expiry_days', value='5'),
            SystemConfig(key='tg_tpl_renew', value='renewed {date}'),
            SystemConfig(key='tg_tpl_low_volume', value='custom {remaining_volume}'),
        ])
        db.session.flush()
        cfg = _get_telegram_depletion_settings()
        self.assertTrue(cfg['enabled'])
        self.assertFalse(cfg['trigger_renew_success'])
        self.assertEqual(cfg['expiry_days'], 5)
        self.assertEqual(cfg['tpl_renew'], 'renewed {date}')
        self.assertEqual(cfg['tpl_low_volume'], 'custom {remaining_volume}')

    def test_system_config_api_saves_telegram_notification_settings(self):
        admin = self._admin('super', role='superadmin', is_superadmin=True)
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['admin_id'] = admin.id
                sess['admin_username'] = admin.username
                sess['role'] = admin.role
                sess['is_superadmin'] = True
            response = client.post('/api/system-config', json={
                'tg_trigger_renew_success': False,
                'tg_depletion_enabled': True,
                'tg_depletion_recommend': True,
                'tg_depletion_expiry_days': 9,
                'tg_depletion_volume_gb': 1.5,
                'tg_depletion_cooldown_days': 11,
                'tg_tpl_renew': 'renew {account_name}',
                'tg_tpl_near_expiry': 'soon {remaining_time}',
                'tg_tpl_low_volume': 'low {remaining_volume}',
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        cfg = _get_telegram_depletion_settings()
        self.assertFalse(cfg['trigger_renew_success'])
        self.assertTrue(cfg['enabled'])
        self.assertTrue(cfg['recommend'])
        self.assertEqual(cfg['expiry_days'], 9)
        self.assertEqual(cfg['volume_gb'], 1.5)
        self.assertEqual(cfg['cooldown_days'], 11)
        self.assertEqual(cfg['tpl_renew'], 'renew {account_name}')


if __name__ == '__main__':
    unittest.main()
