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
    Admin, AdminSession, ClientOperation, ClientOwnership, CustomerAccount, Package, PriceTier,
    ResellerContract, ResellerOffer, Server, ServiceOwnership, SystemConfig, Transaction, WalletLedger,
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
        for model in (WalletLedger, ClientOperation, ResellerOffer, ResellerContract,
                      ServiceOwnership, CustomerAccount, ClientOwnership, AdminSession,
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
        item = self.client.get('/api/my-packages').get_json()['items'][0]
        self.assertEqual(item['price'], 50000)
        self.assertEqual(item['reseller_cost'], 1300)
        from panel.services.billing import _build_sub_page_packages
        self.assertEqual(_build_sub_page_packages(self.reseller)[0]['price'], 50000)
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

    def test_package_cost_modes_keep_customer_price_independent(self):
        from app import _telegram_purchase_packages_payload
        from panel.models import TelegramBotInstance
        from panel.services.billing import _build_sub_page_packages
        from telegram_bot_worker import _resolve_purchase_price
        package = Package(name='retail', days=30, volume=10, price=123456, show_on_sub=True)
        db.session.add(package)
        db.session.commit()
        for settings, expected in [
            ({'reseller_cost_mode': 'fixed', 'reseller_price': 0}, 0),
            ({'reseller_cost_mode': 'fixed', 'reseller_price': 75000}, 75000),
            ({'reseller_cost_mode': 'percent', 'reseller_discount_percent': 12.5, 'reseller_rounding': 0}, 108024),
            ({'reseller_cost_mode': 'percent', 'reseller_discount_percent': 12.5, 'reseller_rounding': 1000}, 108000),
        ]:
            response = self.client.put(f'/admin/packages/{package.id}', json=settings)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(calculate_reseller_price(self.reseller, package=package), expected)
            self.assertEqual(_build_sub_page_packages(self.reseller)[0]['price'], 123456)
            self.assertEqual(_resolve_purchase_price(self.reseller.id, package), 123456)
        bot = TelegramBotInstance(owner_admin_id=self.reseller.id)
        self.assertEqual(_telegram_purchase_packages_payload(bot)[0]['price'], 123456)
        package.price = 125000
        package.reseller_discount_percent = 10
        self.assertEqual(calculate_reseller_price(self.reseller, package=package), 113000)

    def test_invalid_cost_settings_leave_package_unchanged(self):
        response = self.client.post('/admin/packages', json={
            'name': 'fixed', 'days': 30, 'volume': 10, 'price': 100000,
            'reseller_cost_mode': 'fixed', 'reseller_price': 40000})
        self.assertEqual(response.status_code, 200, response.get_json())
        package = db.session.get(Package, response.get_json()['id'])
        for bad in ({'reseller_price': -1}, {'reseller_price': True},
                    {'reseller_price': 1.5}, {'reseller_cost_mode': 'unknown'},
                    {'reseller_discount_percent': 101}, {'reseller_discount_percent': 'nan'},
                    {'reseller_rounding': -1}, {'reseller_rounding': 0.5}):
            response = self.client.put(f'/admin/packages/{package.id}', json={'name': 'changed', **bad})
            self.assertEqual(int(response.headers.get('X-Eve-Status', response.status_code)), 400)
            self.assertEqual(package.name, 'fixed')
            self.assertEqual(package.reseller_price, 40000)

    def test_reseller_tariff_components_and_range_boundaries(self):
        from panel.routes.packages import _calculate_minimum_price
        package = Package(name='P', days=30, volume=10, price=50000, reseller_cost_mode='tariff')
        tier = PriceTier(name='range', min_volume_gb=10, max_volume_gb=20,
                         reseller_id=self.reseller.id, cost_per_gb=1000, cost_per_day=100,
                         reseller_cost_per_gb=200)
        db.session.add_all([package, tier, SystemConfig(key='cost_per_gb', value='3000'),
                            SystemConfig(key='cost_per_day', value='500')])
        db.session.commit()
        for gb, day, expected in [(200, None, 2000), (None, 20, 600), (200, 20, 2600), (0, 0, 0)]:
            tier.reseller_cost_per_gb, tier.reseller_cost_per_day = gb, day
            self.assertEqual(calculate_reseller_price(self.reseller, package=package), expected)
            self.assertEqual(_calculate_minimum_price(10, 30, self.reseller.id, user=self.reseller)[0], expected)
            self.assertEqual(_calculate_minimum_price(10, 30)[0], 45000)
        self.assertEqual(_calculate_minimum_price(20, 30, self.reseller.id, user=self.reseller)[0], 75000)
        self.assertEqual(_calculate_minimum_price(9, 30, self.reseller.id, user=self.reseller)[0], 42000)
        self.reseller.custom_cost_per_gb = 250
        self.assertEqual(_calculate_minimum_price(9, 30, self.reseller.id, user=self.reseller)[0], 2250)
        self.reseller.custom_cost_per_gb = None
        self.reseller.custom_cost_per_day = 25
        self.assertEqual(_calculate_minimum_price(9, 30, self.reseller.id, user=self.reseller)[0], 750)

    def test_reseller_can_hide_last_inherited_package(self):
        from panel.services.billing import _build_sub_page_packages
        package = Package(name='visible', days=30, volume=10, price=50000, show_on_sub=True)
        db.session.add(package)
        db.session.commit()
        with self.client.session_transaction() as session:
            session['admin_id'] = self.reseller.id
        self.assertTrue(self.client.get('/api/my-packages').get_json()['items'][0]['sub_shown'])
        for desired in (False, True, False):
            response = self.client.post(f'/api/my-packages/{package.id}/sub-toggle', json={'show_on_sub': desired})
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(bool(_build_sub_page_packages(self.reseller)), desired)
            self.assertEqual(self.client.get('/api/my-packages').get_json()['items'][0]['sub_shown'], desired)

    def test_tier_api_persists_and_validates_reseller_costs(self):
        response = self.client.post('/api/price-tiers', json={
            'name': 'one rate', 'reseller_cost_per_day': 250,
            'assigned_reseller_ids': [self.reseller.id]})
        self.assertEqual(response.status_code, 200, response.get_json())
        tier_id = response.get_json()['id']
        self.assertEqual(response.get_json()['tier']['reseller_cost_per_day'], 250)
        for bad in (-1, True, 1.5, 'nan'):
            response = self.client.put(f'/api/price-tiers/{tier_id}', json={'reseller_cost_per_day': bad})
            self.assertEqual(int(response.headers.get('X-Eve-Status', response.status_code)), 400)
        response = self.client.put(f'/api/price-tiers/{tier_id}', json={
            'reseller_cost_per_day': None, 'reseller_cost_per_gb': 0})
        self.assertEqual(response.get_json()['tier']['reseller_cost_per_gb'], 0)
        self.assertIsNone(response.get_json()['tier']['reseller_cost_per_day'])

    def test_catalog_publication_and_price_are_isolated_by_reseller(self):
        from panel.services.billing import _build_sub_page_packages
        package = Package(name='Shared', volume=20, days=30, price=150000, show_on_sub=True)
        db.session.add(package)
        db.session.commit()
        endpoint = f'/api/reseller-pricing/{self.reseller.id}'
        response = self.client.put(endpoint + f'/offer/{package.id}', json={
            'selling_price': 180000, 'wholesale_price': 110000, 'published': True})
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(_build_sub_page_packages(self.reseller)[0]['price'], 180000)
        self.assertEqual(_build_sub_page_packages(self.other)[0]['price'], 150000)
        self.assertEqual(_build_sub_page_packages(None)[0]['price'], 150000)
        ops = self.client.get(f'/api/packages?purpose=create&reseller_id={self.reseller.id}').get_json()
        self.assertEqual(ops[0]['price'], 110000)
        with self.client.session_transaction() as session:
            session['admin_id'] = self.reseller.id
        response = self.client.put(f'/api/my-packages/{package.id}/offer', json={
            'selling_price': 190000, 'wholesale_price': 1, 'published': False})
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(_build_sub_page_packages(self.reseller), [])
        self.assertEqual(self.client.get('/api/packages?purpose=create').get_json()[0]['price'], 110000)

    def test_private_panel_permissions_and_separate_customer_publication(self):
        from panel.services.billing import _build_sub_page_packages, package_allowed_in_panel
        package = Package(
            name='Private customer offer', volume=20, days=30, price=150000, show_on_sub=True,
            scope='personal', created_by=self.reseller.id,
        )
        db.session.add(package)
        db.session.commit()
        self.assertEqual(len(_build_sub_page_packages(self.reseller)), 1)
        self.assertEqual(_build_sub_page_packages(self.other), [])
        self.assertFalse(package_allowed_in_panel(package, self.owner, self.reseller.id, 'renew'))
        package.panel_roles = {'create': ['reseller'], 'renew': ['superadmin']}
        db.session.commit()
        _, service = self._ownership()
        service.client_email_snapshot = 'client'
        db.session.commit()
        self.assertTrue(package_allowed_in_panel(package, self.reseller, self.reseller.id, 'create'))
        rows = self.client.get(f'/api/packages?purpose=renew&server_id={self.server.id}&email=client').get_json()
        self.assertEqual([p['id'] for p in rows], [package.id])
        self.assertEqual(self.client.get('/api/packages?purpose=create').get_json(), [])

    def test_billing_owner_uses_the_target_inbound_for_duplicate_legacy_names(self):
        from panel.services.billing import pricing_owner
        db.session.add_all([
            ClientOwnership(reseller_id=self.reseller.id, server_id=self.server.id,
                            inbound_id=1, client_uuid='first', client_email='same-name'),
            ClientOwnership(reseller_id=self.other.id, server_id=self.server.id,
                            inbound_id=2, client_uuid='second', client_email='same-name'),
        ])
        db.session.commit()
        self.assertEqual(pricing_owner(self.server.id, email='same-name', inbound_id=1), self.reseller.id)
        self.assertEqual(pricing_owner(self.server.id, email='same-name', inbound_id=2), self.other.id)
        self.assertIsNone(pricing_owner(self.server.id, email='same-name'))

    def test_contract_quotes_and_operator_independent_reservation(self):
        from panel.services.billing import pricing_quote
        from panel.services.client_operations import begin_client_operation, fail_client_operation
        package = Package(name='Shared', volume=20, days=30, price=150000)
        self.reseller.credit = 200000
        db.session.add(package)
        db.session.commit()
        endpoint = f'/api/reseller-pricing/{self.reseller.id}'
        response = self.client.put(endpoint, json={
            'mode': 'percent', 'percent': 70, 'basis': 'reference', 'rounding': 1000, 'version': 0})
        self.assertTrue(response.get_json()['success'])
        stale = self.client.put(endpoint, json={'mode': 'fixed', 'fixed': 1, 'version': 0})
        self.assertFalse(stale.get_json()['success'])
        quote = pricing_quote(self.reseller, package=package, server_id=self.server.id)
        self.assertEqual(quote['amount'], 105000)
        kwargs = dict(idempotency_key='one-charge', action='renew', admin=self.owner,
                      server_id=self.server.id, amount=quote['amount'], quote=quote)
        op, state, _ = begin_client_operation(**kwargs)
        self.assertEqual(state, 'new')
        self.assertEqual(op.admin_id, self.owner.id)
        self.assertEqual(op.billing_admin_id, self.reseller.id)
        self.assertEqual(op.pricing_quote['contract_version'], 1)
        db.session.refresh(self.reseller)
        self.assertEqual(self.reseller.credit, 95000)
        self.assertEqual(begin_client_operation(**kwargs)[1], 'in_progress')
        fail_client_operation(op, 'confirmed rejected')
        self.assertEqual(db.session.get(Admin, self.reseller.id).credit, 200000)
        response = self.client.put(endpoint, json={'mode': 'percent', 'percent': 40,
                                                  'basis': 'net_sale', 'version': 1})
        self.assertTrue(response.get_json()['success'])
        with self.assertRaisesRegex(ValueError, 'approved customer sale'):
            pricing_quote(self.reseller, package=package)
        self.assertEqual(pricing_quote(self.reseller, package=package, sale_amount=120000)['amount'], 48000)

    def test_quota_grant_reservation_and_refund_are_idempotent(self):
        from panel.services.billing import pricing_quote
        from panel.services.client_operations import begin_client_operation, fail_client_operation
        endpoint = f'/api/reseller-pricing/{self.reseller.id}'
        self.assertTrue(self.client.put(endpoint, json={'mode': 'quota'}).get_json()['success'])
        for _ in range(2):
            self.assertTrue(self.client.post(endpoint + '/quota', json={'gb': 100, 'key': 'one-grant'}).get_json()['success'])
        contract = db.session.get(ResellerContract, self.reseller.id)
        self.assertEqual(contract.quota_mib, 102400)
        quote = pricing_quote(self.reseller, volume_gb=20.5, days=30)
        self.assertEqual((quote['amount'], quote['quota_mib']), (0, 20992))
        with self.assertRaisesRegex(ValueError, 'Unlimited'):
            pricing_quote(self.reseller, days=30, unlimited=True)
        args = dict(idempotency_key='one-quota-operation', action='renew', admin=self.owner,
                    server_id=self.server.id, amount=0, quote=quote)
        op, state, _ = begin_client_operation(**args)
        self.assertEqual(state, 'new')
        self.assertEqual(begin_client_operation(**args)[1], 'in_progress')
        db.session.refresh(contract)
        self.assertEqual(contract.quota_mib, 81408)
        fail_client_operation(op, 'confirmed rejected')
        fail_client_operation(op, 'repeated failure')
        self.assertEqual(db.session.get(ResellerContract, self.reseller.id).quota_mib, 102400)
        self.assertEqual(WalletLedger.query.filter_by(account_type='reseller_quota').count(), 3)
        args['idempotency_key'] = 'too-much'
        args['quote'] = pricing_quote(self.reseller, volume_gb=101, days=30)
        self.assertEqual(begin_client_operation(**args)[1], 'insufficient_credit')

    def test_creation_requotes_after_contract_edit_and_records_reseller_as_payer(self):
        self.reseller.credit = 200000
        package = Package(name='Panel', volume=20, days=30, price=150000)
        db.session.add(package)
        db.session.commit()
        endpoint = f'/api/reseller-pricing/{self.reseller.id}'
        contract = {'mode': 'fixed', 'fixed': 110000, 'version': 0}
        self.assertTrue(self.client.put(endpoint, json=contract).get_json()['success'])
        list_url = f'/api/packages?purpose=create&reseller_id={self.reseller.id}&server_id={self.server.id}'
        old = self.client.get(list_url).get_json()[0]
        contract.update(fixed=115000, version=1)
        self.assertTrue(self.client.put(endpoint, json=contract).get_json()['success'])
        payload = {'mode': 'package', 'package_id': package.id, 'email': 'new-customer',
                   'reseller_id': self.reseller.id, 'operation_id': 'pricing-request',
                   'quote_revision': old['quote_revision']}
        create_url = f'/api/client/{self.server.id}/1/add'
        with patch('app.get_xui_session', return_value=(None, 'fixture rejects panel write')) as panel:
            response = self.client.post(create_url, json=payload)
            self.assertEqual(response.get_json()['code'], 'pricing_changed', response.get_json())
            panel.assert_not_called()
            payload['quote_revision'] = self.client.get(list_url).get_json()[0]['quote_revision']
            self.client.post(create_url, json=payload)
            panel.assert_called_once()
        op = ClientOperation.query.filter_by(idempotency_key='pricing-request').one()
        self.assertEqual(op.billing_admin_id, self.reseller.id)
        self.assertEqual(op.amount, 115000)
        self.assertEqual(op.state, 'failed')
        db.session.refresh(self.reseller)
        self.assertEqual(self.reseller.credit, 200000)
