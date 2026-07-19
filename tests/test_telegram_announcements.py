import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    Admin, CustomerAccount, CustomerTransaction, Server, ServiceOwnership,
    TelegramAnnouncement, TelegramAnnouncementDelivery, TelegramBotInstance,
    TelegramBotUserState, TelegramIdentity, _queue_telegram_announcement,
    _run_telegram_announcement_batch, _telegram_announcement_recipients, app, db,
)


class FakeApi:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((int(chat_id), text))


class TelegramAnnouncementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context(); cls.ctx.push(); db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove(); db.drop_all(); cls.ctx.pop()
        try: os.unlink(_DB_FILE.name)
        except OSError: pass

    def setUp(self):
        self.admin = Admin(username='announce-admin', role='superadmin', enabled=True, is_superadmin=True)
        self.admin.set_password('StrongPassword123!')
        self.customer = CustomerAccount(primary_phone='989121234567')
        self.server = Server(name='Announcement server', host='https://announce.test', username='u', password='p')
        db.session.add_all([self.admin, self.customer, self.server]); db.session.flush()
        self.bot = TelegramBotInstance(scope_key='system', owner_type='system', display_name='Central', enabled=True)
        db.session.add(self.bot); db.session.flush()
        self.identity = TelegramIdentity(customer_id=self.customer.id, telegram_user_id=99101, telegram_chat_id=99101)
        self.state = TelegramBotUserState(bot_instance_id=self.bot.id, telegram_user_id=99101, step='verified', created_at=datetime.utcnow() - timedelta(days=3))
        db.session.add_all([self.identity, self.state, ServiceOwnership(
            customer_id=self.customer.id, server_id=self.server.id, client_uuid='uuid-a', client_email_snapshot='a')])
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        for model in (TelegramAnnouncementDelivery, TelegramAnnouncement, CustomerTransaction,
                      ServiceOwnership, TelegramBotUserState, TelegramIdentity,
                      TelegramBotInstance, Server, CustomerAccount, Admin):
            model.query.delete()
        db.session.commit()

    def test_audience_filters_server_link_and_renewal_range(self):
        db.session.add(CustomerTransaction(customer_id=self.customer.id, type='renewal', amount=-10,
            status='completed', created_at=datetime.utcnow() - timedelta(hours=2)))
        db.session.commit()
        rows = _telegram_announcement_recipients({
            'bot_scope': 'all', 'server_ids': [self.server.id], 'linked_only': True,
            'event_match': 'all', 'renewed_from': (datetime.utcnow() - timedelta(days=1)).isoformat(),
            'renewed_to': None, 'started_from': None, 'started_to': None,
            'purchased_from': None, 'purchased_to': None,
        })
        self.assertEqual([99101], [row['telegram_user_id'] for row in rows])

    def test_queue_is_idempotent_and_worker_reports_success(self):
        row = TelegramAnnouncement(title='Notice', message_text='Hello', created_by_admin_id=self.admin.id,
            filters_json='{"bot_scope":"selected","bot_ids":[' + str(self.bot.id) + ']}')
        db.session.add(row); db.session.commit()
        _queue_telegram_announcement(row); db.session.commit()
        self.assertEqual(1, TelegramAnnouncementDelivery.query.count())
        fake = FakeApi()
        with mock.patch('app._telegram_bot_api_client', return_value=fake):
            self.assertEqual(1, _run_telegram_announcement_batch())
        db.session.refresh(row)
        self.assertEqual('completed', row.status)
        self.assertEqual(1, row.sent_count)
        self.assertEqual([(99101, 'Hello')], fake.sent)


if __name__ == '__main__':
    unittest.main()
