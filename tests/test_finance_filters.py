import os
import tempfile
import unittest
from datetime import datetime


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import Admin, Payment, Transaction, app, db  # noqa: E402


class FinanceFilterRegressionTests(unittest.TestCase):
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
        Transaction.query.delete()
        Payment.query.delete()
        Admin.query.delete()
        admin = Admin(username='owner', password_hash='x', role='superadmin', is_superadmin=True)
        db.session.add(admin)
        db.session.commit()
        self.admin_id = admin.id
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin_id

    def tearDown(self):
        db.session.remove()

    def test_finance_stats_match_filtered_transaction_list_after_edit(self):
        tx = Transaction(
            admin_id=self.admin_id,
            amount=0,
            type='renew',
            category='income',
            description='User Renewal (Free) - free-user',
            client_email='free-user',
            created_at=datetime.utcnow(),
        )
        other_tx = Transaction(
            admin_id=self.admin_id,
            amount=990_000,
            type='purchase',
            category='income',
            description='Purchase - other-user',
            client_email='other-user',
            created_at=datetime.utcnow(),
        )
        other_payment = Payment(
            admin_id=self.admin_id,
            amount=333_000,
            description='Manual payment - payment-user',
            client_email='payment-user',
            payment_date=datetime.utcnow(),
            verified=True,
        )
        db.session.add_all([tx, other_tx, other_payment])
        db.session.commit()

        resp = self.client.put(f'/api/transactions/{tx.id}', json={
            'amount': '150000',
            'is_expense': False,
            'type': 'renew',
            'client_email': 'free-user',
            'description': 'corrected free renewal amount',
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        list_resp = self.client.get('/api/payments?search=free-user&type=renew&direction=income')
        self.assertEqual(list_resp.status_code, 200, list_resp.get_data(as_text=True))
        list_data = list_resp.get_json()
        self.assertEqual(list_data['total'], 1)
        self.assertEqual(list_data['payments'][0]['id'], f'tx-{tx.id}')
        self.assertEqual(list_data['payments'][0]['amount'], 150_000)

        stats_resp = self.client.get('/api/finance/stats?search=free-user&type=renew&direction=income')
        self.assertEqual(stats_resp.status_code, 200, stats_resp.get_data(as_text=True))
        stats = stats_resp.get_json()['stats']
        self.assertEqual(stats['month'], 150_000)
        self.assertEqual(stats['total'], 150_000)
        self.assertEqual(stats['payment_count'], 1)

        payment_type_resp = self.client.get('/api/finance/stats?search=free-user&type=payment&direction=income')
        self.assertEqual(payment_type_resp.status_code, 200, payment_type_resp.get_data(as_text=True))
        payment_type_stats = payment_type_resp.get_json()['stats']
        self.assertEqual(payment_type_stats['month'], 0)
        self.assertEqual(payment_type_stats['total'], 0)
        self.assertEqual(payment_type_stats['payment_count'], 0)

        overview_resp = self.client.get('/api/finance/overview?range=30d&search=free-user&type=renew&direction=income')
        self.assertEqual(overview_resp.status_code, 200, overview_resp.get_data(as_text=True))
        overview = overview_resp.get_json()['series']
        self.assertEqual(sum(overview['income']), 150_000)
        self.assertEqual(sum(overview['expense']), 0)


if __name__ == '__main__':
    unittest.main()
