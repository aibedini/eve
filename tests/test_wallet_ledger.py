"""Phase 1 tests: atomic wallet mutations + immutable ledger.

Covers lost-update prevention, conditional debits, unique idempotency keys,
and balanced manual-receipt approve/reject.
"""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from sqlalchemy import text  # noqa: E402

from app import (  # noqa: E402
    Admin, CustomerAccount, ManualReceipt, WalletLedger, app, db,
    apply_receipt_credit, rollback_receipt_credit,
)
import panel.services.wallet as wallet  # noqa: E402


class WalletLedgerTests(unittest.TestCase):
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
        WalletLedger.query.delete()
        ManualReceipt.query.delete()
        Admin.query.delete()
        CustomerAccount.query.delete()
        db.session.commit()
        self.admin = Admin(
            username='ledger-admin', password_hash='x', role='admin',
            is_superadmin=False, enabled=True, credit=0,
        )
        self.customer = CustomerAccount(primary_phone='989170000123', credit=0)
        db.session.add_all([self.admin, self.customer])
        db.session.commit()

    def _scalar(self, sql, **params):
        return int(db.session.execute(text(sql), params).scalar() or 0)

    def _customer_balance(self):
        return self._scalar('SELECT credit FROM customer_accounts WHERE id = :id', id=self.customer.id)

    def _admin_balance(self):
        return self._scalar('SELECT credit FROM admins WHERE id = :id', id=self.admin.id)

    def test_atomic_delta_survives_a_stale_python_balance(self):
        stale_python_balance = int(self.customer.credit or 0)
        db.session.commit()  # release the read transaction before the other writer
        with db.engine.begin() as conn:
            conn.execute(
                text('UPDATE customer_accounts SET credit = 500 WHERE id = :id'),
                {'id': self.customer.id},
            )
        ok, reason = wallet.apply_balance_delta(
            'customer', self.customer.id, 50, entry_type='adjust')
        self.assertTrue(ok, reason)
        db.session.commit()
        # A Python read-modify-write from the stale 0 would have written 50.
        self.assertEqual(self._customer_balance(), 550)
        self.assertNotEqual(self._customer_balance(), stale_python_balance + 50)

    def test_increments_compose_across_two_sessions(self):
        with db.engine.begin() as conn:
            conn.execute(
                text('UPDATE customer_accounts SET credit = COALESCE(credit,0) + 100 WHERE id = :id'),
                {'id': self.customer.id},
            )
        db.session.expire_all()
        ok, reason = wallet.apply_balance_delta(
            'customer', self.customer.id, 250, entry_type='adjust')
        self.assertTrue(ok, reason)
        db.session.commit()
        self.assertEqual(self._customer_balance(), 350)

    def test_duplicate_idempotency_key_cannot_double_credit(self):
        ok, reason = wallet.apply_balance_delta(
            'customer', self.customer.id, 100, entry_type='topup', idempotency_key='k1')
        self.assertTrue(ok, reason)
        db.session.commit()
        dup, reason = wallet.apply_balance_delta(
            'customer', self.customer.id, 100, entry_type='topup', idempotency_key='k1')
        self.assertFalse(dup)
        self.assertEqual(reason, 'duplicate')
        db.session.commit()
        self.assertEqual(self._customer_balance(), 100)
        # Force the authoritative unique-index path instead of the fast check.
        with mock.patch.object(wallet, 'ledger_entry_exists', return_value=False):
            dup2, reason2 = wallet.apply_balance_delta(
                'customer', self.customer.id, 100, entry_type='topup', idempotency_key='k1')
        self.assertFalse(dup2)
        self.assertEqual(reason2, 'duplicate')
        self.assertEqual(self._customer_balance(), 100)
        self.assertEqual(WalletLedger.query.filter_by(owner_id=self.customer.id).count(), 1)

    def test_debit_is_conditional_and_never_goes_negative(self):
        ok, reason = wallet.apply_balance_delta(
            'customer', self.customer.id, 100, entry_type='topup')
        self.assertTrue(ok, reason)
        db.session.commit()
        ok, reason = wallet.apply_balance_delta(
            'customer', self.customer.id, -150, entry_type='purchase')
        self.assertFalse(ok)
        self.assertEqual(reason, 'insufficient_balance')
        db.session.rollback()
        self.assertEqual(self._customer_balance(), 100)
        self.assertEqual(WalletLedger.query.filter_by(owner_id=self.customer.id).count(), 1)
        ok, reason = wallet.apply_balance_delta(
            'customer', self.customer.id, -100, entry_type='purchase')
        self.assertTrue(ok, reason)
        db.session.commit()
        self.assertEqual(self._customer_balance(), 0)

    def test_receipt_double_approve_credits_once(self):
        receipt = ManualReceipt(admin_id=self.admin.id, amount=5_000, status='pending')
        db.session.add(receipt)
        db.session.commit()
        ok, error = apply_receipt_credit(receipt)
        self.assertTrue(ok, error)
        db.session.commit()
        self.assertEqual(self._admin_balance(), 5_000)
        again, error = apply_receipt_credit(receipt)
        self.assertFalse(again)
        self.assertIn('already', error)
        db.session.rollback()
        self.assertEqual(self._admin_balance(), 5_000)
        self.assertEqual(
            WalletLedger.query.filter_by(account_type='admin', reference_type='manual_receipt').count(), 1)

    def test_approved_receipt_rejected_once_leaves_no_orphan_credit(self):
        receipt = ManualReceipt(admin_id=self.admin.id, amount=4_000, status='pending')
        db.session.add(receipt)
        db.session.commit()
        ok, error = apply_receipt_credit(receipt)
        self.assertTrue(ok, error)
        db.session.commit()
        self.assertEqual(self._admin_balance(), 4_000)
        # Reject the approved receipt: status -> rejected and credit reversed in
        # one transaction.
        receipt.status = 'approved'
        db.session.commit()
        ok, error = rollback_receipt_credit(receipt, reason='fraud')
        self.assertTrue(ok, error)
        receipt.status = 'rejected'
        db.session.commit()
        self.assertEqual(self._admin_balance(), 0)
        # Reversing the same approved receipt twice must not subtract again: the
        # idempotency key makes the second attempt a no-op.
        dup, reason = rollback_receipt_credit(receipt, reason='dup')
        self.assertFalse(dup)
        self.assertEqual(reason, 'duplicate')
        db.session.rollback()
        self.assertEqual(self._admin_balance(), 0)
        self.assertEqual(
            WalletLedger.query.filter_by(account_type='admin', reference_type='manual_receipt').count(), 2)


if __name__ == '__main__':
    unittest.main()
