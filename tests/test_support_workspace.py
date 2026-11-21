"""Support boundaries, web chat, shared budgets and payroll."""

import os
import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_db_file = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_db_file.close()
os.environ['DATABASE_URL'] = 'sqlite:///' + _db_file.name.replace(os.sep, '/')
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import app, db  # noqa: E402
from panel.models import (  # noqa: E402
    Admin, ClientPortalUser, CustomerAccount, Package, Server, ServiceOwnership, SupportActivity, SupportBudget,
    SupportOperator, SystemSetting, Transaction,
)
from panel.services import support  # noqa: E402


class SupportMigrationTest(unittest.TestCase):
    def test_upgrade_is_repeatable_and_preserves_existing_accounts(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[1] / 'alembic/versions/8ae6d12c4b97_support_operations.py'
        spec = spec_from_file_location('support_revision', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine('sqlite://')
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE admins (id INTEGER PRIMARY KEY)')
                connection.exec_driver_sql('INSERT INTO admins VALUES (37)')
                connection.exec_driver_sql('CREATE TABLE service_ownerships (id INTEGER PRIMARY KEY)')
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    migration.upgrade()
                self.assertEqual(connection.exec_driver_sql('SELECT id FROM admins').scalar(), 37)
                inspector = sa.inspect(connection)
                self.assertTrue(all(inspector.has_table(table) for table in migration.TABLES))
                self.assertIn('account_filter', {column['name'] for column in inspector.get_columns('support_operators')})
        finally:
            engine.dispose()


class SupportWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        db.session.remove()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        self.manager = Admin(username='manager', role='superadmin', is_superadmin=True, password_hash='x')
        self.staff = Admin(username='agent', role='support', password_hash='x', telegram_id='23456')
        self.other_owner = Admin(username='shop', role='reseller', password_hash='x')
        self.server = Server(name='S1', host='http://test.invalid', username='x', password='x')
        self.customer = CustomerAccount(primary_phone='+989121234567', phone_verified_at=datetime.utcnow())
        self.other_customer = CustomerAccount(primary_phone='+989121234568', phone_verified_at=datetime.utcnow())
        self.portal = ClientPortalUser(mobile=self.customer.primary_phone, password_hash='x', must_change_password=False)
        db.session.add_all([self.manager, self.staff, self.other_owner, self.server,
                            self.customer, self.other_customer, self.portal])
        db.session.flush()
        self.service = ServiceOwnership(customer_id=self.customer.id, server_id=self.server.id,
                                        client_uuid='uuid-one', client_email_snapshot='customer-one')
        self.other_service = ServiceOwnership(customer_id=self.other_customer.id, server_id=self.server.id,
                                              client_uuid='uuid-two', client_email_snapshot='customer-two',
                                              reseller_id=self.other_owner.id)
        self.profile = SupportOperator(admin_id=self.staff.id, locations={str(self.server.id): [1]},
                                       overrides={'sms': True}, sms_limit=1)
        db.session.add_all([self.profile, self.service, self.other_service])
        db.session.commit()
        self.snapshot = {'inbounds': [{'id': 1, 'server_id': self.server.id, 'clients': [
            {'email': 'customer-one', 'raw_client': {'id': 'uuid-one', 'enable': True}},
            {'email': 'customer-two', 'raw_client': {'id': 'uuid-two', 'enable': True}},
        ]}]}
        for module, name, data in [('app', 'GLOBAL_SERVER_DATA', self.snapshot),
                                   ('panel.core.redis_client', 'load_snapshot_from_redis', lambda: True)]:
            patcher = patch(f'{module}.{name}', data)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def login(self, *, admin=None, portal=None):
        with self.client.session_transaction() as session:
            session.clear()
            if admin:
                session['admin_id'] = admin.id
            if portal:
                session['client_id'] = portal.id

    def test_staff_workspace_hides_business_and_checks_scope_on_api(self):
        self.login(admin=self.staff)
        self.assertEqual(self.client.get('/').location, '/support')
        for endpoint in ('/api/servers', '/api/telegram-operations', '/api/finance/reseller-statement'):
            self.assertEqual(self.client.get(endpoint).status_code, 403)
        workspace = self.client.get('/support')
        self.assertEqual(workspace.status_code, 200)
        self.assertIn(b'support-search', workspace.data)
        self.assertNotIn(b'<div class="wallet-pill"', workspace.data)
        results = self.client.get('/api/support/customers?q=customer').get_json()['items']
        self.assertEqual([row['email'] for row in results], ['customer-one'])
        self.profile.account_filter = {str(self.server.id): ['some-other-uuid']}
        db.session.commit()
        self.assertEqual(self.client.get('/api/support/customers?q=customer').get_json()['items'], [])

    def test_manager_creates_operator_with_override_and_locations(self):
        self.login(admin=self.manager)
        self.assertEqual(self.client.get('/support/team').status_code, 200)
        response = self.client.post('/api/support/team/operators', json={
            'username': 'second.agent', 'password': 'Long-supported-password-123',
            'enabled': True, 'locations': [f'{self.server.id}:1'], 'overrides': {'renew': False},
            'sms_limit': 2, 'free_limit': 0, 'free_max_gb': 0,
        })
        self.assertEqual(response.status_code, 200, response.data)
        profile = db.session.get(SupportOperator, response.get_json()['id'])
        self.assertNotIn('renew', support.capabilities_for(profile))
        self.assertIn('customers', support.capabilities_for(profile))

    def test_portal_chat_checks_customer_and_attributes_operator(self):
        service_id = self.service.id
        self.login(portal=self.portal)
        self.assertEqual(self.client.get('/client/support').status_code, 200)
        self.assertEqual(self.client.get(f'/api/support/portal-chat/{self.other_service.id}').status_code, 404)
        self.assertEqual(self.client.post(f'/api/support/portal-chat/{service_id}',
                                          json={'message': 'Hello'}).status_code, 200)
        self.login(admin=self.staff)
        self.assertEqual(self.client.get(f'/support/chat/{service_id}').status_code, 200)
        reply = self.client.post(f'/api/support/chat/{service_id}', json={'message': 'Resolved', 'complete': True})
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(reply.get_json()['open'])
        event = SupportActivity.query.filter_by(kind='reply').one()
        self.assertEqual((event.operator_id, event.customer_id), (self.staff.id, self.customer.id))
        self.assertEqual(self.client.get('/support/earnings').status_code, 200)

    def test_renewal_requires_scoped_account_and_a_matching_current_quote(self):
        from flask import g, jsonify

        self.login(admin=self.staff)
        package = Package(name='Standard', days=30, volume=20, price=10000, enabled=True)
        db.session.add(package)
        db.session.commit()
        prefix = f'/api/support/{self.server.id}/1/renew/'
        packages = self.client.get(f'/api/support/packages/{self.server.id}/1/renew').get_json()
        revision = packages['items'][0]['revision']
        body = {'email': 'customer-one', 'mode': 'package', 'package_id': package.id, 'free': False,
                'quote_revision': revision}
        headers = {'Idempotency-Key': 'a-scoped-renew-operation'}
        with patch('panel.routes.clients.renew_client') as renew:
            renew.side_effect = lambda *_: jsonify(success=g.support_client_uuid == 'uuid-one',
                                                     final_state='applied_active', wholesale=300)
            denied = self.client.post(prefix + 'customer-two', json={**body, 'email': 'customer-two'},
                                      headers=headers)
            self.assertEqual(denied.status_code, 404)
            changed = self.client.post(prefix + 'customer-one', json={**body, 'quote_revision': '0' * 64},
                                       headers=headers)
            self.assertEqual(changed.get_json()['code'], 'pricing_changed')
            result = self.client.post(prefix + 'customer-one', json=body, headers=headers).get_json()
            self.assertTrue(result['success'])
            self.assertNotIn('wholesale', result)
            renew.assert_called_once()

    def test_sms_reserves_both_limits_and_counts_only_delivery_for_pay(self):
        self.login(admin=self.staff)
        db.session.add(SystemSetting(key='support:0:sms_limit', value='3'))
        db.session.commit()
        path = f'/api/support/account/{self.server.id}/1/customer-one/sms'
        key = '1234-5678-9101-1121-3141'
        with patch('app._send_sms_via_gmweb', return_value={'accepted': True, 'request_id': 'send_1'}) as sender:
            result = self.client.post(path, json={'key': key, 'message': 'Hello'})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.get_json()['success'])
            used = db.session.get(SupportBudget, (f'sms:operator:{self.staff.id}', datetime.utcnow().date()))
            self.assertEqual(used.used, 1)
            self.client.post(path, json={'key': key, 'message': 'Hello'})
            response = self.client.post(path, json={'key': '9999-' + key, 'message': 'Other'})
            self.assertEqual(response.get_json()['code'], 'daily_sms_limit')
            sender.assert_called_once()
        event = SupportActivity.query.filter_by(kind='sms').one()
        support.record_sms_delivery({
            'event_type': 'sms.delivered', 'carrier_status': 'delivered', 'message_id': 'send_1',
            'diagnostics_json': '{"segment_count":2,"all_segments_delivered":false}',
        })
        self.assertEqual(event.state, 'accepted')
        support.record_sms_delivery({
            'event_type': 'sms.delivered', 'carrier_status': 'delivered', 'message_id': 'send_1',
            'diagnostics_json': '{"segment_count":2,"all_segments_delivered":true}',
        })
        support.record_sms_delivery({'event_type': 'sms.delivery_failed', 'carrier_status': 'failed',
                                     'message_id': 'send_1'})
        db.session.commit()
        self.assertEqual(event.state, 'delivered')
        self.assertEqual(event.details['segments'], 2)

    def test_tier_commission_and_one_expense_per_settlement(self):
        self.login(admin=self.manager)
        now = datetime.utcnow().replace(day=1)
        event_at = now - timedelta(days=2)
        event = SupportActivity(operator_id=self.staff.id, kind='renew', reference='op-1',
                                state='completed', created_at=event_at, details={
                                    'volume_gb': 120, 'contract_id': 1,
                                    'terms': {'mode': 'tiers', 'tiers': [[100, 200], [None, 300]]},
                                })
        db.session.add(event)
        db.session.commit()
        approved = self.client.post(f'/api/support/team/operators/{self.staff.id}/payroll',
                                    json={'month': event_at.strftime('%Y-%m')}).get_json()
        self.assertTrue(approved['success'])
        for _ in range(2):
            result = self.client.post(f"/api/support/team/payroll/{approved['id']}/pay", json={})
            self.assertEqual(result.status_code, 200)
        self.assertEqual(Transaction.query.filter_by(type='support_payroll').one().amount, -26000)

    def test_telegram_reviewer_cannot_review_other_tenant_or_other_location(self):
        ticket = SimpleNamespace(ownership=self.service, assigned_admin_id=None)
        self.assertTrue(support.can_access_request(self.staff, ticket, 'telegram_reply'))
        self.assertFalse(support.can_access_request(self.staff, ticket, 'telegram_review'))
        ticket.ownership = self.other_service
        self.assertFalse(support.can_access_request(self.staff, ticket, 'telegram_reply'))
