"""Phase 6 tests: financial identifier masking and the audited reveal paths.

Covers the pure masking helpers, the model serializers, the list endpoints that
must never leak a full card number, the mask round-trip guard on update, and the
permission/scope/audit behaviour of the reveal endpoints.
"""
import os
import tempfile
import unittest
from datetime import datetime

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    Admin, AdminPermission, AdminSession, AuditLog, BankCard, Payment, Transaction,
    app, db,
)
from panel.core.finance_privacy import (  # noqa: E402
    is_masked_value, mask_account_like, mask_card_number, mask_iban,
)

CARD = '6037997512345678'
MASKED = '6037********5678'
OTHER_CARD = '6104337812349876'


class MaskingHelperTests(unittest.TestCase):
    def test_card_number_keeps_the_first_and_last_four_digits(self):
        self.assertEqual(mask_card_number(CARD), MASKED)
        self.assertEqual(mask_card_number('6037-9975-1234-5678'), MASKED)

    def test_masking_is_idempotent(self):
        self.assertEqual(mask_card_number(MASKED), MASKED)

    def test_short_values_are_fully_masked(self):
        self.assertIsNone(mask_card_number(None))
        self.assertIsNone(mask_card_number(''))
        self.assertEqual(mask_card_number('1234'), '****')
        self.assertEqual(mask_card_number('12345'), '*2345')

    def test_account_like_and_iban_keep_only_the_last_four(self):
        iban = 'IR820540102680020817909002'
        masked = mask_account_like(iban)
        self.assertTrue(masked.endswith('9002'))
        self.assertNotIn('8205401026800208', masked)
        self.assertEqual(mask_iban(iban), masked)

    def test_is_masked_value_detects_a_resubmitted_mask(self):
        self.assertTrue(is_masked_value(MASKED, CARD, mask_card_number))
        self.assertTrue(is_masked_value(f'  {MASKED}  ', CARD, mask_card_number))
        self.assertFalse(is_masked_value(OTHER_CARD, CARD, mask_card_number))
        self.assertFalse(is_masked_value('', CARD, mask_card_number))
        self.assertFalse(is_masked_value(MASKED, None, mask_card_number))


class ModelMaskingTests(unittest.TestCase):
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

    def _round_trip(self, obj):
        db.session.add(obj)
        db.session.commit()
        db.session.expire_all()
        return db.session.get(type(obj), obj.id)

    def test_bank_card_dict_is_masked_and_reveal_dict_is_not(self):
        card = self._round_trip(BankCard(
            label='central', card_number=CARD,
            iban='IR820540102680020817909002', account_number='1234567890',
        ))
        payload = card.to_dict()
        self.assertEqual(payload['card_number'], MASKED)
        self.assertEqual(payload['masked_card'], MASKED)
        self.assertFalse(payload['revealed'])
        self.assertTrue(payload['iban'].endswith('9002'))
        self.assertTrue(payload['account_number'].endswith('7890'))
        revealed = card.to_reveal_dict()
        self.assertEqual(revealed['card_number'], CARD)
        self.assertTrue(revealed['revealed'])

    def test_payment_and_transaction_dicts_mask_the_sender_card(self):
        admin = Admin(username='privacy-owner', role='superadmin', is_superadmin=True,
                      enabled=True)
        admin.set_password('CorrectHorseBattery1!')
        admin = self._round_trip(admin)
        payment = self._round_trip(Payment(admin_id=admin.id, amount=10_000,
                                           payment_date=datetime.utcnow(),
                                           sender_card=CARD))
        self.assertEqual(payment.to_dict()['sender_card'], MASKED)
        tx = self._round_trip(Transaction(admin_id=admin.id, amount=10_000,
                                          category='income', sender_card=CARD))
        self.assertEqual(tx.to_dict()['sender_card'], MASKED)


