"""Recent sender card reuse checks across manual payments and client operations."""

import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from sqlalchemy import inspect

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault('SERVER_PASSWORD_KEY', base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['FLASK_ENV'] = 'development'

from app import app, db  # noqa: E402
from panel.models import Admin, BankCard, Payment, SystemSetting, Transaction  # noqa: E402
from panel.services.payment_duplicate import last_four, recent_sender_card_matches  # noqa: E402


class PaymentDuplicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.context.pop()
        try:
            os.unlink(_DB.name)
        except OSError:
            pass

    def setUp(self):
        Transaction.query.delete()
        Payment.query.delete()
        BankCard.query.delete()
        SystemSetting.query.delete()
        Admin.query.delete()
        self.admin = Admin(username='owner', password_hash='x', role='superadmin', is_superadmin=True)
        self.card = BankCard(label='Main card', is_active=True)
        self.other_card = BankCard(label='Other card', is_active=True)
        db.session.add_all([self.admin, self.card, self.other_card])
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['admin_id'] = self.admin.id

    def tearDown(self):
        db.session.rollback()
        db.session.remove()

    def test_matching_sources_and_scope(self):
        now = datetime.utcnow()
        db.session.add_all([
            Payment(admin_id=self.admin.id, card_id=self.card.id, sender_card='۶۰۳۷ ۳۷۹۶',
                    amount=120000, payment_date=now, created_at=now, client_email='manual@example.com'),
            Transaction(admin_id=self.admin.id, card_id=self.card.id, sender_card='3796',
                        amount=90000, category='income', created_at=now - timedelta(hours=1),
                        client_email='renew@example.com'),
            Payment(admin_id=self.admin.id, card_id=self.other_card.id, sender_card='3796',
                    amount=1, payment_date=now, created_at=now),
            Transaction(admin_id=self.admin.id, card_id=self.card.id, sender_card='3796',
                        amount=1, category='expense', created_at=now),
            Transaction(admin_id=self.admin.id, card_id=self.card.id, sender_card='3796',
                        amount=1, category='income', created_at=now - timedelta(hours=25)),
        ])
        db.session.commit()

        self.assertEqual(last_four('۶۰۳۷ ۳۷۹۶'), '3796')
        matches = recent_sender_card_matches(self.card.id, '3796', admin_id=self.admin.id, now=now)
        self.assertEqual([item['user'] for item in matches], ['manual@example.com', 'renew@example.com'])

    def test_endpoint_requires_setting_and_returns_only_match_details(self):
        now = datetime.utcnow()
        db.session.add(Payment(admin_id=self.admin.id, card_id=self.card.id, sender_card='12343796',
                               amount=50000, payment_date=now, created_at=now,
                               client_email='client@example.com'))
        db.session.commit()
        url = f'/api/payments/check-sender-card?card_id={self.card.id}&sender_card=3796'
        self.assertEqual(self.client.get(url).status_code, 404)
        db.session.add(SystemSetting(key='payment_duplicate_check_enabled', value='1'))
        db.session.commit()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['matches'][0]['user'], 'client@example.com')
        self.assertEqual(response.json['matches'][0]['amount'], 50000)
        self.assertNotIn('12343796', response.get_data(as_text=True))

    def test_general_setting_turns_check_on_and_off(self):
        self.assertFalse(self.client.get('/api/settings/general').json['payment_duplicate_check_enabled'])
        enabled = self.client.post('/api/settings/general', json={'payment_duplicate_check_enabled': True})
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json['payment_duplicate_check_enabled'])
        self.assertTrue(self.client.get('/api/settings/general').json['payment_duplicate_check_enabled'])
        dashboard = self.client.get('/')
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('payment-card-check.js', dashboard.get_data(as_text=True))
        disabled = self.client.post('/api/settings/general', json={'payment_duplicate_check_enabled': False})
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json['payment_duplicate_check_enabled'])
        self.assertNotIn('payment-card-check.js', self.client.get('/').get_data(as_text=True))

    def test_recent_lookup_has_destination_time_indexes(self):
        for table, name in (
            ('payments', 'ix_payments_card_created'),
            ('transactions', 'ix_transactions_card_created'),
        ):
            indexes = {index['name']: index['column_names'] for index in inspect(db.engine).get_indexes(table)}
            self.assertEqual(indexes[name], ['card_id', 'created_at'])


if __name__ == '__main__':
    unittest.main()
