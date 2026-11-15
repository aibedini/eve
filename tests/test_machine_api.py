"""XR-GA-009: read-only machine API (EVE Context API v1).

Covers the contract's negative cases (401/403/400), the masked projections, the
``generation``/``fetched_at`` service contract, the enabled-only knowledge rows,
the pending-only ownership claims and the documented event signature formula.

No network: the event transport is injected. No real sleeps.
"""
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['MACHINE_PHONE_PEPPER'] = 'unit-test-phone-pepper-0123456789abcdef'
os.environ['GOD_AI_EVENTS_SECRET'] = 'unit-test-events-secret-0123456789abcdef'

from app import Admin, app, db  # noqa: E402
from panel.core.redis_client import GLOBAL_SERVER_DATA  # noqa: E402
from panel.models import (  # noqa: E402
    BankCard,
    CustomerAccount,
    FAQ,
    MachineApiKey,
    NotificationTemplate,
    OnlineChatScript,
    OwnershipClaim,
    OwnershipClaimItem,
    Package,
    Payment,
    RenewalEvent,
    RenewTemplate,
    Server,
    ServiceLifecycleState,
    ServiceNotificationEvent,
    ServiceNotificationOutbox,
    ServiceObservedState,
    ServiceOwnership,
    SmsSendLog,
    TelegramBotInstance,
    TelegramIdentity,
    TelegramServiceRequest,
    Transaction,
)
from panel.services import machine_api, machine_events  # noqa: E402

GB = 1024 ** 3
AUTH_HEADER = 'Authorization'


def _utcnow():
    return datetime.utcnow()


def _iso_ms(moment):
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)


