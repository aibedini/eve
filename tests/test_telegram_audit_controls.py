import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from app import (  # noqa: E402
    Admin,
    AuditLog,
    BankCard,
    CustomerAccount,
    Package,
    Server,
    ServiceOwnership,
    TelegramBotInstance,
    TelegramBotUserState,
    TelegramIdentity,
    TelegramPurchaseRequest,
    TelegramPurchaseSession,
    TelegramTrialGrant,
    app,
    db,
)
from telegram_bot_worker import _handle_purchase_receipt, _rate_ok  # noqa: E402


class FakeBotApi:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append({'chat_id': int(chat_id), 'text': text})
        return ({'ok': True}, 'direct')

    def send_photo(self, *args, **kwargs):
        return ({'ok': True}, 'direct')

    def send_document(self, *args, **kwargs):
        return ({'ok': True}, 'direct')

    def answer_callback(self, *args, **kwargs):
        pass


class TelegramAuditControlsTests(unittest.TestCase):
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
        try:
            app_module.limiter.reset()
        except Exception:
            pass

    def tearDown(self):
        try:
            app_module.limiter.reset()
        except Exception:
            pass
        db.session.rollback()
        AuditLog.query.delete()
        TelegramTrialGrant.query.delete()
        TelegramPurchaseSession.query.delete()
        TelegramPurchaseRequest.query.delete()
        TelegramBotUserState.query.delete()
        TelegramIdentity.query.delete()
        TelegramBotInstance.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        Package.query.delete()
        BankCard.query.delete()
        Server.query.delete()
        Admin.query.filter(Admin.username.like('audit-test-%')).delete(
            synchronize_session=False)
        db.session.commit()
        db.session.remove()

    def _admin(self, suffix, role='superadmin', **kwargs):
        kwargs.setdefault('is_superadmin', role == 'superadmin')
        admin = Admin(username=f'audit-test-{suffix}', role=role, enabled=True, **kwargs)
        admin.set_password('StrongAuditPassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _client(self, admin):
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = admin.id
        return client

    def _bot(self, owner=None, suffix='bot', **kwargs):
        bot = TelegramBotInstance(
            scope_key=f'audit-test-{suffix}-{id(object())}',
            owner_type='reseller' if owner else 'system',
            owner_admin_id=owner.id if owner else None,
            display_name='Audit Bot',
            **kwargs,
        )
        db.session.add(bot)
        db.session.flush()
        return bot

    # ── audit log ────────────────────────────────────────────────────────

    def test_settings_save_and_lifecycle_actions_are_audited(self):
        superadmin = self._admin('super')
        bot = self._bot(suffix='audit')
        db.session.commit()
        client = self._client(superadmin)
        saved = client.post(
            f'/api/settings/telegram-bots?bot_id={bot.id}',
            json={'display_name': 'Audited Bot'},
        ).get_json()
        self.assertTrue(saved['success'])
        row = AuditLog.query.filter_by(action='telegram_bot.settings_update').one()
        self.assertEqual(row.actor_type, 'admin')
        self.assertEqual(row.actor_admin_id, superadmin.id)
        self.assertEqual(row.target_type, 'TelegramBotInstance')
        self.assertEqual(row.target_id, str(bot.id))

        disabled = client.post(
            f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'disable'}).get_json()
        self.assertTrue(disabled['success'])
        row = AuditLog.query.filter_by(action='telegram_bot.disable').one()
        self.assertEqual(row.actor_admin_id, superadmin.id)

    def test_purchase_reject_is_audited(self):
        superadmin = self._admin('super2')
        bot = self._bot(suffix='review')
        customer = CustomerAccount(primary_phone='989170001111')
        server = Server(name='Audit Server', host='https://audit.test',
                        username='u', password='p')
        package = Package(name='audit', days=30, volume=10, price=100_000, enabled=True)
        db.session.add_all([customer, server, package])
        db.session.flush()
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id, telegram_user_id=8_600_001,
            customer_id=customer.id, server_id=server.id, package_id=package.id,
            amount=100_000, receipt_file_id='file-id', source_chat_id=8_600_001,
            source_message_id=1, status='pending')
        db.session.add(request_row)
        db.session.commit()
        client = self._client(superadmin)
        response = client.post(
            f'/api/telegram-operations/purchases/{request_row.id}/reject').get_json()
        self.assertTrue(response['success'])
        row = AuditLog.query.filter_by(action='telegram_purchase.reject').one()
        self.assertEqual(row.actor_admin_id, superadmin.id)
        self.assertEqual(row.target_id, str(request_row.id))

    # ── web rate limiting ────────────────────────────────────────────────

    def test_runtime_action_rate_limit_blocks_after_threshold(self):
        superadmin = self._admin('super3')
        bot = self._bot(suffix='ratelimit')
        db.session.commit()
        client = self._client(superadmin)
        last_status = None
        for _ in range(21):
            last_status = client.post(
                f'/api/telegram-bots/{bot.id}/runtime',
                json={'action': 'disable'}).status_code
        self.assertEqual(last_status, 429)

    # ── worker sliding-window rate limit ─────────────────────────────────

    def test_rate_ok_sliding_window(self):
        for _ in range(3):
            self.assertTrue(_rate_ok(8_600_100, 'unit-test', 3, 60))
        self.assertFalse(_rate_ok(8_600_100, 'unit-test', 3, 60))
        # A different user or action is unaffected.
        self.assertTrue(_rate_ok(8_600_101, 'unit-test', 3, 60))
        self.assertTrue(_rate_ok(8_600_100, 'other-action', 3, 60))

    # ── duplicate receipt fraud flag + quoted amount freeze ──────────────

    def _receipt_setup(self):
        admin = self._admin('ops', telegram_id='555777')
        bot = self._bot(suffix='fraud')
        customer = CustomerAccount(primary_phone='989170002222')
        server = Server(name='Fraud Server', host='https://fraud.test',
                        username='u', password='p')
        package = Package(name='fraud', days=30, volume=10, price=100_000, enabled=True)
        card = BankCard(label='card', is_active=True)
        db.session.add_all([customer, server, package, card])
        db.session.flush()
        identity = TelegramIdentity(
            telegram_user_id=8_600_200, telegram_chat_id=8_600_200,
            customer_id=customer.id)
        db.session.add(identity)
        db.session.flush()
        state = TelegramBotUserState(
            bot_instance_id=bot.id, telegram_user_id=8_600_200, language='fa')
        session_row = TelegramPurchaseSession(
            bot_instance_id=bot.id, telegram_user_id=8_600_200,
            server_id=server.id, package_id=package.id, bank_card_id=card.id,
            quoted_amount=77_000, action='awaiting_receipt')
        db.session.add_all([state, session_row])
        db.session.flush()
        return bot, session_row, state

    def _send_receipt(self, bot, state, unique_id, user_id=8_600_200):
        api = FakeBotApi()
        message = {
            'chat': {'id': user_id},
            'message_id': 9,
            'photo': [{'file_size': 100, 'file_id': f'f-{unique_id}',
                       'file_unique_id': unique_id}],
        }
        _handle_purchase_receipt(api, bot, message, {'id': user_id}, state)
        return api

    def test_duplicate_receipt_flagged_and_admins_warned(self):
        bot, session_row, state = self._receipt_setup()
        db.session.commit()
        api = self._send_receipt(bot, state, 'uniq-1')
        first = TelegramPurchaseRequest.query.filter_by(
            receipt_file_unique_id='uniq-1').one()
        self.assertFalse(first.duplicate_receipt)
        # The frozen quoted amount wins over a fresh price resolve.
        self.assertEqual(first.amount, 77_000)

        # A different telegram user reusing the same receipt file gets
        # flagged, not auto-rejected.
        customer2 = CustomerAccount(primary_phone='989170004444')
        db.session.add(customer2)
        db.session.flush()
        db.session.add(TelegramIdentity(
            telegram_user_id=8_600_201, telegram_chat_id=8_600_201,
            customer_id=customer2.id))
        state2 = TelegramBotUserState(
            bot_instance_id=bot.id, telegram_user_id=8_600_201, language='fa')
        session2 = TelegramPurchaseSession(
            bot_instance_id=bot.id, telegram_user_id=8_600_201,
            server_id=first.server_id, package_id=first.package_id,
            bank_card_id=first.bank_card_id,
            quoted_amount=77_000, action='awaiting_receipt')
        db.session.add_all([state2, session2])
        db.session.flush()
        api2 = self._send_receipt(bot, state2, 'uniq-1', user_id=8_600_201)
        second = TelegramPurchaseRequest.query.filter(
            TelegramPurchaseRequest.receipt_file_unique_id == 'uniq-1',
            TelegramPurchaseRequest.id != first.id,
        ).one()
        self.assertTrue(second.duplicate_receipt)
        self.assertEqual(second.status, 'pending')
        admin_messages = [m['text'] for m in api2.messages if m['chat_id'] == 555777]
        self.assertTrue(any('FRAUD WARNING' in text for text in admin_messages))

    # ── per-bot counters ─────────────────────────────────────────────────

    def test_per_bot_counters(self):
        superadmin = self._admin('super4')
        bot_a = self._bot(suffix='counters-a')
        bot_b = self._bot(suffix='counters-b')
        customer = CustomerAccount(primary_phone='989170003333')
        server = Server(name='Counter Server', host='https://counter.test',
                        username='u', password='p')
        package = Package(name='counter', days=30, volume=10, price=100_000, enabled=True)
        db.session.add_all([customer, server, package])
        db.session.flush()

        def _purchase(bot, amount, status):
            row = TelegramPurchaseRequest(
                bot_instance_id=bot.id, telegram_user_id=8_600_300,
                customer_id=customer.id, server_id=server.id, package_id=package.id,
                amount=amount, receipt_file_id='file-id', source_chat_id=8_600_300,
                source_message_id=1, status=status)
            db.session.add(row)
            return row

        _purchase(bot_a, 100_000, 'completed')
        _purchase(bot_a, 50_000, 'approved')
        _purchase(bot_a, 10_000, 'pending')
        _purchase(bot_b, 200_000, 'rejected')
        db.session.commit()

        client = self._client(superadmin)
        payload = client.get('/api/telegram-operations').get_json()
        self.assertTrue(payload['success'])
        per_bot = {entry['bot_id']: entry for entry in payload['per_bot']}
        entry_a = per_bot[bot_a.id]
        self.assertEqual(entry_a['purchases'], 3)
        self.assertEqual(entry_a['completed'], 1)
        self.assertEqual(entry_a['revenue'], 150_000)
        self.assertAlmostEqual(entry_a['completion_rate'], round(1 / 3, 3))
        entry_b = per_bot[bot_b.id]
        self.assertEqual(entry_b['purchases'], 1)
        self.assertEqual(entry_b['revenue'], 0)
        self.assertEqual(entry_b['completion_rate'], 0.0)


if __name__ == '__main__':
    unittest.main()