class FinancialPrivacyApiTests(unittest.TestCase):
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
        AuditLog.query.delete()
        Payment.query.delete()
        Transaction.query.delete()
        BankCard.query.delete()
        AdminPermission.query.delete()
        AdminSession.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.superadmin = Admin(username='privacy-root', role='superadmin',
                                is_superadmin=True, enabled=True)
        self.superadmin.set_password('CorrectHorseBattery1!')
        self.admin = Admin(username='privacy-admin', role='admin', enabled=True)
        self.admin.set_password('CorrectHorseBattery1!')
        self.reseller = Admin(username='privacy-reseller', role='reseller', enabled=True,
                              allowed_servers='[]')
        self.reseller.set_password('CorrectHorseBattery1!')
        db.session.add_all([self.superadmin, self.admin, self.reseller])
        db.session.commit()
        self.card = BankCard(label='central', card_number=CARD, iban='IR820540102680020817909002',
                             account_number='9876543210', is_active=True)
        self.private_card = BankCard(label='reseller card', card_number=OTHER_CARD,
                                     reseller_id=self.reseller.id, is_active=True)
        db.session.add_all([self.card, self.private_card])
        db.session.commit()
        self.payment = Payment(admin_id=self.admin.id, amount=25_000,
                               payment_date=datetime.utcnow(), sender_card=CARD,
                               sender_name='Customer')
        self.other_payment = Payment(admin_id=self.superadmin.id, amount=30_000,
                                     payment_date=datetime.utcnow(), sender_card=OTHER_CARD)
        self.tx = Transaction(admin_id=self.admin.id, amount=25_000, category='income',
                              sender_card=CARD, description='deposit')
        db.session.add_all([self.payment, self.other_payment, self.tx])
        db.session.commit()
        self.client = app.test_client()

    def _login(self, admin):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = admin.id
            sess['role'] = admin.role
            sess['is_superadmin'] = bool(admin.is_superadmin)

    def test_card_list_never_contains_a_full_number(self):
        self._login(self.admin)
        response = self.client.get('/api/bank-cards')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn(CARD, body)
        self.assertIn(MASKED, body)
        cards = {card['id']: card for card in response.get_json()['cards']}
        self.assertEqual(cards[self.card.id]['card_number'], MASKED)
        self.assertFalse(cards[self.card.id]['revealed'])

    def test_transaction_list_never_contains_a_full_number(self):
        self._login(self.admin)
        body = self.client.get('/api/transactions').get_data(as_text=True)
        self.assertNotIn(CARD, body)
        self.assertIn(MASKED, body)

    def test_payment_list_never_contains_a_full_number(self):
        self._login(self.admin)
        body = self.client.get('/api/payments').get_data(as_text=True)
        self.assertNotIn(CARD, body)
        self.assertIn(MASKED, body)

    def test_resubmitting_the_mask_keeps_the_stored_number(self):
        self._login(self.admin)
        response = self.client.put(f'/api/bank-cards/{self.card.id}', json={
            'label': 'renamed', 'card_number': MASKED,
        })
        self.assertEqual(response.status_code, 200, response.data)
        db.session.expire_all()
        card = db.session.get(BankCard, self.card.id)
        self.assertEqual(card.card_number, CARD)
        self.assertEqual(card.label, 'renamed')
        self.assertEqual(response.get_json()['card']['card_number'], MASKED)

    def test_a_new_number_replaces_the_stored_one(self):
        self._login(self.admin)
        response = self.client.put(f'/api/bank-cards/{self.card.id}',
                                   json={'card_number': OTHER_CARD})
        self.assertEqual(response.status_code, 200, response.data)
        db.session.expire_all()
        self.assertEqual(db.session.get(BankCard, self.card.id).card_number, OTHER_CARD)

    def test_reveal_requires_the_permission(self):
        self._login(self.reseller)
        response = self.client.post(f'/api/bank-cards/{self.card.id}/reveal')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['code'], 'forbidden')

    def test_reveal_returns_the_full_value_and_audits_the_access(self):
        self._login(self.admin)
        response = self.client.post(f'/api/bank-cards/{self.card.id}/reveal')
        self.assertEqual(response.status_code, 200, response.data)
        payload = response.get_json()['card']
        self.assertEqual(payload['card_number'], CARD)
        self.assertTrue(payload['revealed'])
        rows = AuditLog.query.filter_by(action='bank_card.reveal').all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].target_id, str(self.card.id))
        self.assertIsNone(rows[0].meta_json)

    def test_reveal_respects_the_card_access_scope(self):
        self._login(self.admin)
        denied = self.client.post(f'/api/bank-cards/{self.private_card.id}/reveal')
        self.assertEqual(denied.status_code, 403)
        self._login(self.superadmin)
        allowed = self.client.post(f'/api/bank-cards/{self.private_card.id}/reveal')
        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.assertEqual(allowed.get_json()['card']['card_number'], OTHER_CARD)

    def test_payment_reveal_is_scoped_to_the_owner(self):
        self._login(self.admin)
        mine = self.client.post(f'/api/payments/{self.payment.id}/reveal')
        self.assertEqual(mine.status_code, 200, mine.data)
        self.assertEqual(mine.get_json()['sender_card'], CARD)
        not_mine = self.client.post(f'/api/payments/{self.other_payment.id}/reveal')
        self.assertEqual(not_mine.status_code, 403)

    def test_transaction_reveal_returns_the_full_value(self):
        self._login(self.admin)
        response = self.client.post(f'/api/transactions/{self.tx.id}/reveal')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.get_json()['sender_card'], CARD)
        self.assertEqual(
            AuditLog.query.filter_by(action='transaction.sender_card_reveal').count(), 1,
        )

    def test_reveal_of_a_missing_entry_is_a_404(self):
        self._login(self.admin)
        self.assertEqual(self.client.post('/api/payments/999999/reveal').status_code, 404)
        self.assertEqual(self.client.post('/api/transactions/999999/reveal').status_code, 404)
        self.assertEqual(self.client.post('/api/bank-cards/999999/reveal').status_code, 404)


if __name__ == '__main__':
    unittest.main()
