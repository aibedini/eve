"""Reseller deletion and assigned package tariffs through their API entry points."""
import os
import base64
import tempfile
import unittest
from unittest.mock import patch

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
os.environ['SERVER_PASSWORD_KEY'] = base64.urlsafe_b64encode(b'eve-reseller-test-key-32-bytes!!!!').decode()

from app import app, db  # noqa: E402
from panel.models import (  # noqa: E402
    Admin, AdminSession, ClientOwnership, CustomerAccount, Package, PriceTier,
    Server, ServiceOwnership, SystemConfig, Transaction,
)
from panel.services.billing import calculate_reseller_price  # noqa: E402
from panel.services.sessions import create_session, resolve_session  # noqa: E402


class ResellerManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        cls.context.pop()
        os.unlink(_DB.name)

    def setUp(self):
        for model in (ServiceOwnership, CustomerAccount, ClientOwnership, AdminSession,
                      Transaction, PriceTier, Package, SystemConfig, Server, Admin):
            model.query.delete()
        db.session.commit()
        db.session.expunge_all()
        self.owner = Admin(username='owner', password_hash='x', role='superadmin', is_superadmin=True)
        self.reseller = Admin(username='reseller', password_hash='x', role='reseller')
        self.reseller.set_password('Reseller-test-123!')
        self.other = Admin(username='other', password_hash='x', role='reseller')
        self.server = Server(name='server', host='https://example.com', username='u', password='p')
        db.session.add_all([self.owner, self.reseller, self.other, self.server])
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['admin_id'] = self.owner.id

    def _ownership(self):
        customer = CustomerAccount(display_name='customer')
        db.session.add(customer)
        db.session.flush()
        legacy = ClientOwnership(reseller_id=self.reseller.id, server_id=self.server.id,
                                 inbound_id=1, client_uuid='uuid', client_email='client')
        service = ServiceOwnership(customer_id=customer.id, server_id=self.server.id,
                                   client_uuid='uuid', reseller_id=self.reseller.id)
        db.session.add_all([legacy, service])
        db.session.commit()
        return legacy, service

    def test_delete_transfers_clients_and_revokes_access_preserving_ledger(self):
        legacy, service = self._ownership()
        token, row = create_session(self.reseller)
        tx = Transaction(admin_id=self.reseller.id, amount=-100, type='purchase')
        db.session.add(tx)
        db.session.commit()
        response = self.client.delete(f'/api/admins/{self.reseller.id}',
                                      json={'reseller_id': self.other.id})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(legacy.reseller_id, self.other.id)
        self.assertEqual(service.reseller_id, self.other.id)
        self.assertFalse(self.reseller.enabled)
        self.assertIsNotNone(row.revoked_at)
        self.assertIsNone(resolve_session(token))
        self.assertEqual(tx.admin_id, self.reseller.id)
        self.assertNotIn(self.reseller.id, [a['id'] for a in self.client.get('/api/admins').get_json()])
        self.assertEqual(self.client.put(f'/api/admins/{self.reseller.id}', json={'enabled': True}).status_code, 410)
        stolen = app.test_client()
        with stolen.session_transaction() as session:
            session['admin_id'] = self.reseller.id
        self.assertEqual(stolen.get('/api/packages').status_code, 401)
        fresh = app.test_client()
        self.assertFalse(fresh.post('/login', json={
            'username': 'reseller', 'password': 'Reseller-test-123!'}).get_json()['success'])

    def test_delete_to_system_and_retry(self):
        legacy, service = self._ownership()
        legacy_id = legacy.id
        response = self.client.delete(f'/api/admins/{self.reseller.id}', json={'reseller_id': 0})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIsNone(db.session.get(ClientOwnership, legacy_id))
        self.assertIsNone(service.reseller_id)
        self.assertEqual(self.client.delete(f'/api/admins/{self.reseller.id}').status_code, 200)

    def test_invalid_target_and_self_delete_leave_state_intact(self):
        legacy, service = self._ownership()
        for target_id in (self.reseller.id, self.owner.id, -1, 'bad', True, 3.5, {}):
            response = self.client.delete(f'/api/admins/{self.reseller.id}', json={'reseller_id': target_id})
            self.assertEqual(int(response.headers.get('X-Eve-Status', response.status_code)), 400,
                             (target_id, response.get_json()))
            self.assertTrue(self.reseller.enabled)
            self.assertEqual(legacy.reseller_id, self.reseller.id)
            self.assertEqual(service.reseller_id, self.reseller.id)
        response = self.client.delete(f'/api/admins/{self.owner.id}')
        self.assertEqual(int(response.headers.get('X-Eve-Status', response.status_code)), 400)

    def test_failed_transfer_rolls_back(self):
        legacy, _service = self._ownership()
        with patch('app.ensure_reseller_allowed_for_assignment', side_effect=RuntimeError('test')):
            response = self.client.delete(f'/api/admins/{self.reseller.id}', json={'reseller_id': self.other.id})
        self.assertEqual(response.status_code, 500)
        self.assertTrue(self.reseller.enabled)
        self.assertEqual(legacy.reseller_id, self.reseller.id)

    def test_assigned_tariff_overrides_package_and_updates_revision(self):
        package = Package(name='P', volume=10, days=30, price=50000, reseller_price=40000,
                          show_on_sub=True)
        tier = PriceTier(name='specific', assigned_reseller_ids=f'[{self.reseller.id}]',
                         cost_per_gb=100, cost_per_day=10)
        db.session.add_all([package, tier])
        db.session.commit()
        with self.client.session_transaction() as session:
            session['admin_id'] = self.reseller.id
        response = self.client.get('/api/packages?purpose=create')
        self.assertEqual(response.get_json()[0]['price'], 1300)
        etag = response.headers['ETag']
        self.assertEqual(self.client.get('/api/my-packages').get_json()['items'][0]['price'], 1300)
        from panel.services.billing import _build_sub_page_packages
        self.assertEqual(_build_sub_page_packages(self.reseller)[0]['price'], 1300)
        self.assertEqual(calculate_reseller_price(self.other, package=package), 40000)
        tier.cost_per_gb = 200
        db.session.commit()
        updated = self.client.get('/api/packages?purpose=create', headers={'If-None-Match': etag})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()[0]['price'], 2300)
        self.assertNotEqual(updated.headers['ETag'], etag)

    def test_tariff_scope_and_zero_rate(self):
        package = Package(name='P', volume=10, days=30, price=50000)
        tier = PriceTier(name='specific', reseller_id=self.reseller.id, server_id=self.server.id,
                         max_volume_gb=20, cost_per_gb=0, cost_per_day=0)
        db.session.add_all([package, tier])
        db.session.commit()
        self.assertEqual(calculate_reseller_price(self.reseller, package=package), 50000)
        self.assertEqual(calculate_reseller_price(self.reseller, package=package,
                                                 server_id=self.server.id), 0)
        tier.is_active = False
        db.session.commit()
        self.assertEqual(calculate_reseller_price(self.reseller, package=package,
                                                 server_id=self.server.id), 50000)

    def test_custom_reseller_rates_override_package_price(self):
        package = Package(name='P', volume=10, days=30, price=50000, reseller_price=40000)
        self.reseller.custom_cost_per_gb = 200
        self.reseller.custom_cost_per_day = 10
        db.session.add(package)
        db.session.commit()
        self.assertEqual(calculate_reseller_price(self.reseller, package=package), 2300)
