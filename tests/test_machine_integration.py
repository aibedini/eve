import hashlib
import hmac
import json
import os
import secrets
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

_DB = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + (Path(_DB.name) / 'machine.db').as_posix()
os.environ.update(FLASK_ENV='development', DISABLE_BACKGROUND_THREADS='1', EVE_SKIP_IMPORT_MIGRATIONS='1')

from app import app, db  # noqa: E402
from panel.models import (Admin, ClientOperation, ClientOwnership, CustomerAccount, OwnershipClaim, RenewalEvent, Server,
                          ServiceLifecycleState, ServiceOwnership, TelegramIdentity, Transaction)  # noqa: E402
from panel.models.machine import MachineAPIKey, MachineEvent, MachineProposal, MachineServiceIndex  # noqa: E402
from panel.core.redis_client import GLOBAL_SERVER_DATA  # noqa: E402
from panel.models.support import SupportChat  # noqa: E402
from panel.services import machine_events as events, machine_security as security  # noqa: E402
from panel.services.machine_inventory import legacy_identity, rebuild_service_index  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / 'docs/contracts/eve-god-ai-v1-vectors.json'
VECTORS = json.loads(FIXTURE.read_text(encoding='utf-8'))
ORIGIN = 'https://eve-internal.test'


class MachineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.engine.dispose()
        _DB.cleanup()

    def setUp(self):
        self.env = patch.dict(os.environ, {
            'EVE_MACHINE_INTERNAL_HOST': 'eve-internal.test',
            'EVE_MACHINE_PHONE_PEPPER': secrets.token_hex(32),
            'EVE_MACHINE_PAYMENT_PEPPER': secrets.token_hex(32),
            'EVE_MACHINE_SERVICE_KEY_SECRET': secrets.token_hex(32),
            'EVE_GOD_AI_ORIGIN': 'https://god-internal.test',
            'EVE_GOD_AI_WEBHOOK_SECRET': secrets.token_hex(32),
            'EVE_GOD_AI_CA_FILE': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = app.test_client()
        with app.app_context():
            for model in (MachineEvent, MachineProposal, MachineAPIKey, MachineServiceIndex, ClientOwnership, SupportChat, OwnershipClaim,
                          TelegramIdentity, ServiceLifecycleState, RenewalEvent, ClientOperation,
                          Transaction, ServiceOwnership, CustomerAccount, Server, Admin):
                model.query.delete()
            admin = Admin(username='machine-test', password_hash='unused', role='superadmin',
                          enabled=True, is_superadmin=True)
            customer = CustomerAccount(display_name='Private name', primary_phone='989121234567')
            panel = Server(name='Test panel', host='https://example.invalid', username='unused', password='unused')
            db.session.add_all([admin, customer, panel])
            db.session.flush()
            self.customer_id, self.admin_id, self.server_id = customer.id, admin.id, panel.id
            self.identity = str(uuid4())
            self.service_key = security.public_service_key(panel.id, self.identity)
            self.owner_since = security.utcnow() - timedelta(minutes=5)
            db.session.add(ServiceOwnership(customer_id=customer.id, server_id=panel.id,
                                            client_uuid=self.identity, client_email_snapshot='customer-private',
                                            verified_at=self.owner_since))
            for verified in (True, False):
                db.session.add(RenewalEvent(server_id=panel.id, sub_id='never-export-subscription-id',
                                            client_uuid=self.identity, verified=verified, days=31,
                                            volume_bytes=10 * 1024 ** 3))
            standard_scopes = [scope for scope in security.SCOPES if scope != 'context.customer.read_name']
            _, self.token = security.issue_key('integration', standard_scopes, 15, admin.id)
            _, self.with_name = security.issue_key('name reader', ['context.customer.read', 'context.customer.read_name'], 15, admin.id)
            _, self.name_only = security.issue_key('name only', ['context.customer.read_name'], 15, admin.id)
            _, self.restricted = security.issue_key('support', ['context.support.read'], 15, admin.id)
            expired, self.expired = security.issue_key('expired', ['context.customer.read'], 15, admin.id)
            expired.expires_at = security.utcnow() - timedelta(seconds=1)
            db.session.commit()

    def api(self, path, token=None, method='GET', **kwargs):
        headers = {'Authorization': 'Bearer ' + (token or self.token), 'X-Request-ID': str(uuid4())}
        response = self.client.open('/machine/v1' + path, method=method, headers=headers,
                                    base_url=kwargs.pop('base_url', ORIGIN), **kwargs)
        self.assertEqual(response.headers['X-Request-ID'], headers['X-Request-ID'])
        self.assertEqual(response.json['schema_version'], 1)
        self.assertEqual(response.json['source'], 'eve')
        self.assertIn('fetched_at', response.json)
        return response

    def test_security_boundaries(self):
        self.assertEqual(self.api('/health').status_code, 200)
        self.assertEqual(self.api('/health', base_url='https://public.test').status_code, 404)
        self.assertEqual(self.api('/health', base_url='http://eve-internal.test').status_code, 404)
        self.assertEqual(self.api('/health', environ_overrides={'REMOTE_ADDR': '203.0.113.9'}).status_code, 404)
        forged = self.client.get('/machine/v1/health', base_url='http://public.test', headers={
            'Authorization': 'Bearer ' + self.token, 'X-Forwarded-Host': 'eve-internal.test',
            'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1',
        }, environ_overrides={'REMOTE_ADDR': '203.0.113.9'})
        self.assertEqual(forged.status_code, 404)
        self.assertEqual(self.api('/customers', self.expired).status_code, 401)
        self.assertEqual(self.api('/customers', self.restricted).status_code, 403)
        self.assertEqual(self.api('/customers:lookup').status_code, 400)
        self.assertEqual(self.api('/customers:lookup?phone=09121234567').status_code, 400)
        with patch.object(security, 'RATE_LIMIT', 1):
            limited = self.api('/customers', self.restricted)
            self.assertEqual(limited.status_code, 429)
            self.assertGreater(int(limited.headers['Retry-After']), 0)

    def test_machine_routes_expose_only_context_and_proposals(self):
        paths = {
            '/health', '/customers/<int:customer_id>', '/customers:lookup', '/customers/<int:customer_id>/services',
            '/services', '/services/<service_key>', '/services:lookup', '/services/<service_key>/renewals',
            '/customers/<int:customer_id>/payments', '/customers/<int:customer_id>/support-requests',
            '/ownership-claims', '/knowledge/faqs', '/sms/notifications/<service_key>', '/customers', '/proposals',
        }
        routes = {rule.rule.removeprefix('/machine/v1'): rule.methods - {'HEAD'}
                  for rule in app.url_map.iter_rules() if rule.endpoint.startswith('machine.')}
        self.assertEqual(routes, {path: {'POST'} if path == '/proposals' else {'GET'} for path in paths})

    def test_masked_contract_pages_and_proposal_only(self):
        pepper = security.configured_secret('EVE_MACHINE_PHONE_PEPPER')
        digest = security.phone_lookup_hash('0912 123 4567', pepper)
        response = self.api('/customers:lookup?phone_hash=' + digest)
        fields = VECTORS['response_fields']
        envelope = set(fields['envelope'])
        self.assertEqual(set(response.json), envelope | {'items'})
        self.assertEqual(set(response.json['items'][0]), set(fields['customer']) | {'match_basis'})
        self.assertEqual(response.json['items'][0]['customer_id'], self.customer_id)
        self.assertEqual(response.json['items'][0]['match_basis'], 'phone_hash')
        self.assertNotIn('Private name', response.get_data(as_text=True))
        self.assertNotIn('989121234567', response.get_data(as_text=True))
        services = self.api('/customers/%d/services' % self.customer_id).json['items']
        self.assertEqual(set(services[0]), set(fields['service']))
        for field in ('quota', 'lifecycle', 'flags'):
            self.assertEqual(set(services[0][field]), set(fields[field]))
        self.assertEqual(services[0]['service_key'], self.service_key)
        self.assertNotIn(self.identity, str(services))
        self.assertEqual(self.api(f'/services/eve:{self.server_id}:{self.identity}').status_code, 404)
        self.assertNotIn('customer-private', str(services))
        self.assertNotIn('subscription_link', str(services))
        self.assertEqual(self.api('/services:lookup?client_email=customer-private').json['service_key'], self.service_key)
        rows = self.api('/services/' + self.service_key + '/renewals').json['items']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['verified'])
        self.assertEqual(set(rows[0]), set(fields['renewal']))
        for path in (f'/customers/{self.customer_id}', f'/customers/{self.customer_id}/payments',
                     f'/customers/{self.customer_id}/support-requests', '/ownership-claims',
                     '/knowledge/faqs', '/sms/notifications/' + self.service_key, '/customers?limit=1'):
            self.assertEqual(self.api(path).status_code, 200)
        answer = self.api('/proposals', method='POST', json={'kind': 'reminder', 'customer_id': self.customer_id})
        self.assertEqual(answer.status_code, 202)
        self.assertEqual(set(answer.json), envelope | set(fields['proposal']))
        with app.app_context():
            self.assertEqual(db.session.get(MachineProposal, answer.json['proposal_id']).status, 'pending')
            self.assertEqual(db.session.get(CustomerAccount, self.customer_id).display_name, 'Private name')

    def test_admin_key_shown_once_and_revocable(self):
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['admin_id'] = self.admin_id
        path = '/admin/machine-integration'
        created = self.client.post(path, base_url=ORIGIN, data={
            'action': 'issue', 'name': 'human-issued', 'days': '5', 'scopes': 'context.customer.read',
        })
        self.assertEqual(created.status_code, 200)
        import re
        token = re.search(r'eve_mk_[a-f0-9]{32}\.[a-zA-Z0-9_-]{43}', created.get_data(as_text=True)).group()
        self.assertNotIn(token, self.client.get(path, base_url=ORIGIN).get_data(as_text=True))
        key_id, raw = token[7:].split('.', 1)
        with app.app_context():
            self.assertEqual(db.session.get(MachineAPIKey, key_id).key_hash,
                             hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(self.api('/customers', token).status_code, 200)
        self.assertEqual(self.client.post(path, base_url=ORIGIN, data={'action': 'revoke', 'id': key_id}).status_code, 200)
        self.assertEqual(self.api('/customers', token).status_code, 401)

    def test_admin_key_creation_requires_same_origin_in_production(self):
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['admin_id'] = self.admin_id
        path = '/admin/machine-integration'
        data = {'action': 'issue', 'name': 'origin-test', 'days': '5', 'scopes': 'context.customer.read'}
        with patch('app._is_dev_mode', return_value=False):
            rejected = self.client.post(path, base_url=ORIGIN, data=data, headers={'Origin': 'https://other.test'})
            self.assertEqual(rejected.status_code, 403)
            issued = self.client.post(path, base_url=ORIGIN, data=data, headers={'Origin': ORIGIN})
            self.assertEqual(issued.status_code, 200)
            self.assertIn('eve_mk_', issued.get_data(as_text=True))

    def test_outbox_retry_keeps_delivery_id_and_body(self):
        with app.app_context():
            row = MachineEvent.query.first()
            self.assertIsNotNone(row)
            delivery, body = row.id, row.body
            self.assertNotIn('customer-private', body)
            http = MagicMock()
            peer = http.post.return_value.__enter__.return_value
            peer.status_code = 503
            url, secret = events.webhook_config()
            events.deliver_due(http, url, secret, count=1, clock=lambda: 100)
            request = http.post.call_args.kwargs
            self.assertFalse(request['allow_redirects'])
            self.assertIs(request['verify'], True)
            self.assertEqual(request['headers']['X-Eve-Delivery-Id'], delivery)
            row = db.session.get(MachineEvent, delivery)
            self.assertEqual(row.attempts, 1)
            self.assertIsNone(row.delivered_at)
            row.next_attempt_at = security.utcnow() - timedelta(seconds=3)
            db.session.commit()
            peer.status_code = 202
            events.deliver_due(http, url, secret, count=1, clock=lambda: 103)
            second = http.post.call_args.kwargs
            self.assertEqual(second['headers']['X-Eve-Delivery-Id'], delivery)
            self.assertEqual(second['data'], body.encode())
            expected = hmac.digest(secret, f'103.{delivery}.'.encode() + second['data'], 'sha256').hex()
            self.assertEqual(second['headers']['X-Eve-Signature'], 'v1=' + expected)
            self.assertIsNotNone(db.session.get(MachineEvent, delivery).delivered_at)

    def test_shared_public_vectors(self):
        key = bytes.fromhex(VECTORS['public_key_hex'])
        for vector in VECTORS['phone']:
            self.assertEqual(security.normalize_iran_mobile(vector['input']), vector['normalized'])
            self.assertEqual(security.phone_lookup_hash(vector['input'], key), vector['digest'])
        webhook = VECTORS['webhook']
        self.assertEqual(events.signature(key, webhook['timestamp'], webhook['delivery_id'],
                                          webhook['body'].encode()), webhook['signature'])

    def test_event_sources_and_cash_payment_projection(self):
        raw_card = '0000000000000123'
        raw_operation_id = 'client-supplied-09121234567'
        with app.app_context():
            owner = ServiceOwnership.query.filter_by(customer_id=self.customer_id).one()
            telegram = TelegramIdentity(customer_id=self.customer_id, telegram_user_id=12345)
            tx = Transaction(admin_id=self.admin_id, server_id=self.server_id, sender_card=raw_card, amount=1000)
            db.session.add_all([telegram, tx])
            db.session.flush()
            db.session.add_all([
                SupportChat(service_id=owner.id),
                OwnershipClaim(customer_id=self.customer_id, telegram_identity_id=telegram.id,
                               verified_phone='989121234567'),
                ServiceLifecycleState(server_id=self.server_id, client_uuid=self.identity,
                                      service_key=f'eve:{self.server_id}:{self.identity}', generation=1),
                ClientOperation(idempotency_key=raw_operation_id, request_hash='0' * 64, action='renew',
                                admin_id=self.admin_id, transaction_id=tx.id, state='completed',
                                pricing_quote={'package_id': 11, 'package_name': 'Contract plan'}),
                RenewalEvent(server_id=self.server_id, client_uuid=self.identity, sub_id=self.identity,
                             operation_id=raw_operation_id, verified=True),
            ])
            self.assertTrue(events.record_opt_out(self.server_id, self.identity, '', '#nosms private text'))
            db.session.commit()
            tx_id = tx.id
            outbox = [json.loads(row.body) for row in MachineEvent.query.all()]
            self.assertEqual({row['event_type'] for row in outbox}, {
                'eve.service.lifecycle_changed.v1', 'eve.renewal.verified.v1', 'eve.ownership.changed.v1',
                'eve.ownership_claim.created.v1', 'eve.customer.opt_out_changed.v1',
                'eve.support_request.created.v1', 'eve.payment.recorded.v1',
            })
            for private in (self.identity, raw_card, raw_operation_id, '989121234567', 'private text'):
                self.assertNotIn(private, json.dumps(outbox))
            for row in outbox:
                if 'service_key' in row['data']:
                    self.assertEqual(row['data']['service_key'], self.service_key)
            count = MachineEvent.query.count()
            owner.revoked_at = security.utcnow()
            db.session.flush()
            self.assertEqual(MachineEvent.query.count(), count + 1)
            db.session.rollback()
            self.assertEqual(MachineEvent.query.count(), count)
            emitted_payment = next(row['data'] for row in outbox if row['event_type'] == 'eve.payment.recorded.v1')
        payment, = self.api(f'/customers/{self.customer_id}/payments').json['items']
        self.assertEqual(set(payment), set(VECTORS['response_fields']['payment']))
        self.assertEqual(payment['payment_id'], -tx_id)
        self.assertEqual(payment['masked_card'], '••••0123')
        self.assertEqual(payment['sender_card_hash'], emitted_payment['sender_card_hash'])
        service = self.api('/services/' + self.service_key).json
        self.assertEqual(service['package'], {'id': 11, 'name': 'Contract plan'})
        support, = self.api(f'/customers/{self.customer_id}/support-requests').json['items']
        self.assertEqual(set(support), set(VECTORS['response_fields']['support_request']))
        claim, = self.api('/ownership-claims').json['items']
        self.assertEqual(set(claim), set(VECTORS['response_fields']['ownership_claim']))

    def test_phone_changes_replace_the_old_lookup(self):
        pepper = security.configured_secret('EVE_MACHINE_PHONE_PEPPER')
        old = security.phone_lookup_hash('09121234567', pepper)
        new_phone = '09121234568'
        new = security.phone_lookup_hash(new_phone, pepper)
        with app.app_context():
            db.session.get(CustomerAccount, self.customer_id).primary_phone = new_phone
            db.session.commit()
        self.assertEqual(self.api('/customers:lookup?phone_hash=' + old).json['items'], [])
        self.assertEqual(self.api('/customers:lookup?phone_hash=' + new).json['items'][0]['customer_id'], self.customer_id)

    def test_name_scope_and_batched_phone_hashes(self):
        pepper = security.configured_secret('EVE_MACHINE_PHONE_PEPPER')
        digest = security.phone_lookup_hash('09121234567', pepper)
        detail = f'/customers/{self.customer_id}'
        self.assertEqual(self.api(detail, self.name_only).status_code, 403)
        self.assertEqual(self.api(detail).json['display_name'], '••••')
        customer = self.api(detail, self.with_name).json
        self.assertEqual(customer['display_name'], 'Private name')
        self.assertEqual(customer['primary_phone_masked'], '••••4567')
        self.assertEqual(self.api('/customers', self.with_name).json['items'][0]['display_name'], 'Private name')

        query = [('phone_hash', digest), ('phone_hash', '0' * 64), ('phone_hash', digest)]
        query.extend(('phone_hash', f'{index:064x}') for index in range(1, 98))
        phone_lookup = '/customers:lookup'
        result = self.api(phone_lookup, self.with_name, query_string=query)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json['items']), 100)
        first, missing, duplicate = result.json['items'][:3]
        self.assertEqual(first['items'][0]['display_name'], 'Private name')
        self.assertEqual(first['items'][0]['primary_phone_masked'], '••••4567')
        self.assertEqual(missing, {'index': 1, 'items': []})
        self.assertEqual(duplicate, {'index': 2, 'items': first['items']})
        self.assertEqual(self.api(phone_lookup, query_string=query).json['items'][0]['items'][0]['display_name'], '••••')
        self.assertEqual(self.api(phone_lookup, query_string=query + query[:1]).status_code, 400)
        self.assertEqual(self.api(phone_lookup, query_string=query[:1] + [('phone_hash', 'bad')]).status_code, 400)

    def test_service_lookup_distinguishes_ambiguity_in_single_and_batch(self):
        with app.app_context():
            for email in ('ambiguous', 'AMBIGUOUS'):
                db.session.add(ServiceOwnership(customer_id=self.customer_id, server_id=self.server_id,
                                                client_uuid=str(uuid4()), client_email_snapshot=email))
            db.session.commit()
        lookup = '/services:lookup'
        self.assertEqual(self.api(lookup + '?client_email=unknown').status_code, 404)
        self.assertEqual(self.api(lookup + '?client_email=Ambiguous').status_code, 409)
        query = [('client_email', email) for email in ['CUSTOMER-PRIVATE', 'unknown', 'ambiguous', 'customer-private']]
        query.extend(('client_email', f'missing-{index}') for index in range(96))
        response = self.api(lookup, query_string=query)
        self.assertEqual(response.status_code, 200)
        rows = response.json['items']
        self.assertEqual(len(rows), 100)
        self.assertEqual([row['status'] for row in rows[:4]], [200, 404, 409, 200])
        self.assertEqual(rows[0]['item']['service_key'], self.service_key)
        self.assertEqual(rows[1], {'index': 1, 'status': 404})
        self.assertEqual(rows[2], {'index': 2, 'status': 409})
        self.assertEqual(rows[3]['item'], rows[0]['item'])
        self.assertEqual(self.api(lookup, query_string=query + query[:1]).status_code, 400)
        self.assertEqual(self.api(lookup, query_string=query[:1] + [('client_email', '')]).status_code, 400)

    def test_outbox_uses_configured_ca_and_retries_if_unavailable(self):
        with app.app_context():
            row = MachineEvent.query.first()
            delivery = row.id
            url, secret = events.webhook_config()
            http = MagicMock()
            http.post.side_effect = OSError('unavailable CA bundle')
            with patch.dict(os.environ, EVE_GOD_AI_CA_FILE='/internal/god-ai-ca.pem'):
                events.deliver_due(http, url, secret, count=1)
            self.assertEqual(http.post.call_args.kwargs['verify'], '/internal/god-ai-ca.pem')
            row = db.session.get(MachineEvent, delivery)
            self.assertEqual(row.attempts, 1)
            self.assertIsNone(row.delivered_at)
            self.assertGreater(row.next_attempt_at, security.utcnow())

    def test_full_inventory_paging_contact_lookup_and_legacy_identity(self):
        legacy_uuid, unowned_uuid = str(uuid4()), str(uuid4())
        with app.app_context():
            legacy = ClientOwnership(reseller_id=self.admin_id, server_id=self.server_id,
                                     inbound_id=1, client_email='legacy-service')
            db.session.add(legacy)
            db.session.add(ClientOwnership(reseller_id=self.admin_id, server_id=self.server_id,
                                           client_uuid=self.identity, client_email='customer-private'))
            db.session.commit()
            old_key = security.public_service_key(self.server_id, legacy_identity(legacy.id))
        inbounds = [{'server_id': self.server_id, 'id': 1, 'clients': [
            {'id': self.identity, 'email': 'customer-private', 'comment': '09121234567'},
            {'id': legacy_uuid, 'email': 'legacy-service', 'comment': '۰۹۱۲۱۲۳۴۵۶۷ #nosms'},
            {'id': unowned_uuid, 'email': 'user_09121234567', 'comment': '09121234568'},
        ]}]
        statuses = [{'server_id': self.server_id, 'success': True}]
        with patch.dict(GLOBAL_SERVER_DATA, inbounds=inbounds, servers_status=statuses):
            with app.app_context():
                pepper = security.configured_secret('EVE_MACHINE_PHONE_PEPPER')
                rebuild_service_index(pepper)
                self.assertEqual(MachineServiceIndex.query.count(), 3)
            expected = {self.service_key: self.customer_id,
                        security.public_service_key(self.server_id, legacy_uuid): None,
                        security.public_service_key(self.server_id, unowned_uuid): None}
            cursor = None
            collected = []
            for _ in expected:
                page = self.api('/services', query_string={'limit': 1, 'cursor': cursor or ''}).json
                collected.extend(page['items'])
                cursor = page['next_cursor']
            self.assertIsNone(cursor)
            self.assertEqual({row['service_key']: row['customer_id'] for row in collected}, expected)
            self.assertEqual(self.api('/services?updated_since=9999-01-01T00:00:00Z').json['items'], [])
            self.assertEqual(self.api('/services?cursor=bad').status_code, 400)
            self.assertEqual(self.api('/services', self.restricted).status_code, 403)

            lookup = '/services:lookup'
            legacy_view = self.api(lookup + '?client_email=LEGACY-SERVICE').json
            self.assertIsNone(legacy_view['customer_id'])
            self.assertEqual(legacy_view['reseller_id'], self.admin_id)
            self.assertEqual(legacy_view['client_email'], '••••')
            self.assertTrue(legacy_view['flags']['nosms'])
            self.assertEqual(self.api('/services/' + old_key).json['service_key'], legacy_view['service_key'])
            digest = security.phone_lookup_hash('09121234567', pepper)
            result = self.api(lookup, query_string={'phone_hash': digest})
            self.assertEqual({row['service_key'] for row in result.json['items']}, set(expected))
            self.assertNotIn('09121234567', result.get_data(as_text=True))
            self.assertNotIn(legacy_uuid, result.get_data(as_text=True))
            other = security.phone_lookup_hash('09121234568', pepper)
            self.assertEqual(self.api(lookup, query_string={'phone_hash': other}).json['items'], [])
            self.assertEqual(self.api(lookup, query_string={'phone_hash': digest, 'client_email': 'legacy-service'}).status_code, 400)
            with patch.dict(os.environ, EVE_MACHINE_PHONE_PEPPER=secrets.token_hex(32)):
                self.assertEqual(self.api(lookup, query_string={'phone_hash': digest}).json['items'], [])

            with app.app_context():
                revisions = {row.client_uuid: row.updated_at for row in MachineServiceIndex.query.all()}
                rebuild_service_index(pepper)
                self.assertEqual({row.client_uuid: row.updated_at for row in MachineServiceIndex.query.all()}, revisions)
                owner = ServiceOwnership.query.filter_by(client_uuid=self.identity).one()
                owner.revoked_at = security.utcnow()
                db.session.commit()
            self.assertIsNone(self.api('/services/' + self.service_key).json['customer_id'])

        with patch.dict(GLOBAL_SERVER_DATA, inbounds=[], servers_status=[], _snapshot_complete=False):
            with app.app_context():
                rebuild_service_index(pepper)
            retained = self.api(lookup, query_string={'phone_hash': digest}).json['items']
            self.assertEqual({row['service_key'] for row in retained}, set(expected))

    def test_index_exposes_legacy_service_without_snapshot_and_replaces_contact(self):
        with app.app_context():
            legacy = ClientOwnership(reseller_id=self.admin_id, server_id=self.server_id,
                                     client_email='acct_09121234567')
            db.session.add(legacy)
            db.session.commit()
            pepper = security.configured_secret('EVE_MACHINE_PHONE_PEPPER')
            rebuild_service_index(pepper)
            key = security.public_service_key(self.server_id, legacy_identity(legacy.id))
        lookup = '/services:lookup'
        before = security.phone_lookup_hash('09121234567', pepper)
        after = security.phone_lookup_hash('09121234568', pepper)
        self.assertEqual(self.api(lookup, query_string={'phone_hash': before}).json['items'][0]['service_key'], key)
        with app.app_context():
            legacy = ClientOwnership.query.one()
            legacy.client_email = 'acct_09121234568'
            db.session.commit()
            rebuild_service_index(pepper)
        self.assertEqual(self.api(lookup, query_string={'phone_hash': before}).json['items'], [])
        self.assertEqual(self.api(lookup, query_string={'phone_hash': after}).json['items'][0]['service_key'], key)

    def test_migration_upgrades_an_existing_sqlite_schema(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        scripts = ScriptDirectory.from_config(Config(str(FIXTURE.parents[2] / 'alembic.ini')))
        migration = scripts.get_revision('45170a8ab431').module
        inventory_migration = scripts.get_revision('26713b0b96e2').module
        new_tables = {'machine_api_keys', 'machine_events', 'machine_phone_lookups', 'machine_proposals',
                      'machine_service_index'}
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            db.metadata.create_all(connection, tables=[table for table in db.metadata.sorted_tables
                                                       if table.name not in new_tables])
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
                inventory_migration.upgrade()
                inventory_migration.upgrade()
            self.assertLessEqual(new_tables, set(inspect(connection).get_table_names()))
            self.assertIn('ix_machine_customers_cursor',
                          {index['name'] for index in inspect(connection).get_indexes('customer_accounts')})
            self.assertIn('ix_machine_service_index_phone_hash',
                          {index['name'] for index in inspect(connection).get_indexes('machine_service_index')})
        engine.dispose()