class MachineApiTestBase(unittest.TestCase):
    """Isolated scratch DB + one seeded customer/service used by every case."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls._clear()
        db.session.remove()
        cls.ctx.pop()

    @classmethod
    def _clear(cls):
        # Dependents first. Every delete is best-effort so a suite that dropped a
        # table earlier in the same process cannot break this file.
        models = (
            MachineApiKey, OwnershipClaimItem, OwnershipClaim,
            TelegramServiceRequest, TelegramBotInstance, TelegramIdentity,
            ServiceNotificationEvent, ServiceNotificationOutbox, SmsSendLog,
            ServiceObservedState, ServiceLifecycleState, ServiceOwnership,
            Payment, Transaction, BankCard, RenewalEvent,
            FAQ, RenewTemplate, NotificationTemplate, OnlineChatScript,
            CustomerAccount, Package, Admin, Server,
        )
        for model in models:
            try:
                model.query.delete()
            except Exception:
                db.session.rollback()
        db.session.commit()
        # Drop the identity map with the rows: SQLite reuses primary keys after a
        # bulk delete, and a stale MachineApiKey object would collide with the
        # next test's freshly created key (same issue as tests/conftest.py).
        db.session.remove()

    def setUp(self):
        type(self)._clear()
        GLOBAL_SERVER_DATA.update({
            'inbounds': [], 'stats': {}, 'servers_status': [],
            'last_update': None, 'normalized_server_ids': set(),
            'normalized_indexes': {},
        })

        self.server = Server(name='DE-3', host='10.0.0.1', username='root',
                             password='not-a-secret', enabled=True)
        self.admin = Admin(username='machine-admin', role='admin', enabled=True)
        self.admin.set_password('CorrectHorseBattery1!')
        db.session.add_all([self.server, self.admin])
        db.session.commit()

        self.email = 'g276-09195292411@example.test'
        self.phone = '+989195292411'
        self.customer = CustomerAccount(
            display_name='Ali Rezaei', primary_phone='989195292411',
            phone_verified_at=_utcnow(), status='active', risk_level='standard',
            preferred_language='fa', credit=12000,
        )
        db.session.add(self.customer)
        db.session.commit()

        self.service_key = f'eve:{self.server.id}:client-uuid-1'
        self.client_uuid = 'client-uuid-1'
        self.ownership = ServiceOwnership(
            customer_id=self.customer.id, server_id=self.server.id,
            client_uuid=self.client_uuid, client_email_snapshot=self.email,
            verification_method='admin', verified_at=_utcnow(),
        )
        self.lifecycle = ServiceLifecycleState(
            service_key=self.service_key, server_id=self.server.id,
            client_uuid=self.client_uuid, client_email=self.email,
            generation=37, last_lifecycle_change_at=_utcnow(),
            last_renewed_at=_utcnow() - timedelta(days=10),
        )
        self.observed = ServiceObservedState(
            service_key=self.service_key, server_id=self.server.id,
            client_uuid=self.client_uuid, client_email=self.email,
            last_state='active', last_remaining_bytes=int(8.8 * GB),
            last_total_bytes=50 * GB, last_observed_at=_utcnow(),
            state_version=3,
        )
        db.session.add_all([self.ownership, self.lifecycle, self.observed])
        db.session.commit()

        self.expiry = _utcnow() + timedelta(days=6.6)
        self._set_live_client({
            'id': self.client_uuid,
            'email': self.email,
            'enable': True,
            'totalGB': 50 * GB,
            'up': 20 * GB,
            'down': int(21.2 * GB),
            'remaining_bytes': int(8.8 * GB),
            'expiryTimestamp': _iso_ms(self.expiry),
            'service_state': 'active',
            'comment': '#nopm',
            'raw_client': {'comment': '#nopm'},
        })

        self.key, self.token = machine_api.create_machine_api_key(
            'test-all-scopes', sorted(machine_api.KNOWN_SCOPES),
            created_by='unit-test',
        )

        self.identity = TelegramIdentity(
            customer_id=self.customer.id, telegram_user_id=1001,
            telegram_chat_id=1001, phone_normalized='989121111111',
            phone_verified_at=_utcnow(),
        )
        db.session.add(self.identity)
        db.session.commit()

    def _set_live_client(self, client):
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server.id,
            'id': 1,
            'clients': [client],
        }]
        GLOBAL_SERVER_DATA['servers_status'] = [{
            'server_id': self.server.id, 'success': True, 'name': 'DE-3',
        }]

    def auth(self, token=None):
        return {AUTH_HEADER: 'Bearer %s' % (token or self.token)}

    def get(self, path, *, token=None, headers=None, **kwargs):
        merged = dict(self.auth(token))
        merged.update(headers or {})
        return self.client.get(path, headers=merged, **kwargs)

    def _walk_keys(self, value):
        if isinstance(value, dict):
            for key, nested in value.items():
                yield str(key)
                yield from self._walk_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from self._walk_keys(nested)


class MachineAuthTests(MachineApiTestBase):
    def test_health_is_public_and_versioned(self):
        response = self.client.get('/machine/v1/health')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertEqual(body['source'], 'eve')
        self.assertEqual(body['schema_version'], machine_api.SCHEMA_HEALTH)
        self.assertTrue(body['fetched_at'].endswith('Z'))
        self.assertTrue(response.headers.get('X-Request-ID'))

    def test_missing_bearer_token_is_401(self):
        response = self.client.get('/machine/v1/customers/%d' % self.customer.id)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'unauthorized')

    def test_malformed_bearer_token_is_401(self):
        response = self.get('/machine/v1/customers/%d' % self.customer.id,
                            token='eve_mk_not-a-token')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'invalid_key')

    def test_unknown_key_is_401(self):
        _row, token = machine_api.create_machine_api_key(
            'other', [machine_api.SCOPE_CUSTOMER_READ])
        response = self.get('/machine/v1/customers/%d' % self.customer.id,
                            token=token + 'x')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'invalid_key')

    def test_expired_key_is_401(self):
        _row, token = machine_api.create_machine_api_key(
            'expired', [machine_api.SCOPE_CUSTOMER_READ],
            expires_at=_utcnow() - timedelta(minutes=1))
        response = self.get('/machine/v1/customers/%d' % self.customer.id,
                            token=token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'expired_key')

    def test_revoked_key_is_401(self):
        row, token = machine_api.create_machine_api_key(
            'revoked', [machine_api.SCOPE_CUSTOMER_READ])
        row.revoked_at = _utcnow()
        db.session.commit()
        response = self.get('/machine/v1/customers/%d' % self.customer.id,
                            token=token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'revoked_key')

    def test_key_without_required_scope_is_403(self):
        _row, token = machine_api.create_machine_api_key(
            'customer-only', [machine_api.SCOPE_CUSTOMER_READ])
        response = self.get('/machine/v1/services/%s' % self.service_key,
                            token=token)
        self.assertEqual(response.status_code, 403)
        body = response.get_json()
        self.assertEqual(body['error'], 'insufficient_scope')
        self.assertEqual(body['required_scope'], machine_api.SCOPE_SERVICE_READ)

    def test_request_id_is_echoed_in_header_and_body(self):
        response = self.get('/machine/v1/customers/%d' % self.customer.id,
                            headers={'X-Request-ID': 'req-abc-123'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('X-Request-ID'), 'req-abc-123')
        self.assertEqual(response.get_json()['request_id'], 'req-abc-123')

    def test_missing_request_id_is_generated(self):
        response = self.get('/machine/v1/customers/%d' % self.customer.id)
        request_id = response.headers.get('X-Request-ID')
        self.assertTrue(request_id)
        self.assertEqual(response.get_json()['request_id'], request_id)

    def test_error_responses_also_echo_the_request_id(self):
        response = self.client.get(
            '/machine/v1/customers/1', headers={'X-Request-ID': 'req-unauth'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get('X-Request-ID'), 'req-unauth')
        self.assertEqual(response.get_json()['request_id'], 'req-unauth')
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')

    def test_last_used_at_is_the_only_write(self):
        before = {
            'name': self.key.name, 'key_hash': self.key.key_hash,
            'scopes': self.key.scopes, 'created_at': self.key.created_at,
        }
        self.assertIsNone(db.session.get(MachineApiKey, self.key.id).last_used_at)
        response = self.get('/machine/v1/customers/%d' % self.customer.id)
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        refreshed = db.session.get(MachineApiKey, self.key.id)
        self.assertIsNotNone(refreshed.last_used_at)
        self.assertEqual(refreshed.name, before['name'])
        self.assertEqual(refreshed.key_hash, before['key_hash'])
        self.assertEqual(refreshed.scopes, before['scopes'])
        self.assertEqual(refreshed.created_at, before['created_at'])
        self.assertEqual(MachineApiKey.query.count(), 1)

    def test_unknown_customer_is_404(self):
        response = self.get('/machine/v1/customers/999999')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['error'], 'not_found')

    def test_feature_flag_disables_the_blueprint(self):
        os.environ['EVE_MACHINE_API_ENABLED'] = 'false'
        try:
            response = self.client.get('/machine/v1/health')
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json()['error'], 'not_found')
        finally:
            os.environ.pop('EVE_MACHINE_API_ENABLED', None)


class MachinePhoneLookupTests(MachineApiTestBase):
    def test_missing_phone_hash_is_400(self):
        response = self.get('/machine/v1/customers:lookup')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'phone_hash_required')

    def test_raw_phone_number_is_not_accepted(self):
        response = self.get('/machine/v1/customers:lookup?phone=09195292411')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'phone_hash_required')

    def test_invalid_phone_hash_shape_is_400(self):
        response = self.get('/machine/v1/customers:lookup?phone_hash=not-a-hash')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_phone_hash')

    def test_phone_hash_returns_the_masked_customer(self):
        phone_hash = machine_api.phone_lookup_hash('09195292411')
        response = self.get('/machine/v1/customers:lookup?phone_hash=' + phone_hash)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['count'], 1)
        match = body['customers'][0]
        self.assertEqual(match['customer_id'], self.customer.id)
        self.assertEqual(match['match_basis'], 'customer_phone')
        self.assertEqual(match['display_name'], 'A***i')
        self.assertIn('***', match['phone'])
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn('989195292411', serialized)
        self.assertNotIn('Ali Rezaei', serialized)

    def test_phone_hash_can_return_several_customers(self):
        other = CustomerAccount(display_name='Sara Ahmadi',
                                primary_phone='989351111111', status='active')
        db.session.add(other)
        db.session.commit()
        db.session.add(TelegramIdentity(
            customer_id=other.id, telegram_user_id=2002,
            phone_normalized='989195292411', phone_verified_at=_utcnow(),
        ))
        db.session.commit()
        phone_hash = machine_api.phone_lookup_hash('+98 919 529 2411')
        response = self.get('/machine/v1/customers:lookup?phone_hash=' + phone_hash)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['count'], 2)
        bases = {row['customer_id']: row['match_basis'] for row in body['customers']}
        self.assertEqual(bases[self.customer.id], 'customer_phone')
        self.assertEqual(bases[other.id], 'telegram_verified_phone')

    def test_unmatched_phone_hash_returns_zero_rows(self):
        phone_hash = hmac.new(b'unit-test-phone-pepper-0123456789abcdef',
                              b'989000000000', hashlib.sha256).hexdigest()
        response = self.get('/machine/v1/customers:lookup?phone_hash=' + phone_hash)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['count'], 0)


class MachineServiceProjectionTests(MachineApiTestBase):
    def test_service_projection_has_generation_fetched_at_and_masks(self):
        response = self.get('/machine/v1/services/%s' % self.service_key)
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        self.assertEqual(body['schema_version'], 'eve.context.service.v1')
        self.assertEqual(body['source'], 'eve')
        self.assertTrue(body['fetched_at'].endswith('Z'))
        self.assertEqual(body['service_key'], self.service_key)
        self.assertEqual(body['customer_id'], self.customer.id)
        self.assertEqual(body['status'], 'active')
        self.assertEqual(body['lifecycle']['generation'], 37)
        self.assertEqual(body['lifecycle']['state'], 'active')
        self.assertEqual(body['quota']['total_gb'], 50.0)
        self.assertEqual(body['quota']['used_gb'], 41.2)
        self.assertEqual(body['quota']['remaining_gb'], 8.8)
        self.assertFalse(body['quota']['unlimited'])
        self.assertAlmostEqual(body['days_left'], 6.6, delta=0.1)
        self.assertFalse(body['flags']['nosms'])
        self.assertTrue(body['flags']['nopm'])
        self.assertEqual(body['server']['id'], self.server.id)
        self.assertEqual(body['server']['label'], 'DE-3')
        self.assertEqual(body['server']['status'], 'online')
        self.assertEqual(body['owner_type'], 'direct')
        self.assertIsNone(body['reseller_id'])
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn('subscription_link', serialized)
        self.assertNotIn(self.email, serialized)
        self.assertNotIn('989195292411', serialized)
        self.assertEqual(body['client_email'], 'g276***11')

    def test_service_projection_includes_the_package_from_the_last_transaction(self):
        db.session.add(Package(name='30d-50GB', days=30, volume=50, price=100000))
        db.session.commit()
        db.session.add(Transaction(
            admin_id=self.admin.id, server_id=self.server.id,
            client_email=self.email, amount=100000, type='renew',
            category='income', package_name='30d-50GB', volume_gb=50, days=30,
        ))
        db.session.commit()
        body = self.get('/machine/v1/services/%s' % self.service_key).get_json()
        self.assertEqual(body['package']['name'], '30d-50GB')
        self.assertIsNotNone(body['package']['id'])
        self.assertIsNone(body['last_verified_renewal_at'])

    def test_service_projection_includes_the_last_verified_renewal(self):
        renewed_at = _utcnow() - timedelta(days=10)
        db.session.add_all([
            RenewalEvent(server_id=self.server.id, sub_id=self.email,
                         client_uuid=self.client_uuid, event_type='renewal',
                         source='explicit_renew', renewed_at=renewed_at,
                         verified=True, verified_at=renewed_at,
                         operation_id='op-1'),
            RenewalEvent(server_id=self.server.id, sub_id=self.email,
                         client_uuid=self.client_uuid, event_type='inferred_reset',
                         source='counter_reset', renewed_at=_utcnow(),
                         verified=False),
        ])
        db.session.commit()
        body = self.get('/machine/v1/services/%s' % self.service_key).get_json()
        self.assertTrue(body['last_verified_renewal_at'].startswith(
            renewed_at.replace(microsecond=0).isoformat()[:19]))

    def test_service_projection_falls_back_to_the_observed_state(self):
        # A cold web worker has no local snapshot yet; the durable observed-state
        # ledger must still answer instead of returning an empty/404 service.
        GLOBAL_SERVER_DATA['inbounds'] = []
        GLOBAL_SERVER_DATA['servers_status'] = []
        self.observed.last_expiry_ms = _iso_ms(self.expiry)
        db.session.commit()
        response = self.get('/machine/v1/services/%s' % self.service_key)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['status'], 'active')
        self.assertEqual(body['quota']['total_gb'], 50.0)
        self.assertEqual(body['quota']['remaining_gb'], 8.8)
        self.assertAlmostEqual(body['days_left'], 6.6, delta=0.1)
        self.assertEqual(body['server']['status'], 'unknown')

    def test_unknown_service_is_404_and_malformed_is_400(self):
        self.assertEqual(
            self.get('/machine/v1/services/eve:%d:missing' % self.server.id).status_code,
            404)
        malformed = self.get('/machine/v1/services/not-a-service-key')
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.get_json()['error'], 'invalid_service_key')

    def test_customer_services_list_is_scoped_to_the_owner(self):
        response = self.get('/machine/v1/customers/%d/services' % self.customer.id)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['services'][0]['service_key'], self.service_key)
        self.assertEqual(
            self.get('/machine/v1/customers/999999/services').status_code, 404)

    def test_service_lookup_by_client_email(self):
        response = self.get(
            '/machine/v1/services:lookup?client_email=' + self.email)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['services'][0]['service_key'], self.service_key)
        self.assertNotIn(self.email, json.dumps(body))
        self.assertEqual(
            self.get('/machine/v1/services:lookup').status_code, 400)

    def test_customer_projection_masks_name_and_phone(self):
        body = self.get('/machine/v1/customers/%d' % self.customer.id).get_json()
        self.assertEqual(body['customer_id'], self.customer.id)
        self.assertEqual(body['display_name'], 'A***i')
        self.assertEqual(body['phone'], '9891***411')
        self.assertTrue(body['phone_verified'])
        self.assertNotIn('subscription_link', json.dumps(body))

    def test_renewals_endpoint_returns_only_verified_rows(self):
        verified = RenewalEvent(
            server_id=self.server.id, sub_id=self.email,
            client_uuid=self.client_uuid, event_type='renewal',
            source='explicit_renew', renewed_at=_utcnow() - timedelta(days=10),
            verified=True, verified_at=_utcnow(), operation_id='op-2',
            days=30, volume_bytes=50 * GB,
        )
        unverified = RenewalEvent(
            server_id=self.server.id, sub_id=self.email,
            client_uuid=self.client_uuid, event_type='inferred_reset',
            source='counter_reset', renewed_at=_utcnow(), verified=False,
        )
        db.session.add_all([verified, unverified])
        db.session.commit()
        response = self.get(
            '/machine/v1/services/%s/renewals?limit=5' % self.service_key)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['schema_version'], machine_api.SCHEMA_RENEWALS)
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['renewals'][0]['id'], verified.id)
        self.assertTrue(body['renewals'][0]['verified'])
        self.assertNotIn('client_email_snapshot', body['renewals'][0])


class MachineFinanceSupportKnowledgeTests(MachineApiTestBase):
    def _payment_fixture(self):
        card = BankCard(label='Main gate', bank_name='Melli',
                        card_number='6037991234567890', is_active=True)
        db.session.add(card)
        db.session.commit()
        payment = Payment(
            admin_id=self.admin.id, card_id=card.id,
            sender_card='6037991111223344', sender_name='Ali Rezaei',
            amount=250000, payment_date=_utcnow() - timedelta(days=2),
            client_email=self.email, description='card 6037991111223344',
            verified=True,
        )
        db.session.add(payment)
        db.session.commit()
        return card, payment

    def test_payments_are_masked_and_contain_no_pan_or_token(self):
        _card, payment = self._payment_fixture()
        response = self.get(
            '/machine/v1/customers/%d/payments' % self.customer.id)
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        self.assertEqual(body['schema_version'], machine_api.SCHEMA_PAYMENTS)
        self.assertEqual(body['count'], 1)
        row = body['payments'][0]
        self.assertEqual(row['id'], payment.id)
        self.assertIn('***', row['sender_card'])
        self.assertNotEqual(row['sender_card'], '6037991111223344')
        self.assertIn('***', row['destination_card']['masked_card'])
        serialized = json.dumps(body)
        self.assertNotIn('6037991111223344', serialized)
        self.assertNotIn('6037991234567890', serialized)
        self.assertNotIn('token', set(key.lower() for key in self._walk_keys(body)))
        self.assertNotIn('description', row)

    def test_payments_since_filter_is_validated(self):
        self._payment_fixture()
        self.assertEqual(
            self.get('/machine/v1/customers/%d/payments?since=not-a-date'
                     % self.customer.id).status_code, 400)
        response = self.get(
            '/machine/v1/customers/%d/payments?since=2020-01-01T00:00:00Z'
            % self.customer.id)
        self.assertEqual(response.status_code, 200)

    def test_support_requests_carry_no_note_or_receipt(self):
        bot = TelegramBotInstance(scope_key='machine-test', owner_type='system',
                                  display_name='Eve', enabled=False)
        db.session.add(bot)
        db.session.commit()
        request = TelegramServiceRequest(
            bot_instance_id=bot.id, telegram_user_id=1001,
            customer_id=self.customer.id,
            service_ownership_id=self.ownership.id, request_type='renewal',
            status='pending', note='card 6037991111223344',
            receipt_file_id='telegram-file-id', amount=100000,
        )
        db.session.add(request)
        db.session.commit()
        response = self.get(
            '/machine/v1/customers/%d/support-requests' % self.customer.id)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['count'], 1)
        row = body['requests'][0]
        self.assertEqual(row['service_key'], self.service_key)
        self.assertEqual(row['channel'], 'telegram')
        serialized = json.dumps(body)
        self.assertNotIn('telegram-file-id', serialized)
        self.assertNotIn('6037991111223344', serialized)
        self.assertNotIn('note', row)
        self.assertNotIn('receipt_file_id', row)

    def _claim(self, status):
        claim = OwnershipClaim(
            customer_id=self.customer.id,
            telegram_identity_id=self.identity.id,
            status=status, claim_method='admin_review',
            verified_phone='989195292411',
        )
        db.session.add(claim)
        db.session.commit()
        db.session.add(OwnershipClaimItem(
            claim_id=claim.id, server_id=self.server.id,
            client_uuid='claim-uuid-%s' % status,
            client_email_snapshot=self.email, match_reason='phone_match',
            match_score=95, status='pending' if status == 'pending' else 'approved',
        ))
        db.session.commit()
        return claim

    def test_ownership_claims_returns_only_pending(self):
        pending = self._claim('pending')
        approved = self._claim('approved')
        response = self.get('/machine/v1/ownership-claims?status=pending')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['claims'][0]['id'], pending.id)
        self.assertNotEqual(body['claims'][0]['id'], approved.id)
        self.assertEqual(body['claims'][0]['items'][0]['match_reason'], 'phone_match')
        self.assertIn('***', body['claims'][0]['verified_phone'])
        self.assertEqual(
            self.get('/machine/v1/ownership-claims?status=approved').status_code,
            400)

    def test_knowledge_returns_only_enabled_public_rows(self):
        enabled_faq = FAQ(title='How to renew', content='<p>renew</p>',
                          is_enabled=True)
        disabled_faq = FAQ(title='Hidden', content='<p>no</p>', is_enabled=False)
        active_renew = RenewTemplate(name='Renew', content='renew {name}',
                                     is_active=True)
        inactive_renew = RenewTemplate(name='Hidden renew', content='x',
                                       is_active=False)
        active_global = NotificationTemplate(name='Global notice', content='hi',
                                             type='client_created', is_active=True)
        active_reseller = NotificationTemplate(name='Reseller notice', content='hi',
                                               type='client_created',
                                               is_active=True, owner_id=self.admin.id)
        inactive_notice = NotificationTemplate(name='Inactive', content='hi',
                                               is_active=False)
        active_script = OnlineChatScript(name='Greeting', script_code='hello',
                                        is_active=True)
        inactive_script = OnlineChatScript(name='Hidden greeting', script_code='x',
                                           is_active=False)
        db.session.add_all([
            enabled_faq, disabled_faq, active_renew, inactive_renew,
            active_global, active_reseller, inactive_notice, active_script,
            inactive_script,
        ])
        db.session.commit()
        response = self.get('/machine/v1/knowledge/faqs')
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        self.assertEqual(body['schema_version'], machine_api.SCHEMA_KNOWLEDGE)
        self.assertEqual([row['id'] for row in body['faqs']], [enabled_faq.id])
        self.assertEqual([row['id'] for row in body['renew_templates']],
                         [active_renew.id])
        self.assertEqual([row['id'] for row in body['notification_templates']],
                         [active_global.id])
        self.assertEqual([row['id'] for row in body['online_chat_scripts']],
                         [active_script.id])

    def test_sms_notifications_are_masked(self):
        db.session.add(ServiceNotificationEvent(
            event_id='evt-notify-1', service_key=self.service_key,
            server_id=self.server.id, client_uuid=self.client_uuid,
            client_email=self.email, state='volume_low',
            previous_state='active', notification_kind='low_volume',
            state_version=4, lifecycle_generation=37, status='sent',
            attempt_count=1, created_at=_utcnow(),
        ))
        db.session.add(ServiceNotificationOutbox(
            event_id='lc-1', service_key=self.service_key,
            server_id=self.server.id, client_uuid=self.client_uuid,
            generation=37, reason='renewal', invalidate_kinds='low_volume',
            status='sent', attempt_count=1, created_at=_utcnow(),
        ))
        db.session.add(SmsSendLog(
            email=self.email, server_id=self.server.id, server_name='DE-3',
            state='low_volume', recipient='0912***4643', status='sent',
            requested_to='09195292411', sent_to='+989195292411',
            service_key=self.service_key, lifecycle_generation=37,
            created_at=_utcnow(), updated_at=_utcnow(),
        ))
        db.session.commit()
        response = self.get(
            '/machine/v1/sms/notifications/%s' % self.service_key)
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        self.assertEqual(body['schema_version'], machine_api.SCHEMA_SMS)
        self.assertEqual(body['count'], 3)
        serialized = json.dumps(body)
        self.assertNotIn('09195292411', serialized)
        self.assertNotIn('989195292411', serialized)
        send = body['sends'][0]
        self.assertIn('***', send['requested_to'])
        self.assertIn('***', send['sent_to'])
        self.assertEqual(body['notifications'][0]['notification_kind'], 'low_volume')


class MachineEventTests(unittest.TestCase):
    """The event envelope + HMAC formula documented in the contract."""

    SECRET = 'unit-test-events-secret-0123456789abcdef'

    def _event(self):
        return machine_events.build_event(
            'eve.service.lifecycle_changed.v1',
            {'service_key': 'eve:12:abc', 'generation': 37, 'state': 'expiring'},
            event_id='evt-fixed-1',
            occurred_at=datetime(2026, 11, 15, 10, 0, 0),
            subject_refs={'service_key': 'eve:12:abc'},
            generation=37,
            correlation_id='corr-1',
        )

    def test_envelope_contains_the_documented_fields(self):
        event = self._event()
        self.assertEqual(event['event_id'], 'evt-fixed-1')
        self.assertEqual(event['type'], 'eve.service.lifecycle_changed.v1')
        self.assertEqual(event['schema_version'], 1)
        self.assertEqual(event['producer'], 'eve')
        self.assertEqual(event['tenant_scope'], 'internal-only')
        self.assertEqual(event['occurred_at'], '2026-11-15T10:00:00Z')
        self.assertEqual(event['generation'], 37)
        self.assertEqual(event['subject_refs'], {'service_key': 'eve:12:abc'})
        self.assertEqual(event['payload']['state'], 'expiring')

    def test_signature_matches_the_documented_formula(self):
        captured = {}

        class _Response:
            status_code = 202

        def fake_post(url, data=None, headers=None, timeout=None):
            captured.update(url=url, data=data, headers=headers, timeout=timeout)
            return _Response()

        event = self._event()
        result = machine_events.push_event(
            event, base_url='https://god.example/', secret=self.SECRET,
            post=fake_post, now=1763191200,
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['event_id'], 'evt-fixed-1')
        self.assertEqual(captured['url'],
                         'https://god.example/api/v1/integrations/eve/events')
        self.assertEqual(captured['headers']['X-Eve-Delivery-Id'], 'evt-fixed-1')
        self.assertEqual(captured['headers']['X-Eve-Timestamp'], '1763191200')
        body = machine_events.encode_event_body(event)
        self.assertEqual(captured['data'], body)
        expected = hmac.new(
            self.SECRET.encode('utf-8'),
            b'1763191200.evt-fixed-1.' + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(captured['headers']['X-Eve-Signature'],
                         'sha256=' + expected)
        self.assertNotIn(self.SECRET, json.dumps(captured['headers']))

    def test_unknown_type_and_missing_required_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            machine_events.build_event('eve.unknown.v1', {})
        with self.assertRaises(ValueError):
            machine_events.build_event(
                'eve.renewal.verified.v1', {'service_key': 'eve:1:x'})

    def test_raw_pii_or_message_text_is_rejected(self):
        for forbidden in ({'service_key': 'eve:1:x', 'generation': 1,
                           'state': 'active', 'phone': '0912...'},
                          {'service_key': 'eve:1:x', 'generation': 1,
                           'state': 'active', 'message': 'hello'}):
            with self.assertRaises(ValueError):
                machine_events.build_event(
                    'eve.service.lifecycle_changed.v1', forbidden)

    def test_missing_configuration_is_reported_not_raised(self):
        result = machine_events.push_event(
            self._event(), base_url='', secret='', post=None)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'events_not_configured')

    def test_http_failure_is_reported(self):
        class _Response:
            status_code = 503

        result = machine_events.push_event(
            self._event(), base_url='https://god.example', secret=self.SECRET,
            post=lambda *args, **kwargs: _Response(), now=1763191200)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'http_503')

    def test_push_event_type_builds_and_sends(self):
        captured = {}

        class _Response:
            status_code = 200

        def fake_post(url, data=None, headers=None, timeout=None):
            captured['body'] = data
            return _Response()

        result = machine_events.push_event_type(
            'eve.payment.recorded.v1',
            {'payment_id': 5, 'customer_id': 9, 'amount': 100000,
             'sender_card_hash': 'a' * 64},
            base_url='https://god.example', secret=self.SECRET,
            post=fake_post, now=1763191200,
        )
        self.assertTrue(result['ok'])
        decoded = json.loads(captured['body'])
        self.assertEqual(decoded['payload']['sender_card_hash'], 'a' * 64)


if __name__ == '__main__':
    unittest.main()
