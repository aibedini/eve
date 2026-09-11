"""Regression tests for per-column secret domains in migrations and rotation.

The legacy encrypt_sensitive_values_v1 migration must encrypt each column with
the domain its model reads with, and the rotation runner must be able to move a
row that an earlier build encrypted under the wrong (generic) domain into the
column's current domain instead of skipping it forever.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from sqlalchemy import text  # noqa: E402

import maintenance as maintenance_module  # noqa: E402
from app import (  # noqa: E402
    Admin, BankCard, Payment, SystemConfig, SystemMigration, Transaction, app, db,
)
from panel.security import decrypt_secret, encrypt_secret, keyring  # noqa: E402
from panel.services import secret_rotation  # noqa: E402

MASTER = 'ZGV2ZWxvcG1lbnQtbWFzdGVyLWtleS0wMDAwMDAwMDAwMDAwMDAwMDA='
CARD = '6037997512345678'


class SecretDomainMigrationTests(unittest.TestCase):
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
        self._env = mock.patch.dict(os.environ, {'SERVER_PASSWORD_KEY': MASTER}, clear=False)
        self._env.start()
        for name in [key for key in os.environ if key.startswith('EVE_KEY_')]:
            os.environ.pop(name, None)
        keyring._key_b64.cache_clear()
        for model in (Payment, Transaction, BankCard, SystemConfig, SystemMigration, Admin):
            model.query.delete()
        db.session.commit()
        self.admin = Admin(username='domain-admin', role='superadmin', is_superadmin=True,
                           enabled=True)
        self.admin.set_password('CorrectHorseBattery1!')
        db.session.add(self.admin)
        db.session.commit()  # need the id for the finance rows below
        self.card = BankCard(label='legacy card', is_active=True)
        self.payment = Payment(admin_id=self.admin.id, amount=1000,
                               payment_date=datetime.utcnow())
        self.tx = Transaction(admin_id=self.admin.id, amount=1000, category='income')
        self.config = SystemConfig(key='sms_gmweb_api_key', value='')
        db.session.add_all([self.card, self.payment, self.tx, self.config])
        db.session.commit()

    def tearDown(self):
        self._env.stop()
        keyring._key_b64.cache_clear()

    def _stored(self, sql, params):
        return db.session.execute(text(sql), params).scalar()

    def test_legacy_migration_encrypts_each_column_with_its_model_domain(self):
        # Simulate a database written before column encryption existed.
        db.session.execute(text('UPDATE bank_cards SET card_number = :v WHERE id = :i'),
                          {'v': CARD, 'i': self.card.id})
        db.session.execute(text('UPDATE payments SET sender_card = :v WHERE id = :i'),
                          {'v': CARD, 'i': self.payment.id})
        db.session.execute(text('UPDATE transactions SET sender_card = :v WHERE id = :i'),
                          {'v': CARD, 'i': self.tx.id})
        db.session.execute(text('UPDATE system_configs SET value = :v WHERE key = :k'),
                          {'v': 'plaintext-api-key', 'k': 'sms_gmweb_api_key'})
        SystemMigration.query.filter_by(
            migration_id='encrypt_sensitive_values_v1').delete()
        db.session.commit()

        result = maintenance_module._run_secret_migration(batch_size=50)
        self.assertEqual(result['status'], 'complete')

        stored_card = self._stored('SELECT card_number FROM bank_cards WHERE id = :i',
                                   {'i': self.card.id})
        stored_payment = self._stored('SELECT sender_card FROM payments WHERE id = :i',
                                     {'i': self.payment.id})
        stored_tx = self._stored('SELECT sender_card FROM transactions WHERE id = :i',
                                {'i': self.tx.id})
        stored_config = self._stored('SELECT value FROM system_configs WHERE key = :k',
                                    {'k': 'sms_gmweb_api_key'})
        for value in (stored_card, stored_payment, stored_tx, stored_config):
            self.assertEqual(keyring.split_envelope(value)[0], 2)
        self.assertEqual(decrypt_secret(stored_card, 'finance'), CARD)
        self.assertEqual(decrypt_secret(stored_payment, 'finance'), CARD)
        self.assertEqual(decrypt_secret(stored_tx, 'finance'), CARD)
        self.assertEqual(decrypt_secret(stored_config, 'messaging'), 'plaintext-api-key')

        # The ORM reads each column with the same domain now, so the rows load.
        db.session.expire_all()
        self.assertEqual(db.session.get(BankCard, self.card.id).card_number, CARD)
        self.assertEqual(db.session.get(Payment, self.payment.id).sender_card, CARD)
        self.assertEqual(db.session.get(Transaction, self.tx.id).sender_card, CARD)

    def test_rotation_moves_wrong_domain_rows_into_the_column_domain(self):
        legacy = encrypt_secret(CARD, 'generic')  # what the pre-fix build wrote
        self.assertTrue(legacy.startswith('enc:v2:'))
        db.session.execute(text('UPDATE bank_cards SET card_number = :v WHERE id = :i'),
                          {'v': legacy, 'i': self.card.id})
        db.session.execute(text('UPDATE transactions SET sender_card = :v WHERE id = :i'),
                          {'v': legacy, 'i': self.tx.id})
        SystemMigration.query.filter_by(
            migration_id=secret_rotation.ROTATION_MIGRATION_ID).delete()
        db.session.commit()

        result = secret_rotation.run_rotation(batch_size=50)
        self.assertEqual(result['status'], 'complete')
        stored_card = self._stored('SELECT card_number FROM bank_cards WHERE id = :i',
                                   {'i': self.card.id})
        stored_tx = self._stored('SELECT sender_card FROM transactions WHERE id = :i',
                                {'i': self.tx.id})
        self.assertNotEqual(stored_card, legacy)
        self.assertNotEqual(stored_tx, legacy)
        self.assertEqual(decrypt_secret(stored_card, 'finance'), CARD)
        self.assertEqual(decrypt_secret(stored_tx, 'finance'), CARD)
        db.session.expire_all()
        self.assertEqual(db.session.get(BankCard, self.card.id).card_number, CARD)
        self.assertEqual(db.session.get(Transaction, self.tx.id).sender_card, CARD)
        ledger = SystemMigration.query.filter_by(
            migration_id=secret_rotation.ROTATION_MIGRATION_ID).one()
        self.assertEqual(json.loads(ledger.cursor_json or '{}').get('skipped', 0), 0)


if __name__ == '__main__':
    unittest.main()
