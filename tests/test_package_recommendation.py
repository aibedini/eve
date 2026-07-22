import io
import os
import tempfile
import unittest
import requests
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from sqlalchemy.exc import IntegrityError
from cryptography.fernet import Fernet


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    Admin,
    BankCard,
    CustomerAccount,
    GLOBAL_SERVER_DATA,
    PendingSms,
    RenewalEvent,
    OwnershipClaim,
    OwnershipClaimItem,
    Package,
    Server,
    ServiceDelegation,
    ServiceOwnership,
    SubAppConfig,
    TelegramIdentity,
    TelegramOwnershipSession,
    TelegramPurchaseRequest,
    TelegramPurchaseRequestAllocation,
    TelegramPurchaseRequestDetail,
    TelegramPurchaseInboundRoute,
    TelegramPurchaseNameDraft,
    TelegramPurchasePolicy,
    TelegramPurchaseServerRule,
    TelegramPurchaseSession,
    TelegramServiceRequest,
    TelegramServiceRequestMessage,
    TelegramServiceSession,
    TelegramBotInstance,
    TelegramBotRuntime,
    TelegramBotTestUser,
    TelegramBotUserState,
    TelegramEgressProfile,
    TelegramProxyEndpoint,
    SMS_GMWEB_API_KEY_KEY,
    SMS_GMWEB_BASE_URL_KEY,
    SMS_GMWEB_TIMEOUT_KEY,
    SmsSendLog,
    SystemConfig,
    SystemSetting,
    GENERAL_CALENDAR_SETTING_KEY,
    GENERAL_TIMEZONE_SETTING_KEY,
    app,
    db,
    add_cached_client,
    _build_subscription_package_recommendation,
    _cancel_pending_sms_for_account,
    _cancel_sms_via_gmweb,
    _cancel_stale_account_sms,
    _run_sms_depletion_scan,
    _send_sms_via_gmweb,
    _sms_db_segment_stats_today,
    _sms_db_segments_used_today,
    _sms_gateway_ready,
    _sms_reserve_daily_segments,
    _select_subscription_package,
    normalize_iran_mobile,
    discover_phone_ownership_claim,
    format_app_datetime,
    parse_jalali_date,
    review_ownership_claim_item,
    _telegram_bot_attempt,
    _telegram_bot_diagnostic,
    _detect_telegram_inbound_profiles,
    _telegram_proxy_from_payload,
    verify_ownership_claim_subscription,
    v3_add_client,
)
from telegram_diagnostics import (  # noqa: E402
    classify_telegram_connection_error, probe_telegram_transport,
    redact_connection_error,
)
from telegram_xray import XraySupervisor, build_xray_config_from_uri, write_xray_config  # noqa: E402
from telegram_bot_worker import (  # noqa: E402
    _ensure_purchase_detail,
    _ensure_purchase_inbound_allocation,
    _extract_subscription_token,
    _purchase_provisioning_inbound_ids,
    _render_purchase_account_name,
    _service_expiry,
    process_update,
)
from telegram_bot_runtime import COPY, TelegramBotApi, TelegramRoute  # noqa: E402


PACKAGES = [
    {'id': 1, 'name': '10 GB', 'days': 31, 'volume': 10, 'price': 150_000},
    {'id': 2, 'name': '20 GB', 'days': 31, 'volume': 20, 'price': 270_000},
    {'id': 3, 'name': '30 GB', 'days': 31, 'volume': 30, 'price': 390_000},
    {'id': 4, 'name': '50 GB', 'days': 31, 'volume': 50, 'price': 600_000},
]


class FakeTelegramApi:
    def __init__(self):
        self.messages = []
        self.callbacks = []
        self.copies = []
        self.media = []
        self.uploads = []

    def send_message(self, chat_id, text, **extra):
        self.messages.append({'chat_id': chat_id, 'text': text, **extra})
        return {'message_id': len(self.messages)}, 'test'

    def answer_callback(self, callback_query_id, text=''):
        self.callbacks.append((callback_query_id, text))
        return True, 'test'

    def copy_message(self, chat_id, from_chat_id, message_id):
        self.copies.append({
            'chat_id': chat_id, 'from_chat_id': from_chat_id, 'message_id': message_id,
        })
        return {'message_id': message_id}, 'test'

    def send_photo(self, chat_id, file_id, **extra):
        self.media.append({'kind': 'photo', 'chat_id': chat_id, 'file_id': file_id, **extra})
        return {'message_id': len(self.media)}, 'test'

    def send_document(self, chat_id, file_id, **extra):
        self.media.append({'kind': 'document', 'chat_id': chat_id, 'file_id': file_id, **extra})
        return {'message_id': len(self.media)}, 'test'

    def send_upload(self, chat_id, content, filename, content_type, *, as_photo=False,
                    caption=''):
        self.uploads.append({
            'chat_id': chat_id, 'content': content, 'filename': filename,
            'content_type': content_type, 'as_photo': as_photo, 'caption': caption,
        })
        if as_photo:
            result = {'message_id': 800, 'photo': [{
                'file_id': 'uploaded-photo', 'file_unique_id': 'uploaded-photo-unique',
                'file_size': len(content),
            }]}
        else:
            result = {'message_id': 800, 'document': {
                'file_id': 'uploaded-document', 'file_unique_id': 'uploaded-document-unique',
                'file_name': filename, 'mime_type': content_type, 'file_size': len(content),
            }}
        return result, 'test'


class PackageRecommendationRegressionTests(unittest.TestCase):
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

    def tearDown(self):
        db.session.rollback()
        TelegramEgressProfile.query.delete()
        TelegramProxyEndpoint.query.delete()
        TelegramPurchaseRequestAllocation.query.delete()
        TelegramPurchaseRequestDetail.query.delete()
        TelegramPurchaseRequest.query.delete()
        TelegramPurchaseInboundRoute.query.delete()
        TelegramPurchaseNameDraft.query.delete()
        TelegramPurchaseSession.query.delete()
        TelegramPurchaseServerRule.query.delete()
        TelegramPurchasePolicy.query.delete()
        TelegramServiceRequestMessage.query.delete()
        TelegramServiceRequest.query.delete()
        TelegramServiceSession.query.delete()
        TelegramBotUserState.query.delete()
        TelegramOwnershipSession.query.delete()
        TelegramBotTestUser.query.delete()
        TelegramBotRuntime.query.delete()
        TelegramBotInstance.query.delete()
        OwnershipClaimItem.query.delete()
        OwnershipClaim.query.delete()
        TelegramIdentity.query.delete()
        ServiceDelegation.query.delete()
        ServiceOwnership.query.delete()
        SubAppConfig.query.delete()
        CustomerAccount.query.delete()
        Package.query.delete()
        BankCard.query.delete()
        SmsSendLog.query.delete()
        PendingSms.query.delete()
        SystemConfig.query.filter(SystemConfig.key.in_([
            SMS_GMWEB_BASE_URL_KEY,
            SMS_GMWEB_API_KEY_KEY,
            SMS_GMWEB_TIMEOUT_KEY,
        ])).delete(synchronize_session=False)
        SystemSetting.query.filter(SystemSetting.key.in_([
            GENERAL_CALENDAR_SETTING_KEY,
            GENERAL_TIMEZONE_SETTING_KEY,
        ])).delete(synchronize_session=False)
        Admin.query.filter(Admin.username.like('claim-test-%')).delete(synchronize_session=False)
        db.session.commit()
        # Reset the scoped session so stale/dirty instances from this test can
        # never leak into the next test's identity map (flush-time corruption).
        db.session.remove()

    def _make_claim(self, suffix, *, reviewer_role='admin', requested_reseller=False):
        customer = CustomerAccount(primary_phone=f'98912000{int(suffix):04d}')
        identity = TelegramIdentity(
            customer=customer,
            telegram_user_id=8_000_000 + int(suffix),
            telegram_chat_id=8_000_000 + int(suffix),
            phone_normalized=customer.primary_phone,
            phone_verified_at=datetime.utcnow(),
        )
        server = Server(
            name=f'Claim Test {suffix}', host=f'https://claim-{suffix}.test',
            username='u', password='p',
        )
        reviewer = Admin(username=f'claim-test-{suffix}', role=reviewer_role)
        reviewer.set_password('StrongClaimPassword123!')
        db.session.add_all([customer, identity, server, reviewer])
        db.session.flush()
        claim = OwnershipClaim(
            customer_id=customer.id,
            telegram_identity_id=identity.id,
            requested_reseller_id=(reviewer.id if requested_reseller else None),
            verified_phone=customer.primary_phone,
            claim_method='admin_review',
        )
        db.session.add(claim)
        db.session.flush()
        item = OwnershipClaimItem(
            claim_id=claim.id,
            server_id=server.id,
            client_uuid=f'claim-client-{suffix}',
            client_email_snapshot=f'g{suffix}-{customer.primary_phone}',
            match_reason='phone_match',
            match_score=90,
        )
        db.session.add(item)
        db.session.commit()
        return customer, identity, server, reviewer, claim, item

    def test_forecast_above_catalog_uses_largest_available(self):
        selected, comfort, _ = _select_subscription_package(
            PACKAGES, daily_gb=3.0, safety_margin=0.2,
        )
        self.assertEqual(selected['id'], 4)
        self.assertIsNone(comfort)

    def test_iran_mobile_normalization_accepts_supported_prefixes(self):
        expected = '989195292411'
        for value in (
            '09195292411', '9195292411', '+989195292411',
            '00989195292411', '98 919 529 2411', '۰۹۱۹۵۲۹۲۴۱۱',
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_iran_mobile(value), expected)

    def test_iran_mobile_normalization_extracts_from_client_label(self):
        self.assertEqual(normalize_iran_mobile('g276-09195292411'), '989195292411')
        self.assertEqual(normalize_iran_mobile('customer_9195292411'), '989195292411')

    def test_iran_mobile_normalization_rejects_incomplete_or_landline_values(self):
        for value in ('', None, '0919529241', '02188776655', 'account-1234567890'):
            with self.subTest(value=value):
                self.assertEqual(normalize_iran_mobile(value), '')

    def test_customer_account_stores_only_canonical_verified_phone(self):
        customer = CustomerAccount(display_name='Test Customer')
        canonical = customer.set_primary_phone('۰۹۱۹ ۵۲۹ ۲۴۱۱', verified=True)
        db.session.add(customer)
        db.session.commit()

        self.assertEqual(canonical, '989195292411')
        self.assertEqual(customer.primary_phone, canonical)
        self.assertIsNotNone(customer.phone_verified_at)
        self.assertEqual(customer.status, 'active')
        self.assertEqual(customer.preferred_language, 'fa')

    def test_customer_account_rejects_invalid_phone(self):
        customer = CustomerAccount()
        with self.assertRaises(ValueError):
            customer.set_primary_phone('not-a-mobile')

    def test_service_ownership_is_unique_per_stable_panel_identity(self):
        customer = CustomerAccount(primary_phone='989195292411')
        other = CustomerAccount(primary_phone='989121234567')
        server = Server(name='Ownership Test', host='https://example.test', username='u', password='p')
        db.session.add_all([customer, other, server])
        db.session.flush()
        db.session.add(ServiceOwnership(
            customer_id=customer.id, server_id=server.id,
            client_uuid='client-stable-id', client_email_snapshot='g276-09195292411',
        ))
        db.session.commit()

        db.session.add(ServiceOwnership(
            customer_id=other.id, server_id=server.id,
            client_uuid='client-stable-id', client_email_snapshot='renamed-client',
        ))
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_service_delegation_filters_permissions_and_is_revocable(self):
        owner = CustomerAccount(primary_phone='989195292411')
        delegate = CustomerAccount(primary_phone='989121234567')
        server = Server(name='Delegation Test', host='https://example.test', username='u', password='p')
        db.session.add_all([owner, delegate, server])
        db.session.flush()
        ownership = ServiceOwnership(
            customer_id=owner.id, server_id=server.id, client_uuid='family-service',
        )
        db.session.add(ownership)
        db.session.flush()
        delegation = ServiceDelegation(
            service_ownership_id=ownership.id,
            delegate_customer_id=delegate.id,
            invited_by_customer_id=owner.id,
            accepted_at=datetime.utcnow(),
        )
        safe = delegation.set_permissions({
            'view_status': True, 'create_ticket': True, 'transfer_ownership': True,
        })
        db.session.add(delegation)
        db.session.commit()

        self.assertTrue(delegation.is_active)
        self.assertTrue(safe['view_status'])
        self.assertTrue(safe['create_ticket'])
        self.assertNotIn('transfer_ownership', delegation.get_permissions())
        self.assertFalse(delegation.get_permissions()['renew'])

        delegation.revoked_at = datetime.utcnow()
        self.assertFalse(delegation.is_active)

    def test_telegram_identity_verifies_and_canonicalizes_own_phone(self):
        identity = TelegramIdentity(telegram_user_id=7000001)
        canonical = identity.set_verified_phone('0919 529 2411')
        self.assertEqual(canonical, '989195292411')
        self.assertEqual(identity.phone_normalized, canonical)
        self.assertIsNotNone(identity.phone_verified_at)

    def test_admin_can_approve_claim_and_create_service_ownership(self):
        customer, _identity, server, reviewer, claim, item = self._make_claim(1)
        result = review_ownership_claim_item(item.id, reviewer, approve=True)

        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(result['claim_status'], 'approved')
        ownership = ServiceOwnership.query.filter_by(
            server_id=server.id, client_uuid='claim-client-1',
        ).one()
        self.assertEqual(ownership.customer_id, customer.id)
        self.assertEqual(ownership.verified_by_admin_id, reviewer.id)
        self.assertEqual(claim.status, 'approved')

    def test_reseller_cannot_review_another_resellers_claim(self):
        _customer, _identity, _server, owner_reseller, _claim, item = self._make_claim(
            2, reviewer_role='reseller', requested_reseller=True,
        )
        other = Admin(username='claim-test-other', role='reseller')
        other.set_password('StrongClaimPassword123!')
        db.session.add(other)
        db.session.commit()

        with self.assertRaises(PermissionError):
            review_ownership_claim_item(item.id, other, approve=True)
        self.assertEqual(item.status, 'pending')
        self.assertNotEqual(owner_reseller.id, other.id)

    def test_claim_approval_detects_existing_active_owner_conflict(self):
        customer, _identity, server, reviewer, claim, item = self._make_claim(3)
        existing_customer = CustomerAccount(primary_phone='989121111111')
        db.session.add(existing_customer)
        db.session.flush()
        existing = ServiceOwnership(
            customer_id=existing_customer.id,
            server_id=server.id,
            client_uuid=item.client_uuid,
            client_email_snapshot='existing-owner',
        )
        db.session.add(existing)
        db.session.commit()

        result = review_ownership_claim_item(item.id, reviewer, approve=True)
        self.assertFalse(result['success'])
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['claim_status'], 'needs_attention')
        self.assertEqual(item.conflict_owner_id, existing_customer.id)
        self.assertEqual(existing.customer_id, existing_customer.id)
        self.assertNotEqual(existing.customer_id, customer.id)
        self.assertEqual(claim.status, 'needs_attention')

    def test_claim_item_can_be_rejected_with_audited_reason(self):
        _customer, _identity, _server, reviewer, claim, item = self._make_claim(4)
        result = review_ownership_claim_item(
            item.id, reviewer, approve=False, rejection_reason='Phone ownership not proven',
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['claim_status'], 'rejected')
        self.assertEqual(item.rejection_reason, 'Phone ownership not proven')
        self.assertEqual(item.reviewed_by_admin_id, reviewer.id)
        self.assertEqual(claim.status, 'rejected')

    def test_telegram_bot_and_proxy_safe_dicts_never_expose_secrets(self):
        bot = TelegramBotInstance(scope_key='system', token_encrypted='enc:secret')
        db.session.add(bot)
        db.session.flush()
        proxy = TelegramProxyEndpoint(
            bot_instance_id=bot.id, proxy_type='socks5', host='127.0.0.1', port=1080,
            username_encrypted='enc:user', password_encrypted='enc:password',
        )
        db.session.add(proxy)
        db.session.commit()

        bot_payload = bot.to_safe_dict()
        proxy_payload = proxy.to_safe_dict()
        self.assertTrue(bot_payload['token_configured'])
        self.assertNotIn('token', bot_payload)
        self.assertTrue(proxy_payload['password_configured'])
        self.assertNotIn('password', proxy_payload)
        self.assertNotIn('username', proxy_payload)

    def test_proxy_payload_encrypts_credentials_and_blank_preserves_them(self):
        fernet = Fernet(Fernet.generate_key())
        proxy = TelegramProxyEndpoint(proxy_type='socks5', priority=100, enabled=True)
        with patch('app._get_server_password_fernet', return_value=fernet):
            _telegram_proxy_from_payload(proxy, {
                'proxy_type': 'socks5', 'host': 'proxy.test', 'port': 1080,
                'username': 'alice', 'password': 'secret', 'priority': 10,
            })
            encrypted_password = proxy.password_encrypted
            _telegram_proxy_from_payload(proxy, {
                'host': 'proxy.test', 'port': 1080, 'username': '', 'password': '',
            })
        self.assertTrue(encrypted_password.startswith('enc:'))
        self.assertEqual(proxy.password_encrypted, encrypted_password)

    def test_configured_diagnostic_falls_back_from_proxy_to_direct(self):
        fernet = Fernet(Fernet.generate_key())
        with patch('app._get_server_password_fernet', return_value=fernet):
            bot = TelegramBotInstance(
                scope_key='system', connection_mode='proxy_first',
                token_encrypted='enc:' + fernet.encrypt(b'123456:abcdefghijklmnopqrstuvwxyz').decode(),
            )
            db.session.add(bot)
            db.session.flush()
            proxy = TelegramProxyEndpoint(
                bot_instance_id=bot.id, proxy_type='socks5', host='proxy.test', port=1080,
            )
            db.session.add(proxy)
            db.session.commit()

            class GoodResponse:
                status_code = 200
                content = b'{}'

                @staticmethod
                def json():
                    return {'ok': True, 'result': {'id': 42, 'username': 'eve_test_bot', 'first_name': 'Eve'}}

            def fake_get_me(_token, proxies=None, timeout_sec=10):
                if proxies:
                    raise ConnectionError('proxy unavailable')
                return GoodResponse()

            transport_ok = {'success': True, 'stages': [
                {'name': 'transport', 'status': 'passed', 'latency_ms': 1},
            ]}
            with patch('app.probe_telegram_transport', return_value=transport_ok), \
                    patch('app._telegram_get_me', side_effect=fake_get_me):
                result = _telegram_bot_diagnostic(bot, route='configured')

        self.assertTrue(result['success'])
        self.assertEqual(result['route'], 'direct')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(proxy.health_status, 'failed')
        self.assertEqual(bot.bot_username, 'eve_test_bot')

    def test_staged_proxy_diagnostic_stops_before_get_me_and_persists_safe_error(self):
        fernet = Fernet(Fernet.generate_key())
        password = 'do-not-leak-proxy-password'
        with patch('app._get_server_password_fernet', return_value=fernet):
            bot = TelegramBotInstance(
                scope_key='system', connection_mode='proxy_only',
                token_encrypted='enc:' + fernet.encrypt(b'123456:abcdefghijklmnopqrstuvwxyz').decode(),
            )
            db.session.add(bot)
            db.session.flush()
            proxy = TelegramProxyEndpoint(
                bot_instance_id=bot.id, proxy_type='socks5', host='proxy.test', port=1080,
                password_encrypted='enc:' + fernet.encrypt(password.encode()).decode(),
            )
            db.session.add(proxy)
            db.session.commit()
            failed = {
                'success': False,
                'error': 'SOCKS authentication failed for ***',
                'error_code': 'proxy_tunnel_auth_failed',
                'stages': [
                    {'name': 'proxy_tcp', 'status': 'passed', 'latency_ms': 2},
                    {'name': 'proxy_tunnel', 'status': 'failed', 'latency_ms': 3,
                     'error_code': 'proxy_tunnel_auth_failed'},
                ],
            }
            with patch('app.probe_telegram_transport', return_value=failed), \
                    patch('app._telegram_get_me') as get_me:
                result = _telegram_bot_diagnostic(bot, only_proxy_id=proxy.id)

        self.assertFalse(result['success'])
        self.assertEqual(result['attempts'][0]['error_code'], 'proxy_tunnel_auth_failed')
        self.assertEqual(len(result['attempts'][0]['stages']), 2)
        self.assertNotIn(password, proxy.last_error)
        get_me.assert_not_called()

    def test_transport_probe_classifies_proxy_tcp_timeout(self):
        def timeout_socket(*_args, **_kwargs):
            raise TimeoutError('connection timed out')

        result = probe_telegram_transport(
            proxy_type='socks5', host='proxy.test', port=1080,
            username='alice', password='secret-value', socket_factory=timeout_socket,
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'proxy_tcp_timeout')
        self.assertEqual(result['stages'][0]['name'], 'proxy_tcp')
        self.assertNotIn('secret-value', str(result))

    def test_connection_error_redaction_removes_proxy_and_bot_secrets(self):
        raw = ('HTTPSConnectionPool at socks5h://user:password@proxy.test:1080 '
               'for /bot123456:abcdefghijklmnopqrstuvwxyz/getMe')
        safe = redact_connection_error(raw, ('user', 'password'))
        self.assertNotIn('password', safe)
        self.assertNotIn('123456:abcdefghijklmnopqrstuvwxyz', safe)
        self.assertIn('/bot***', safe)

    def test_ssl_zero_return_is_classified_as_closed_xray_outbound(self):
        raw = ("SOCKSHTTPSConnectionPool(host='api.telegram.org', port=443): "
               "SSLZeroReturnError: TLS/SSL connection has been closed (EOF)")
        code, message = classify_telegram_connection_error(raw)
        self.assertEqual(code, 'route_outbound_closed')
        self.assertIn('selected route', message.lower())
        self.assertNotIn('SOCKSHTTPSConnectionPool', message)

    def test_get_me_eof_returns_friendly_route_error(self):
        transport_ok = {'success': True, 'stages': [
            {'name': 'telegram_tls', 'status': 'passed', 'latency_ms': 1},
        ]}
        failure = requests.exceptions.SSLError(
            "TLS/SSL connection has been closed (EOF)")
        with patch('app.probe_telegram_transport', return_value=transport_ok), \
                patch('app._telegram_get_me', side_effect=failure):
            result = _telegram_bot_attempt(
                '123456:abcdefghijklmnopqrstuvwxyz', 'xray://test',
                proxies={'https': 'socks5h://127.0.0.1:12080'},
            )
        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'route_outbound_closed')
        self.assertEqual(result['api_attempts'], 2)
        self.assertNotIn('SOCKSHTTPSConnectionPool', result['error'])

    def test_get_me_recovers_from_one_transient_eof(self):
        class GoodResponse:
            status_code = 200
            content = b'{}'

            @staticmethod
            def json():
                return {'ok': True, 'result': {'id': 42, 'username': 'eve_test_bot'}}

        transport_ok = {'success': True, 'stages': [
            {'name': 'telegram_tls', 'status': 'passed', 'latency_ms': 1},
        ]}
        failure = requests.exceptions.SSLError(
            "TLS/SSL connection has been closed (EOF)")
        with patch('app.probe_telegram_transport', return_value=transport_ok), \
                patch('app._telegram_get_me', side_effect=[failure, GoodResponse()]), \
                patch('app.time.sleep'):
            result = _telegram_bot_attempt(
                '123456:abcdefghijklmnopqrstuvwxyz', 'xray://test',
                proxies={'https': 'socks5h://127.0.0.1:12080'},
            )
        self.assertTrue(result['success'])
        self.assertEqual(result['api_attempts'], 2)

    def test_runtime_retries_tls_eof_with_fresh_connection_pool(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {'ok': True, 'result': {'id': 42}}
        failure = requests.exceptions.SSLError(
            "TLS/SSL connection has been closed (EOF)")
        api = TelegramBotApi(
            '123456:abcdefghijklmnopqrstuvwxyz',
            [TelegramRoute('xray://test', {'https': 'socks5h://127.0.0.1:12080'})],
        )
        api._session = MagicMock()
        api._session.post.side_effect = [failure, response]
        with patch('telegram_bot_runtime.time.sleep'):
            result, route = api.call('getMe')
        self.assertEqual(result['id'], 42)
        self.assertEqual(route, 'xray://test')
        self.assertEqual(api._session.post.call_count, 2)
        api._session.close.assert_called_once_with()

    def test_vless_ws_tls_is_rendered_as_loopback_only_xray_config(self):
        uri = ('vless://11111111-1111-1111-1111-111111111111@example.com:443'
               '?type=ws&security=tls&sni=edge.example.com&host=cdn.example.com&path=%2Feve#route')
        config = build_xray_config_from_uri(uri, 12080)
        inbound = config['inbounds'][0]
        outbound = config['outbounds'][0]
        self.assertEqual(inbound['listen'], '127.0.0.1')
        self.assertEqual(inbound['port'], 12080)
        self.assertEqual(outbound['protocol'], 'vless')
        self.assertEqual(outbound['streamSettings']['network'], 'ws')
        self.assertEqual(outbound['streamSettings']['wsSettings']['path'], '/eve')
        self.assertEqual(outbound['streamSettings']['tlsSettings']['serverName'], 'edge.example.com')

    def test_vless_http_obfuscation_is_not_silently_dropped(self):
        uri = ('vless://11111111-1111-1111-1111-111111111111@example.com:80'
               '?type=http&host=edge.example.com&path=%2Ftelegram#route')
        config = build_xray_config_from_uri(uri, 12080)
        stream = config['outbounds'][0]['streamSettings']
        self.assertEqual(stream['network'], 'tcp')
        header = stream['tcpSettings']['header']
        self.assertEqual(header['type'], 'http')
        self.assertEqual(header['request']['path'], ['/telegram'])
        self.assertEqual(header['request']['headers']['Host'], ['edge.example.com'])

    def test_vless_reality_requires_public_key(self):
        uri = ('vless://11111111-1111-1111-1111-111111111111@example.com:443'
               '?type=tcp&security=reality&sni=example.org')
        with self.assertRaisesRegex(ValueError, 'public key'):
            build_xray_config_from_uri(uri, 12080)

    def test_xray_config_file_is_atomic_and_contains_no_public_listener(self):
        uri = ('vless://11111111-1111-1111-1111-111111111111@example.com:443'
               '?type=grpc&security=tls&serviceName=eve')
        with tempfile.TemporaryDirectory() as directory:
            path, digest = write_xray_config(uri, 12081, directory, 7)
            with open(path, encoding='utf-8') as handle:
                payload = handle.read()
        self.assertTrue(digest)
        self.assertIn('127.0.0.1', payload)
        self.assertNotIn('0.0.0.0', payload)

    def test_xray_supervisor_uses_fixed_argv_without_a_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = os.path.join(directory, 'xray.exe')
            with open(binary, 'wb') as handle:
                handle.write(b'placeholder')
            config_path = os.path.join(directory, 'profile-1.json')
            process = MagicMock()
            process.poll.return_value = None
            process.pid = 4321
            validation = MagicMock(returncode=0, stderr='')
            supervisor = XraySupervisor(directory, binary)
            with patch('telegram_xray.write_xray_config', return_value=(config_path, 'digest')), \
                    patch('telegram_xray.subprocess.run', return_value=validation) as run, \
                    patch('telegram_xray.subprocess.Popen', return_value=process) as popen, \
                    patch('telegram_xray._port_ready', return_value=True):
                result = supervisor.sync(1, 'vless://secret', 12080)

        self.assertTrue(result['success'])
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertIsInstance(popen.call_args.args[0], list)
        self.assertFalse(popen.call_args.kwargs['shell'])
        self.assertNotIn('vless://secret', str(run.call_args))
        self.assertNotIn('vless://secret', str(popen.call_args))

    def test_telegram_egress_safe_dict_never_exposes_connection_uri(self):
        profile = TelegramEgressProfile(
            bot_instance_id=1, name='Foreign route', config_encrypted='enc:top-secret',
            local_port=12080,
        )
        safe = profile.to_safe_dict()
        self.assertTrue(safe['config_configured'])
        self.assertNotIn('config_encrypted', safe)
        self.assertNotIn('config_uri', safe)
        self.assertNotIn('top-secret', str(safe))

    def test_telegram_egress_api_encrypts_uri_and_blank_edit_preserves_it(self):
        reviewer = Admin(
            username='claim-test-egress-admin', role='superadmin',
            is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        db.session.add(reviewer)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id
        uri = ('vless://11111111-1111-1111-1111-111111111111@example.com:443'
               '?type=ws&security=tls&sni=example.com&path=%2Feve')
        fernet = Fernet(Fernet.generate_key())
        with patch('app._get_server_password_fernet', return_value=fernet):
            created = client.post('/api/settings/telegram-bots/egress', json={
                'name': 'Foreign route', 'config_uri': uri,
                'local_port': 12080, 'priority': 10, 'enabled': True,
            }).get_json()
            profile = db.session.get(TelegramEgressProfile, created['profile']['id'])
            encrypted = profile.config_encrypted
            updated = client.put(
                f"/api/settings/telegram-bots/egress/{profile.id}",
                json={'name': 'Foreign route edited', 'config_uri': '',
                      'local_port': 12080, 'priority': 10, 'enabled': True},
            ).get_json()
            loaded = client.get('/api/settings/telegram-bots').get_json()

        self.assertTrue(created['success'])
        self.assertTrue(encrypted.startswith('enc:'))
        self.assertNotIn(uri, str(created))
        self.assertTrue(updated['success'])
        self.assertEqual(profile.config_encrypted, encrypted)
        self.assertNotIn(uri, str(loaded))
        self.assertNotIn('config_encrypted', str(loaded))

    def test_telegram_egress_test_waits_for_pending_worker_reload(self):
        reviewer = Admin(
            username='claim-test-egress-pending', role='superadmin',
            is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        bot = TelegramBotInstance(
            scope_key='system', display_name='Pending route bot',
            connection_mode='proxy_only',
        )
        db.session.add_all([reviewer, bot])
        db.session.flush()
        profile = TelegramEgressProfile(
            bot_instance_id=bot.id, name='Reloading route',
            config_encrypted='enc:not-read-during-pending-test',
            local_port=12080, enabled=True, runtime_status='pending',
        )
        db.session.add(profile)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id
        with patch('app.time.monotonic', side_effect=[0, 13]):
            response = client.post(
                f'/api/settings/telegram-bots/egress/{profile.id}/test')
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['error_code'], 'xray_route_not_ready')
        self.assertEqual(payload['runtime_status'], 'pending')

    def test_telegram_egress_candidates_never_return_client_uuid(self):
        reviewer = Admin(
            username='claim-test-egress-candidates', role='superadmin',
            is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        server = Server(
            name='Candidate Foreign', host='https://foreign.example.com',
            username='u', password='p', enabled=True,
        )
        db.session.add_all([reviewer, server])
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id
        secret_uuid = '11111111-2222-3333-4444-555555555555'
        previous = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': server.id, 'id': 91, 'remark': 'Telegram route',
            'protocol': 'vless', 'streamSettings': {'network': 'ws', 'security': 'tls'},
            'clients': [{'id': secret_uuid, 'email': 'eve-system-route'}],
        }]
        try:
            payload = client.get('/api/settings/telegram-bots/egress/candidates').get_json()
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous

        self.assertTrue(payload['success'])
        self.assertEqual(payload['candidates'][0]['client_id'], 'eve-system-route')
        self.assertNotIn(secret_uuid, str(payload))

    def test_telegram_bot_settings_api_masks_saved_token(self):
        reviewer = Admin(
            username='claim-test-ui-admin', role='superadmin', is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        db.session.add(reviewer)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id

        fernet = Fernet(Fernet.generate_key())
        with patch('app._get_server_password_fernet', return_value=fernet):
            response = client.post('/api/settings/telegram-bots', json={
                'display_name': 'Eve Test Bot',
                'bot_token': '123456:abcdefghijklmnopqrstuvwxyz',
                'enabled': False,
                'test_mode': True,
                'enabled_languages': ['fa', 'en'],
                'default_language': 'fa',
                'connection_mode': 'proxy_first',
            })
            saved = response.get_json()
            loaded_response = client.get('/api/settings/telegram-bots')
            loaded = loaded_response.get_json()
            settings_page = client.get('/settings')

        self.assertTrue(saved['success'])
        self.assertTrue(loaded['success'])
        self.assertTrue(loaded['bot']['token_configured'])
        self.assertNotIn('bot_token', loaded['bot'])
        self.assertNotIn('token_encrypted', loaded['bot'])
        self.assertEqual(settings_page.status_code, 200)
        self.assertIn(b'id="tab-telegram"', settings_page.data)
        self.assertIn(b'id="telegram-subtab-bots"', settings_page.data)
        self.assertIn(b'Telegram Announcements', settings_page.data)
        self.assertNotIn(b'tab-telegram_bots', settings_page.data)
        self.assertIn('no-store', loaded_response.headers.get('Cache-Control', ''))
        self.assertEqual(loaded_response.headers.get('Surrogate-Control'), 'no-store')
        self.assertIn('no-store', settings_page.headers.get('Cache-Control', ''))

    def test_general_settings_persist_calendar_and_render_global_date_runtime(self):
        reviewer = Admin(
            username='claim-test-calendar-admin', role='superadmin', is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        db.session.add(reviewer)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id

        response = client.post('/api/settings/general', json={
            'timezone': 'Asia/Tehran',
            'calendar': 'gregorian',
            'panel_lang': 'en',
            'near_expiry_days': 3,
            'near_expiry_hours': 0,
            'low_volume_gb': 1,
            'panel_domain': '',
        })
        loaded = client.get('/api/settings/general').get_json()
        settings_page = client.get('/settings')
        operations_page = client.get('/telegram-operations')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(loaded['calendar'], 'gregorian')
        self.assertEqual(loaded['timezone'], 'Asia/Tehran')
        self.assertIn(b'id="general-calendar"', settings_page.data)
        self.assertIn(b'window.EveDate', settings_page.data)
        self.assertIn(b'parseInput', settings_page.data)
        self.assertEqual(operations_page.status_code, 200)
        self.assertIn(b'overflow-y: auto', operations_page.data)
        self.assertIn(b'grid-template-rows: minmax(0, 1fr)', operations_page.data)
        self.assertIn(b'max-height: 100%', operations_page.data)
        self.assertIn(b'Intl.NumberFormat(EveDate.locale)', operations_page.data)

    def test_configured_calendar_and_timezone_drive_formatting_and_input_parsing(self):
        db.session.add_all([
            SystemSetting(key=GENERAL_CALENDAR_SETTING_KEY, value='gregorian'),
            SystemSetting(key=GENERAL_TIMEZONE_SETTING_KEY, value='Asia/Tehran'),
        ])
        db.session.commit()

        parsed = parse_jalali_date('2026/07/14 12:00')
        self.assertEqual(parsed, datetime(2026, 7, 14, 8, 30))
        self.assertEqual(format_app_datetime(parsed), '2026/07/14 12:00')

        db.session.get(SystemSetting, GENERAL_CALENDAR_SETTING_KEY).value = 'jalali'
        db.session.commit()
        self.assertTrue(format_app_datetime(parsed).startswith('1405/04/'))

    def test_telegram_test_user_api_requires_numeric_id_and_returns_runtime(self):
        reviewer = Admin(
            username='claim-test-bot-testers', role='superadmin', is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        db.session.add(reviewer)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id

        invalid = client.post('/api/settings/telegram-bots/test-users', json={
            'telegram_user_id': '@username', 'label': 'unsafe',
        })
        created = client.post('/api/settings/telegram-bots/test-users', json={
            'telegram_user_id': '123456789', 'label': 'Owner phone',
        })
        loaded = client.get('/api/settings/telegram-bots').get_json()

        self.assertFalse(invalid.get_json()['success'])
        self.assertEqual(created.status_code, 200)
        self.assertEqual(loaded['test_users'][0]['telegram_user_id'], 123456789)
        self.assertEqual(loaded['runtime']['status'], 'stopped')

    def test_telegram_send_test_rejects_disabled_bot(self):
        reviewer = Admin(
            username='claim-test-disabled-send', role='superadmin', is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=False, test_mode=False,
            enabled_languages_json='["fa","en"]', default_language='fa',
        )
        db.session.add_all([reviewer, bot])
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id

        response = client.post('/api/settings/telegram-bots/send-test', json={
            'telegram_user_id': '123456789',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')
        self.assertIn('Enable the Telegram bot', response.get_json()['error'])

    def test_telegram_purchase_policy_api_defaults_and_saves_server_rules(self):
        reviewer = Admin(
            username='claim-test-purchase-policy', role='superadmin',
            is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        server = Server(name='Policy Server', host='https://policy.test', username='u', password='p')
        db.session.add_all([reviewer, server])
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id

        loaded = client.get('/api/settings/telegram-bots').get_json()
        self.assertFalse(loaded['purchase_policy']['customer_selects_server'])
        self.assertEqual(loaded['purchase_policy']['assignment_strategy'], 'least_clients')
        target = next(row for row in loaded['purchase_servers'] if row['server_id'] == server.id)
        target.update({
            'eligible': True, 'customer_visible': True,
            'display_name': 'Germany Premium', 'priority': 5, 'weight': 3,
        })
        response = client.post('/api/settings/telegram-bots', json={
            'display_name': 'Test Bot', 'enabled': False, 'test_mode': True,
            'enabled_languages': ['fa', 'en'], 'default_language': 'fa',
            'connection_mode': 'proxy_first',
            'purchase_policy': {
                'customer_selects_server': True,
                'assignment_strategy': 'priority',
                'account_name_mode': 'customer',
                'account_name_template': 'tg-{telegram_username}-{phone}',
            },
            'purchase_servers': [target],
        })
        self.assertEqual(response.status_code, 200)
        bot = TelegramBotInstance.query.filter_by(scope_key='system').one()
        policy = db.session.get(TelegramPurchasePolicy, bot.id)
        rule = TelegramPurchaseServerRule.query.filter_by(
            bot_instance_id=bot.id, server_id=server.id,
        ).one()
        self.assertTrue(policy.customer_selects_server)
        self.assertEqual(policy.account_name_mode, 'customer')
        self.assertEqual(policy.account_name_template, 'tg-{telegram_username}-{phone}')
        self.assertEqual(rule.display_name, 'Germany Premium')
        self.assertEqual(rule.priority, 5)

        invalid = client.post('/api/settings/telegram-bots', json={
            'display_name': 'Test Bot', 'enabled': False, 'test_mode': True,
            'enabled_languages': ['fa'], 'default_language': 'fa',
            'connection_mode': 'proxy_first',
            'purchase_policy': {
                'customer_selects_server': True,
                'assignment_strategy': 'least_clients',
                'account_name_mode': 'generated',
                'account_name_template': 'unsafe/{unknown}',
            },
            'purchase_servers': [target],
        })
        self.assertFalse(invalid.get_json()['success'])
        self.assertEqual(invalid.headers.get('X-Eve-Status'), '400')

    def test_telegram_test_mode_ignores_unauthorized_user_without_identity(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=True,
            enabled_languages_json='["fa","en"]', default_language='fa',
        )
        db.session.add(bot)
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 1, 'message': {
            'message_id': 1, 'text': '/start',
            'from': {'id': 70001, 'first_name': 'Unknown'},
            'chat': {'id': 70001, 'type': 'private'},
        }})
        db.session.commit()

        self.assertIsNone(TelegramIdentity.query.filter_by(telegram_user_id=70001).first())
        self.assertEqual(CustomerAccount.query.count(), 0)
        self.assertEqual(api.messages, [])

    def test_telegram_start_language_and_verified_contact_flow(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=True,
            enabled_languages_json='["fa","en"]', default_language='fa',
        )
        db.session.add(bot)
        db.session.flush()
        db.session.add(TelegramBotTestUser(
            bot_instance_id=bot.id, telegram_user_id=70002, label='Owner', enabled=True,
        ))
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 2, 'message': {
            'message_id': 1, 'text': '/start',
            'from': {'id': 70002, 'first_name': 'Ali'},
            'chat': {'id': 70002, 'type': 'private'},
        }})
        process_update(api, bot, {'update_id': 3, 'callback_query': {
            'id': 'callback-1', 'data': 'lang:en',
            'from': {'id': 70002, 'first_name': 'Ali'},
            'message': {'chat': {'id': 70002, 'type': 'private'}},
        }})
        process_update(api, bot, {'update_id': 4, 'message': {
            'message_id': 2,
            'from': {'id': 70002, 'first_name': 'Ali'},
            'chat': {'id': 70002, 'type': 'private'},
            'contact': {'user_id': 70002, 'phone_number': '09195292411'},
        }})
        db.session.commit()

        state = TelegramBotUserState.query.filter_by(telegram_user_id=70002).one()
        identity = TelegramIdentity.query.filter_by(telegram_user_id=70002).one()
        customer = db.session.get(CustomerAccount, identity.customer_id)
        self.assertEqual(state.language, 'en')
        self.assertEqual(state.step, 'verified')
        self.assertEqual(customer.primary_phone, '989195292411')
        self.assertEqual(customer.preferred_language, 'en')
        self.assertEqual(api.callbacks, [('callback-1', '✓')])

    def test_telegram_verified_start_shows_persistent_main_menu(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=False,
            enabled_languages_json='["fa","en"]', default_language='fa',
        )
        customer = CustomerAccount(
            primary_phone='989125551230', phone_verified_at=datetime.utcnow(),
            preferred_language='en',
        )
        identity = TelegramIdentity(
            customer=customer, telegram_user_id=70009, telegram_chat_id=70009,
            phone_normalized=customer.primary_phone, phone_verified_at=datetime.utcnow(),
        )
        db.session.add_all([bot, customer, identity])
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 8, 'message': {
            'message_id': 1, 'text': '/start',
            'from': {'id': 70009, 'first_name': 'Verified'},
            'chat': {'id': 70009, 'type': 'private'},
        }})

        self.assertEqual(api.messages[-1]['text'], COPY['en']['welcome_menu'])
        keyboard = api.messages[-1]['reply_markup']
        self.assertTrue(keyboard['is_persistent'])
        self.assertEqual(keyboard['keyboard'][0][0]['text'], COPY['en']['menu_services'])
        self.assertTrue(any(
            button['text'] == COPY['en']['menu_tutorial']
            for row in keyboard['keyboard'] for button in row
        ))

    def test_telegram_tutorial_uses_website_apps_recommendation_and_links(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=False,
            enabled_languages_json='["fa","en"]', default_language='fa',
        )
        regular = SubAppConfig(
            app_code='plain-android', name='Plain Client', os_type='android',
            is_enabled=True, display_order=1,
        )
        recommended = SubAppConfig(
            app_code='recommended-android', name='Recommended Client', os_type='android',
            is_enabled=True, is_recommended=True, display_order=2,
            title_fa='راهنمای برنامه پیشنهادی',
            description_fa='برنامه را نصب کنید.\nلینک را اضافه کنید.',
            download_link='https://downloads.example/client.apk',
            store_link='https://play.google.com/store/apps/details?id=example.client',
            tutorial_link='https://video.example/tutorial',
        )
        db.session.add_all([bot, regular, recommended])
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 801, 'message': {
            'message_id': 1, 'text': COPY['fa']['menu_tutorial'],
            'from': {'id': 70801, 'first_name': 'Owner'},
            'chat': {'id': 70801, 'type': 'private'},
        }})
        device_buttons = api.messages[-1]['reply_markup']['inline_keyboard']
        self.assertEqual(
            [row[0]['callback_data'] for row in device_buttons],
            ['tutorial-os:android', 'tutorial-os:ios', 'tutorial-os:windows'],
        )

        process_update(api, bot, {'update_id': 802, 'callback_query': {
            'id': 'tutorial-android', 'data': 'tutorial-os:android',
            'from': {'id': 70801},
            'message': {'chat': {'id': 70801, 'type': 'private'}},
        }})
        app_buttons = api.messages[-1]['reply_markup']['inline_keyboard']
        recommended_button = next(
            row[0] for row in app_buttons
            if row[0].get('callback_data') == f'tutorial-app:{recommended.id}'
        )
        self.assertTrue(recommended_button['text'].startswith('⭐'))

        process_update(api, bot, {'update_id': 803, 'callback_query': {
            'id': 'tutorial-app', 'data': f'tutorial-app:{recommended.id}',
            'from': {'id': 70801},
            'message': {'chat': {'id': 70801, 'type': 'private'}},
        }})
        detail = api.messages[-1]
        self.assertIn('راهنمای برنامه پیشنهادی', detail['text'])
        self.assertIn('لینک را اضافه کنید.', detail['text'])
        urls = {
            button['text']: button['url']
            for row in detail['reply_markup']['inline_keyboard'] for button in row
            if button.get('url')
        }
        self.assertEqual(urls[COPY['fa']['tutorial_download']], recommended.download_link)
        self.assertEqual(urls[COPY['fa']['tutorial_google_play']], recommended.store_link)
        self.assertEqual(urls[COPY['fa']['tutorial_video']], recommended.tutorial_link)

    def test_telegram_service_expiry_follows_global_site_calendar(self):
        expiry = datetime(2027, 7, 19, 12, 0, tzinfo=timezone.utc)
        expiry_ms = int(expiry.timestamp() * 1000)
        client = {'expiryTimestamp': expiry_ms, 'raw_client': {}}
        db.session.add(SystemSetting(key=GENERAL_TIMEZONE_SETTING_KEY, value='Asia/Tehran'))
        calendar = SystemSetting(key=GENERAL_CALENDAR_SETTING_KEY, value='jalali')
        db.session.add(calendar)
        db.session.flush()

        expected_jalali = format_app_datetime(expiry).split(' ', 1)[0].translate(
            str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')
        )
        self.assertIn(expected_jalali, _service_expiry(client, 'fa'))
        self.assertNotIn('2027/07/19', _service_expiry(client, 'fa'))

        calendar.value = 'gregorian'
        db.session.flush()
        expected_gregorian = format_app_datetime(expiry).split(' ', 1)[0]
        self.assertIn(expected_gregorian, _service_expiry(client, 'en'))

    def test_telegram_owned_service_drilldown_link_renewal_and_support(self):
        previous_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=False,
            enabled_languages_json='["fa","en"]', default_language='fa',
        )
        customer = CustomerAccount(
            primary_phone='989125551231', phone_verified_at=datetime.utcnow(),
            preferred_language='fa',
        )
        identity = TelegramIdentity(
            customer=customer, telegram_user_id=70010, telegram_chat_id=70010,
            phone_normalized=customer.primary_phone, phone_verified_at=datetime.utcnow(),
        )
        server = Server(name='PLUS', host='https://plus.test', username='u', password='p')
        package = Package(
            name='30GB / 30 Days', days=30, volume=30, price=320000,
            enabled=True, scope='global', display_order=1,
        )
        reviewer = Admin(
            username='claim-test-service-reviewer', role='superadmin',
            is_superadmin=True, enabled=True, telegram_id='70010',
        )
        reviewer.set_password('StrongClaimPassword123!')
        db.session.add_all([bot, customer, identity, server, package, reviewer])
        db.session.flush()
        ownership = ServiceOwnership(
            customer_id=customer.id, server_id=server.id,
            client_uuid='service-client-1', client_email_snapshot='g276-09125551231',
            verification_method='subscription', verified_at=datetime.utcnow(),
        )
        db.session.add(ownership)
        db.session.commit()
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': server.id, 'id': 11, 'clients': [{
                'id': 'service-client-1', 'email': 'g276-09125551231',
                'up': 1024 ** 3, 'down': 2 * (1024 ** 3),
                'remaining_bytes': 7 * (1024 ** 3),
                'expiryTimestamp': 0, 'service_state': 'active', 'enable': True,
                'raw_client': {
                    'id': 'service-client-1', 'email': 'g276-09125551231',
                    'subId': 'private-sub-token', 'expiryTime': 0,
                },
            }],
        }]
        api = FakeTelegramApi()
        try:
            process_update(api, bot, {'update_id': 9, 'message': {
                'message_id': 1, 'text': COPY['fa']['menu_services'],
                'from': {'id': 70010, 'first_name': 'Owner'},
                'chat': {'id': 70010, 'type': 'private'},
            }})
            list_keyboard = api.messages[-1]['reply_markup']['inline_keyboard']
            self.assertIn('PLUS', list_keyboard[0][0]['text'])
            self.assertEqual(list_keyboard[1][0]['callback_data'], f'service:{ownership.id}')

            process_update(api, bot, {'update_id': 10, 'callback_query': {
                'id': 'service-detail', 'data': f'service:{ownership.id}',
                'from': {'id': 70010, 'first_name': 'Owner'},
                'message': {'chat': {'id': 70010, 'type': 'private'}},
            }})
            self.assertIn('g276-09125551231', api.messages[-1]['text'])
            self.assertIn(COPY['fa']['status_active'], api.messages[-1]['text'])
            detail_callbacks = [
                button['callback_data']
                for row in api.messages[-1]['reply_markup']['inline_keyboard'] for button in row
            ]
            self.assertIn(f'service-renew:{ownership.id}', detail_callbacks)
            self.assertIn(f'service-support:{ownership.id}', detail_callbacks)

            with patch('telegram_bot_worker._public_base_url', return_value='https://eve.example'):
                process_update(api, bot, {'update_id': 11, 'callback_query': {
                    'id': 'service-link', 'data': f'service-link:{ownership.id}',
                    'from': {'id': 70010},
                    'message': {'chat': {'id': 70010, 'type': 'private'}},
                }})
            self.assertIn('/s/', api.messages[-1]['text'])
            self.assertIn('private-sub-token', api.messages[-1]['text'])

            process_update(api, bot, {'update_id': 12, 'callback_query': {
                'id': 'service-renew', 'data': f'service-renew:{ownership.id}',
                'from': {'id': 70010},
                'message': {'chat': {'id': 70010, 'type': 'private'}},
            }})
            self.assertEqual(
                api.messages[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data'],
                f'renew-package:{ownership.id}:{package.id}',
            )
            process_update(api, bot, {'update_id': 13, 'callback_query': {
                'id': 'renew-package', 'data': f'renew-package:{ownership.id}:{package.id}',
                'from': {'id': 70010},
                'message': {'chat': {'id': 70010, 'type': 'private'}},
            }})
            renewal = TelegramServiceRequest.query.filter_by(request_type='renewal').one()
            self.assertEqual(renewal.package_id, package.id)
            self.assertEqual(renewal.amount, 320000)
            self.assertTrue(any(
                message['text'].startswith('Telegram Renewal request')
                for message in api.messages
            ))

            process_update(api, bot, {'update_id': 131, 'callback_query': {
                'id': 'renew-package-duplicate',
                'data': f'renew-package:{ownership.id}:{package.id}',
                'from': {'id': 70010},
                'message': {'chat': {'id': 70010, 'type': 'private'}},
            }})
            self.assertEqual(
                TelegramServiceRequest.query.filter_by(request_type='renewal').count(), 1,
            )
            self.assertTrue(any(
                f'#{renewal.id}' in message['text']
                and message.get('reply_markup', {}).get('inline_keyboard', [[{}]])[0][0].get(
                    'callback_data'
                ) == f'renew-request:{renewal.id}:cancel'
                for message in api.messages
            ))
            self.assertGreaterEqual(sum(
                message['text'].startswith(f'Telegram Renewal request #{renewal.id}')
                for message in api.messages
            ), 2)

            process_update(api, bot, {'update_id': 14, 'callback_query': {
                'id': 'service-support', 'data': f'service-support:{ownership.id}',
                'from': {'id': 70010},
                'message': {'chat': {'id': 70010, 'type': 'private'}},
            }})
            process_update(api, bot, {'update_id': 15, 'message': {
                'message_id': 2, 'text': 'Please check this service.',
                'from': {'id': 70010, 'first_name': 'Owner'},
                'chat': {'id': 70010, 'type': 'private'},
            }})
            support = TelegramServiceRequest.query.filter_by(request_type='support').one()
            self.assertEqual(support.note, 'Please check this service.')
            self.assertTrue(any(
                message['text'] == COPY['fa']['support_pending'].format(request_id=support.id)
                for message in api.messages
            ))

            process_update(api, bot, {'update_id': 151, 'callback_query': {
                'id': 'service-support-follow-up', 'data': f'service-support:{ownership.id}',
                'from': {'id': 70010},
                'message': {'chat': {'id': 70010, 'type': 'private'}},
            }})
            process_update(api, bot, {'update_id': 152, 'message': {
                'message_id': 3, 'text': 'This is a follow-up message.',
                'from': {'id': 70010, 'first_name': 'Owner'},
                'chat': {'id': 70010, 'type': 'private'},
            }})
            self.assertEqual(
                TelegramServiceRequest.query.filter_by(request_type='support').count(), 1,
            )
            self.assertEqual(
                TelegramServiceRequestMessage.query.filter_by(
                    request_id=support.id, sender_type='customer',
                ).order_by(TelegramServiceRequestMessage.id.asc()).with_entities(
                    TelegramServiceRequestMessage.message,
                ).all(),
                [('Please check this service.',), ('This is a follow-up message.',)],
            )
            self.assertEqual(support.note, 'This is a follow-up message.')
            with patch(
                'telegram_bot_worker._execute_renewal_request',
                return_value=(False, 'panel unavailable'),
            ):
                process_update(api, bot, {'update_id': 159, 'callback_query': {
                    'id': 'admin-service-complete-failed',
                    'data': f'admin-service:{renewal.id}:complete',
                    'from': {'id': 70010, 'first_name': 'Admin'},
                    'message': {'chat': {'id': 70010, 'type': 'private'}},
                }})
            self.assertEqual(renewal.status, 'pending')
            self.assertIn('panel unavailable', api.messages[-1]['text'])

            with patch(
                'telegram_bot_worker._execute_renewal_request',
                return_value=(True, {'success': True}),
            ) as execute_renewal:
                process_update(api, bot, {'update_id': 16, 'callback_query': {
                    'id': 'admin-service-complete',
                    'data': f'admin-service:{renewal.id}:complete',
                    'from': {'id': 70010, 'first_name': 'Admin'},
                    'message': {'chat': {'id': 70010, 'type': 'private'}},
                }})
            execute_renewal.assert_called_once_with(renewal, reviewer)
            self.assertEqual(renewal.status, 'completed')
            self.assertEqual(renewal.reviewed_by_admin_id, reviewer.id)
            self.assertEqual(api.messages[-1]['text'], COPY['fa']['request_completed'])

            process_update(api, bot, {'update_id': 17, 'callback_query': {
                'id': 'renew-package-after-complete',
                'data': f'renew-package:{ownership.id}:{package.id}',
                'from': {'id': 70010},
                'message': {'chat': {'id': 70010, 'type': 'private'}},
            }})
            pending_again = TelegramServiceRequest.query.filter_by(
                request_type='renewal', status='pending',
            ).one()
            process_update(api, bot, {'update_id': 18, 'callback_query': {
                'id': 'renew-request-cancel',
                'data': f'renew-request:{pending_again.id}:cancel',
                'from': {'id': 70010},
                'message': {'chat': {'id': 70010, 'type': 'private'}},
            }})
            self.assertEqual(pending_again.status, 'cancelled')
            self.assertIn(f'#{pending_again.id}', api.messages[-1]['text'])
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous_inbounds

    def test_telegram_purchase_receipt_manual_approval_flow(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=False,
            enabled_languages_json='["fa","en"]', default_language='fa',
        )
        customer = CustomerAccount(
            primary_phone='989125551232', phone_verified_at=datetime.utcnow(),
            preferred_language='fa',
        )
        identity = TelegramIdentity(
            customer=customer, telegram_user_id=70011, telegram_chat_id=70011,
            phone_normalized=customer.primary_phone, phone_verified_at=datetime.utcnow(),
        )
        server = Server(name='BUY', host='https://buy.test', username='u', password='p')
        package = Package(
            name='20GB / 30 Days', days=30, volume=20, price=230000,
            enabled=True, scope='global', display_order=1,
        )
        card = BankCard(
            label='Main card', bank_name='Test Bank', owner_name='Eve Owner',
            card_number='6037997512345678', is_active=True,
        )
        reviewer = Admin(
            username='claim-test-purchase-reviewer', role='superadmin',
            is_superadmin=True, enabled=True, telegram_id='70011',
        )
        reviewer.set_password('StrongClaimPassword123!')
        db.session.add_all([bot, customer, identity, server, package, card, reviewer])
        db.session.flush()
        db.session.add(TelegramPurchasePolicy(
            bot_instance_id=bot.id, customer_selects_server=False,
            assignment_strategy='least_clients', account_name_mode='generated',
        ))
        db.session.add(TelegramPurchaseServerRule(
            bot_instance_id=bot.id, server_id=server.id, eligible=True,
            customer_visible=False, priority=1, weight=1,
        ))
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 20, 'message': {
            'message_id': 1, 'text': COPY['fa']['menu_buy_service'],
            'from': {'id': 70011, 'first_name': 'Buyer'},
            'chat': {'id': 70011, 'type': 'private'},
        }})
        package_callbacks = [
            button['callback_data']
            for row in api.messages[-1]['reply_markup']['inline_keyboard'] for button in row
        ]
        self.assertIn(f'buy-package:0:{package.id}', package_callbacks)
        process_update(api, bot, {'update_id': 22, 'callback_query': {
            'id': 'buy-package', 'data': f'buy-package:0:{package.id}',
            'from': {'id': 70011},
            'message': {'chat': {'id': 70011, 'type': 'private'}},
        }})
        self.assertIn('6037 9975 1234 5678', api.messages[-1]['text'])
        state = TelegramBotUserState.query.filter_by(
            bot_instance_id=bot.id, telegram_user_id=70011,
        ).one()
        self.assertEqual(state.step, 'awaiting_purchase_receipt')

        process_update(api, bot, {'update_id': 23, 'message': {
            'message_id': 44,
            'photo': [{
                'file_id': 'receipt-file-id', 'file_unique_id': 'receipt-unique-id',
                'file_size': 120000,
            }],
            'from': {'id': 70011, 'first_name': 'Buyer'},
            'chat': {'id': 70011, 'type': 'private'},
        }})
        request_row = TelegramPurchaseRequest.query.one()
        self.assertEqual(request_row.status, 'pending')
        self.assertEqual(request_row.amount, 230000)
        self.assertEqual(request_row.server_id, server.id)
        self.assertTrue(request_row.detail.account_name.startswith(f'tg{request_row.id}-'))
        self.assertEqual(request_row.source_message_id, 44)
        self.assertEqual(api.media[-1], {
            'kind': 'photo', 'chat_id': 70011, 'file_id': 'receipt-file-id',
        })
        self.assertTrue(any(
            message['text'].startswith('Telegram purchase request')
            for message in api.messages
        ))

        # Approved orders created by an older worker may not have their frozen
        # provisioning detail. The retry path must repair that metadata safely.
        db.session.delete(request_row.detail)
        db.session.flush()
        db.session.expire(request_row, ['detail'])
        self.assertIsNone(request_row.detail)
        repaired_detail = _ensure_purchase_detail(request_row)
        self.assertTrue(repaired_detail.account_name.startswith(f'tg{request_row.id}-'))

        with patch(
            'telegram_bot_worker._execute_purchase_request',
            return_value=(False, 'panel temporarily unavailable'),
        ):
            process_update(api, bot, {'update_id': 24, 'callback_query': {
                'id': 'admin-purchase-approve-failed',
                'data': f'admin-purchase:{request_row.id}:approve',
                'from': {'id': 70011, 'first_name': 'Admin'},
                'message': {'chat': {'id': 70011, 'type': 'private'}},
            }})
        self.assertEqual(request_row.status, 'approved')
        self.assertTrue(any(
            message.get('reply_markup', {}).get('inline_keyboard', [[{}]])[0][0].get(
                'callback_data'
            ) == f'admin-purchase:{request_row.id}:approve'
            for message in api.messages
        ))
        self.assertEqual(request_row.reviewed_by_admin_id, reviewer.id)
        self.assertEqual(api.messages[-1]['text'], COPY['fa']['purchase_approved'])

        with patch(
            'telegram_bot_worker._execute_purchase_request',
            return_value=(True, {'client': {
                'dashboard_link': 'https://eve.example/s/1/new-subscription',
            }}),
        ) as execute_purchase:
            process_update(api, bot, {'update_id': 25, 'callback_query': {
                'id': 'admin-purchase-approve-retry',
                'data': f'admin-purchase:{request_row.id}:approve',
                'from': {'id': 70011, 'first_name': 'Admin'},
                'message': {'chat': {'id': 70011, 'type': 'private'}},
            }})
        execute_purchase.assert_called_once_with(request_row, reviewer)
        self.assertEqual(request_row.status, 'completed')
        self.assertIn(request_row.detail.account_name, api.messages[-1]['text'])
        self.assertIn('https://eve.example/s/1/new-subscription', api.messages[-1]['text'])

    def test_v3_add_client_verifies_ambiguous_empty_200_response(self):
        server = MagicMock(id=1, host='https://panel.example/base')
        session_obj = MagicMock()
        empty = MagicMock(status_code=200, content=b'', headers={}, text='')
        verified_payload = {
            'success': True,
            'obj': {'client': {'email': 'tg2-2411', 'id': 'client-uuid'}},
        }
        verified = MagicMock(
            status_code=200,
            content=b'{"success":true}',
            headers={'Content-Type': 'application/json'},
            text='{"success":true}',
        )
        verified.json.return_value = verified_payload
        session_obj.post.return_value = empty
        session_obj.get.return_value = verified

        ok, result, error = v3_add_client(
            server, session_obj,
            {'email': 'tg2-2411', 'id': 'client-uuid'}, [31, 32],
        )

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertTrue(result['verified_after_empty_response'])
        self.assertEqual(session_obj.post.call_count, 1)
        self.assertEqual(session_obj.get.call_count, 1)

    def test_v3_add_client_empty_200_stays_failed_when_client_missing(self):
        server = MagicMock(id=1, host='https://panel.example/base')
        session_obj = MagicMock()
        session_obj.post.return_value = MagicMock(
            status_code=200, content=b'', headers={}, text='',
        )
        missing = MagicMock(
            status_code=200,
            content=b'{"success":false}',
            headers={'Content-Type': 'application/json'},
            text='{"success":false}',
        )
        missing.json.return_value = {'success': False, 'msg': 'not found'}
        session_obj.get.return_value = missing

        ok, _result, error = v3_add_client(
            server, session_obj,
            {'email': 'tg2-2411', 'id': 'client-uuid'}, [31],
        )

        self.assertFalse(ok)
        self.assertIn('client was not found after verification', error)

    def test_telegram_purchase_excludes_listener_only_inbounds(self):
        previous_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = [
            {'server_id': 1, 'id': 27, 'protocol': 'socks', 'enable': True},
            {'server_id': 1, 'id': 31, 'protocol': 'shadowsocks', 'enable': True},
            {'server_id': 1, 'id': 32, 'protocol': 'shadowsocks', 'enable': True},
            {'server_id': 1, 'id': 39, 'protocol': 'mixed', 'enable': True},
            {'server_id': 1, 'id': 40, 'protocol': 'vless', 'enable': False},
            {'server_id': 2, 'id': 99, 'protocol': 'vless', 'enable': True},
        ]
        try:
            self.assertEqual(_purchase_provisioning_inbound_ids(1), [31, 32])
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous_inbounds

    def test_telegram_v3_auto_detect_keeps_recurring_valid_assignment_packs(self):
        server = Server(name='Detect v3', host='https://detect-v3.test', username='u', password='p')
        db.session.add(server)
        db.session.commit()
        previous_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = [
            {'server_id': server.id, 'id': 31, 'protocol': 'shadowsocks', 'enable': True},
            {'server_id': server.id, 'id': 32, 'protocol': 'vless', 'enable': True},
            {'server_id': server.id, 'id': 39, 'protocol': 'mixed', 'enable': True},
            {'server_id': server.id, 'id': 40, 'protocol': 'vless', 'enable': False},
        ]
        payload = {'success': True, 'obj': [
            {'email': 'a', 'inboundIds': [31, 32, 39]},
            {'email': 'b', 'inboundIds': [32, 31]},
            {'email': 'c', 'inboundIds': [31, 40]},
            {'email': 'd', 'inboundIds': []},
        ]}
        try:
            with patch('app.load_snapshot_from_redis'), \
                    patch('app.get_xui_session', return_value=(MagicMock(), None)), \
                    patch('app.server_is_v3', return_value=True), \
                    patch('app._v3_get', return_value=(True, payload, None)):
                profiles = _detect_telegram_inbound_profiles(server)
            self.assertEqual(profiles, [{
                'inbound_ids': [31, 32], 'client_count': 2, 'signature': '31,32',
            }])
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous_inbounds

    def test_telegram_purchase_route_enforces_legacy_single_inbound_and_freezes_retry(self):
        bot = TelegramBotInstance(scope_key='route-test', display_name='Route test')
        package = Package(name='Route package', days=30, volume=10, price=1000, enabled=True)
        server = Server(name='Legacy route', host='https://legacy-route.test', username='u', password='p')
        customer = CustomerAccount(primary_phone='989121112233', phone_verified_at=datetime.utcnow())
        db.session.add_all([bot, package, server, customer])
        db.session.flush()
        route = TelegramPurchaseInboundRoute(
            bot_instance_id=bot.id, package_id=package.id, server_id=server.id,
            mode='manual', inbound_ids_json='[31,32]', enabled=True,
        )
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id, telegram_user_id=90123, customer_id=customer.id,
            server_id=server.id, package_id=package.id, amount=1000,
            receipt_file_id='receipt', source_chat_id=90123, source_message_id=1,
        )
        db.session.add_all([route, request_row])
        db.session.commit()
        valid = [{'id': 31}, {'id': 32}]
        with patch('telegram_bot_worker._telegram_customer_inbounds', return_value=valid), \
                patch('telegram_bot_worker.get_xui_session', return_value=(MagicMock(), None)), \
                patch('telegram_bot_worker.server_is_v3', return_value=False):
            inbound_ids, error = _ensure_purchase_inbound_allocation(request_row)
            self.assertIsNone(inbound_ids)
            self.assertIn('exactly one', error)
            route.inbound_ids_json = '[31]'
            db.session.flush()
            inbound_ids, error = _ensure_purchase_inbound_allocation(request_row)
            self.assertEqual(inbound_ids, [31])
            self.assertIsNone(error)
        route.inbound_ids_json = '[32]'
        db.session.flush()
        inbound_ids, error = _ensure_purchase_inbound_allocation(request_row)
        self.assertEqual(inbound_ids, [31])
        self.assertIsNone(error)

    def test_telegram_v3_auto_route_uses_weighted_pack_and_freezes_it(self):
        bot = TelegramBotInstance(scope_key='auto-route-test', display_name='Auto route')
        package = Package(name='Auto package', days=30, volume=20, price=2000, enabled=True)
        server = Server(name='Auto v3', host='https://auto-v3.test', username='u', password='p')
        customer = CustomerAccount(primary_phone='989121112244', phone_verified_at=datetime.utcnow())
        db.session.add_all([bot, package, server, customer])
        db.session.flush()
        route = TelegramPurchaseInboundRoute(
            bot_instance_id=bot.id, package_id=package.id, server_id=server.id,
            mode='auto_detect', inbound_ids_json='[]', enabled=True,
        )
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id, telegram_user_id=90124, customer_id=customer.id,
            server_id=server.id, package_id=package.id, amount=2000,
            receipt_file_id='receipt', source_chat_id=90124, source_message_id=2,
        )
        db.session.add_all([route, request_row])
        db.session.commit()
        profiles = [
            {'inbound_ids': [31, 32], 'client_count': 8, 'signature': '31,32'},
            {'inbound_ids': [34, 35], 'client_count': 3, 'signature': '34,35'},
        ]
        with patch('telegram_bot_worker._telegram_customer_inbounds', return_value=[
                    {'id': 31}, {'id': 32}, {'id': 34}, {'id': 35},
                ]), \
                patch('telegram_bot_worker.get_xui_session', return_value=(MagicMock(), None)), \
                patch('telegram_bot_worker.server_is_v3', return_value=True), \
                patch('telegram_bot_worker._detect_telegram_inbound_profiles', return_value=profiles), \
                patch('telegram_bot_worker.random.choices', return_value=[profiles[1]]) as chooser:
            inbound_ids, error = _ensure_purchase_inbound_allocation(request_row)
        self.assertEqual(inbound_ids, [34, 35])
        self.assertIsNone(error)
        self.assertEqual(chooser.call_args.kwargs['weights'], [8, 3])
        with patch('telegram_bot_worker._detect_telegram_inbound_profiles', side_effect=AssertionError('must not redetect')):
            inbound_ids, error = _ensure_purchase_inbound_allocation(request_row)
        self.assertEqual(inbound_ids, [34, 35])
        self.assertIsNone(error)

    def test_telegram_generated_account_name_supports_username_and_full_phone(self):
        bot = TelegramBotInstance(scope_key='name-token-test', display_name='Name token')
        server = Server(name='Name token server', host='https://name-token.test', username='u', password='p')
        customer = CustomerAccount(primary_phone='989195292411', phone_verified_at=datetime.utcnow())
        db.session.add_all([bot, server, customer])
        db.session.flush()
        identity = TelegramIdentity(
            customer_id=customer.id,
            telegram_user_id=215614184,
            username='navid_test',
            phone_normalized='989195292411',
            phone_verified_at=datetime.utcnow(),
        )
        policy = TelegramPurchasePolicy(
            bot_instance_id=bot.id,
            account_name_mode='generated',
            account_name_template='tg-{telegram_username}-{phone}',
        )
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id, telegram_user_id=identity.telegram_user_id,
            customer_id=customer.id, server_id=server.id, amount=1000,
            receipt_file_id='receipt', source_chat_id=identity.telegram_user_id,
            source_message_id=3,
        )
        db.session.add_all([identity, policy, request_row])
        db.session.flush()
        self.assertEqual(
            _render_purchase_account_name(bot, request_row, customer, None),
            'tg-navid_test-09195292411',
        )
        identity.username = None
        policy.account_name_template = 'tg-{telegram_username}-{phone_last4}'
        db.session.flush()
        self.assertEqual(
            _render_purchase_account_name(bot, request_row, customer, None),
            'tg-user215614184-2411',
        )

    def test_telegram_customer_server_and_account_name_policy_flow(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Policy Bot', enabled=True, test_mode=False,
            enabled_languages_json='["fa"]', default_language='fa',
        )
        customer = CustomerAccount(
            primary_phone='989125551233', phone_verified_at=datetime.utcnow(),
            preferred_language='fa',
        )
        identity = TelegramIdentity(
            customer=customer, telegram_user_id=70013, telegram_chat_id=70013,
            phone_normalized=customer.primary_phone, phone_verified_at=datetime.utcnow(),
        )
        visible_server = Server(
            name='Internal A', host='https://visible.test', username='u', password='p',
        )
        hidden_server = Server(
            name='Internal B', host='https://hidden.test', username='u', password='p',
        )
        package = Package(
            name='Policy Package', days=30, volume=10, price=130000,
            enabled=True, scope='global', display_order=0,
        )
        card = BankCard(label='Policy Card', card_number='6037997512345678', is_active=True)
        db.session.add_all([bot, customer, identity, visible_server, hidden_server, package, card])
        db.session.flush()
        db.session.add(TelegramPurchasePolicy(
            bot_instance_id=bot.id, customer_selects_server=True,
            assignment_strategy='priority', account_name_mode='customer',
            account_name_template='tg{order_id}-{phone_last4}',
        ))
        db.session.add_all([
            TelegramPurchaseServerRule(
                bot_instance_id=bot.id, server_id=visible_server.id, eligible=True,
                customer_visible=True, display_name='Germany Premium', priority=1, weight=1,
            ),
            TelegramPurchaseServerRule(
                bot_instance_id=bot.id, server_id=hidden_server.id, eligible=True,
                customer_visible=False, display_name='Hidden', priority=2, weight=1,
            ),
        ])
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 30, 'message': {
            'message_id': 1, 'text': COPY['fa']['menu_buy_service'],
            'from': {'id': 70013}, 'chat': {'id': 70013, 'type': 'private'},
        }})
        buttons = [
            button for row in api.messages[-1]['reply_markup']['inline_keyboard'] for button in row
        ]
        self.assertEqual([button['callback_data'] for button in buttons], [
            f'buy-server:{visible_server.id}',
        ])
        self.assertIn('Germany Premium', buttons[0]['text'])

        process_update(api, bot, {'update_id': 31, 'callback_query': {
            'id': 'server', 'data': f'buy-server:{visible_server.id}',
            'from': {'id': 70013}, 'message': {'chat': {'id': 70013, 'type': 'private'}},
        }})
        process_update(api, bot, {'update_id': 32, 'callback_query': {
            'id': 'package', 'data': f'buy-package:{visible_server.id}:{package.id}',
            'from': {'id': 70013}, 'message': {'chat': {'id': 70013, 'type': 'private'}},
        }})
        self.assertEqual(api.messages[-1]['text'], COPY['fa']['purchase_account_name_prompt'])
        process_update(api, bot, {'update_id': 33, 'message': {
            'message_id': 2, 'text': 'نام نامعتبر',
            'from': {'id': 70013}, 'chat': {'id': 70013, 'type': 'private'},
        }})
        self.assertEqual(api.messages[-1]['text'], COPY['fa']['purchase_account_name_invalid'])
        process_update(api, bot, {'update_id': 34, 'message': {
            'message_id': 3, 'text': 'navid_01',
            'from': {'id': 70013}, 'chat': {'id': 70013, 'type': 'private'},
        }})
        self.assertIn('6037 9975 1234 5678', api.messages[-1]['text'])
        process_update(api, bot, {'update_id': 35, 'message': {
            'message_id': 4,
            'photo': [{'file_id': 'customer-name-receipt', 'file_size': 10}],
            'from': {'id': 70013}, 'chat': {'id': 70013, 'type': 'private'},
        }})
        request_row = TelegramPurchaseRequest.query.one()
        self.assertEqual(request_row.server_id, visible_server.id)
        self.assertEqual(request_row.detail.account_name, 'navid_01')

    def test_telegram_rejects_contact_belonging_to_another_user(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=False,
            enabled_languages_json='["fa"]', default_language='fa',
        )
        db.session.add(bot)
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 5, 'message': {
            'message_id': 2,
            'from': {'id': 70003, 'first_name': 'Ali'},
            'chat': {'id': 70003, 'type': 'private'},
            'contact': {'user_id': 99999, 'phone_number': '09195292411'},
        }})
        db.session.commit()

        identity = TelegramIdentity.query.filter_by(telegram_user_id=70003).one()
        self.assertIsNone(identity.customer_id)
        self.assertEqual(CustomerAccount.query.count(), 0)
        self.assertIn('امنیت', api.messages[-1]['text'])

    def test_telegram_does_not_reassign_phone_owned_by_another_identity(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=False,
            enabled_languages_json='["fa"]', default_language='fa',
        )
        customer = CustomerAccount(primary_phone='989195292411', phone_verified_at=datetime.utcnow())
        original = TelegramIdentity(
            customer=customer, telegram_user_id=70004, telegram_chat_id=70004,
            phone_normalized=customer.primary_phone, phone_verified_at=datetime.utcnow(),
        )
        db.session.add_all([bot, customer, original])
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 6, 'message': {
            'message_id': 2,
            'from': {'id': 70005, 'first_name': 'Other'},
            'chat': {'id': 70005, 'type': 'private'},
            'contact': {'user_id': 70005, 'phone_number': '09195292411'},
        }})
        db.session.commit()

        claimant = TelegramIdentity.query.filter_by(telegram_user_id=70005).one()
        state = TelegramBotUserState.query.filter_by(telegram_user_id=70005).one()
        self.assertIsNone(claimant.customer_id)
        self.assertEqual(original.customer_id, customer.id)
        self.assertEqual(state.step, 'needs_review')

    def test_telegram_discovers_phone_clients_and_deduplicates_inbounds(self):
        customer = CustomerAccount(primary_phone='989125551234', phone_verified_at=datetime.utcnow())
        identity = TelegramIdentity(
            customer=customer, telegram_user_id=70006, telegram_chat_id=70006,
            phone_normalized=customer.primary_phone, phone_verified_at=datetime.utcnow(),
        )
        server = Server(
            name='Discovery Test', host='https://discovery.test', username='u', password='p',
        )
        db.session.add_all([customer, identity, server])
        db.session.commit()
        previous = GLOBAL_SERVER_DATA.get('inbounds')
        client = {
            'id': 'discovery-client-uuid', 'subId': 'discovery-sub-token',
            'email': 'g276-09125551234',
        }
        GLOBAL_SERVER_DATA['inbounds'] = [
            {'server_id': server.id, 'id': 1, 'clients': [dict(client)]},
            {'server_id': server.id, 'id': 2, 'clients': [dict(client)]},
            {'server_id': server.id, 'id': 3, 'clients': [
                {'id': 'other-client', 'subId': 'other', 'email': 'g277-09120000000'},
            ]},
        ]
        try:
            with patch('app.load_snapshot_from_redis', return_value=False):
                claim = discover_phone_ownership_claim(identity)
                db.session.commit()
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous

        self.assertIsNotNone(claim)
        self.assertEqual(len(claim.items), 1)
        self.assertEqual(claim.items[0].client_uuid, 'discovery-client-uuid')
        self.assertEqual(claim.items[0].match_score, 100)

    def test_subscription_link_proves_claim_without_storing_secret(self):
        customer = CustomerAccount(primary_phone='989125551235', phone_verified_at=datetime.utcnow())
        identity = TelegramIdentity(
            customer=customer, telegram_user_id=70007, telegram_chat_id=70007,
            phone_normalized=customer.primary_phone, phone_verified_at=datetime.utcnow(),
        )
        server = Server(
            name='Proof Test', host='https://proof.test', username='u', password='p',
        )
        db.session.add_all([customer, identity, server])
        db.session.commit()
        secret_sub_id = 'proof-secret-sub-token'
        previous = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': server.id, 'id': 1, 'clients': [{
                'id': 'proof-client-uuid', 'subId': secret_sub_id,
                'email': 'g278-9125551235',
            }],
        }]
        try:
            with patch('app.load_snapshot_from_redis', return_value=False):
                claim = discover_phone_ownership_claim(identity)
                item = claim.items[0]
                rejected = verify_ownership_claim_subscription(item, customer.id, 'wrong-token')
                accepted = verify_ownership_claim_subscription(item, customer.id, secret_sub_id)
                db.session.commit()
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous

        ownership = ServiceOwnership.query.filter_by(
            server_id=server.id, client_uuid='proof-client-uuid',
        ).one()
        self.assertFalse(rejected['success'])
        self.assertTrue(accepted['success'])
        self.assertEqual(ownership.customer_id, customer.id)
        self.assertEqual(ownership.verification_method, 'subscription_link')
        self.assertTrue(item.subscription_verified)
        self.assertNotIn(secret_sub_id, str(item.__dict__))
        self.assertEqual(
            _extract_subscription_token(f'https://eve.example/s/{server.id}/{secret_sub_id}'),
            secret_sub_id,
        )
        self.assertEqual(_extract_subscription_token('not-a-link'), '')
        self.assertEqual(_extract_subscription_token('https://user:pass@eve.example/s/secret'), '')

    def test_admin_can_review_claim_from_telegram_without_tester_allowlist(self):
        bot = TelegramBotInstance(
            scope_key='system', display_name='Test', enabled=True, test_mode=True,
            enabled_languages_json='["fa"]', default_language='fa',
        )
        customer = CustomerAccount(primary_phone='989125551236', phone_verified_at=datetime.utcnow())
        identity = TelegramIdentity(
            customer=customer, telegram_user_id=70008, telegram_chat_id=70008,
            phone_normalized=customer.primary_phone, phone_verified_at=datetime.utcnow(),
        )
        server = Server(name='Admin Review Test', host='https://review.test', username='u', password='p')
        reviewer = Admin(
            username='claim-test-telegram-reviewer', role='superadmin', is_superadmin=True,
            enabled=True, telegram_id='80008',
        )
        reviewer.set_password('StrongClaimPassword123!')
        db.session.add_all([bot, customer, identity, server, reviewer])
        db.session.flush()
        claim = OwnershipClaim(
            customer_id=customer.id, telegram_identity_id=identity.id,
            verified_phone=customer.primary_phone, status='pending', claim_method='admin_review',
        )
        db.session.add(claim)
        db.session.flush()
        item = OwnershipClaimItem(
            claim_id=claim.id, server_id=server.id, client_uuid='admin-review-client',
            client_email_snapshot='g279-09125551236', status='pending',
        )
        db.session.add(item)
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 7, 'callback_query': {
            'id': 'admin-callback', 'data': f'admin-claim:{item.id}:approve',
            'from': {'id': 80008, 'first_name': 'Admin'},
            'message': {'chat': {'id': 80008, 'type': 'private'}},
        }})

        ownership = ServiceOwnership.query.filter_by(
            server_id=server.id, client_uuid='admin-review-client',
        ).one()
        self.assertEqual(ownership.customer_id, customer.id)
        self.assertEqual(item.status, 'approved')
        self.assertEqual(api.callbacks, [('admin-callback', 'Saved')])

    def test_new_client_is_appended_as_latest_user_in_every_target_inbound(self):
        previous = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = [
            {'server_id': 10, 'id': 7, 'clients': [{'email': 'g275'}], 'active_count': 1},
            {'server_id': 10, 'id': 8, 'clients': [{'email': 'old'}], 'active_count': 1},
        ]
        raw_client = {
            'id': 'uuid-g276', 'email': 'g276', 'enable': True,
            'expiryTime': 0, 'totalGB': 0, 'comment': '',
        }
        try:
            changed = add_cached_client(10, [7, 8], raw_client, publish=False)
            self.assertTrue(changed)
            self.assertEqual(GLOBAL_SERVER_DATA['inbounds'][0]['clients'][-1]['email'], 'g276')
            self.assertEqual(GLOBAL_SERVER_DATA['inbounds'][1]['clients'][-1]['email'], 'g276')
            self.assertEqual(GLOBAL_SERVER_DATA['inbounds'][0]['client_count'], 2)
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous

    def test_new_client_write_through_is_idempotent(self):
        previous = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': 10, 'id': 7,
            'clients': [{'id': 'uuid-g276', 'email': 'g276'}],
        }]
        try:
            changed = add_cached_client(
                10, [7], {'id': 'uuid-g276', 'email': 'g276'}, publish=False,
            )
            self.assertFalse(changed)
            self.assertEqual(len(GLOBAL_SERVER_DATA['inbounds'][0]['clients']), 1)
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous

    def test_live_counter_prevents_hourly_rollup_blind_spot(self):
        recommendation = _build_subscription_package_recommendation(
            999999,
            'live-only-account',
            PACKAGES,
            terminal=True,
            live_usage={
                'total_bytes': 10 * 1024 ** 3,
                'volume_limit_bytes': 10 * 1024 ** 3,
                'expiry_ts_ms': 0,
                'observed_at': datetime.utcnow(),
            },
        )
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation['package_id'], 1)
        self.assertEqual(recommendation['model_version'], 'usage-fit-v3')

    def test_high_live_usage_is_visible_and_truthfully_labeled(self):
        recommendation = _build_subscription_package_recommendation(
            999999,
            'high-usage-live-account',
            PACKAGES,
            terminal=True,
            live_usage={
                'total_bytes': 100 * 1024 ** 3,
                'volume_limit_bytes': 100 * 1024 ** 3,
                'expiry_ts_ms': 0,
                'observed_at': datetime.utcnow(),
            },
        )
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation['package_id'], 4)
        self.assertTrue(recommendation['capacity_limited'])

    def test_all_callers_fall_back_to_shared_live_snapshot(self):
        previous = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': 777777,
            'clients': [{
                'subId': 'template-account',
                'up': 2 * 1024 ** 3,
                'down': 8 * 1024 ** 3,
                'totalGB': 10 * 1024 ** 3,
                'expiryTimestamp': 0,
            }],
        }]
        try:
            recommendation = _build_subscription_package_recommendation(
                777777, 'template-account', PACKAGES, terminal=True,
            )
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation['package_id'], 1)

    def test_zero_usage_still_has_no_recommendation(self):
        recommendation = _build_subscription_package_recommendation(
            999999,
            'never-used-account',
            PACKAGES,
            live_usage={'total_bytes': 0},
        )
        self.assertIsNone(recommendation)

    def test_live_fallback_survives_rollup_schema_failure(self):
        with patch.object(RenewalEvent.query_class, 'filter_by', side_effect=RuntimeError('migration pending')):
            recommendation = _build_subscription_package_recommendation(
                999999,
                'schema-recovery-account',
                PACKAGES,
                terminal=True,
                live_usage={
                    'total_bytes': 10 * 1024 ** 3,
                    'volume_limit_bytes': 10 * 1024 ** 3,
                    'observed_at': datetime.utcnow(),
                },
            )
        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation['package_id'], 1)

    def test_cancel_sms_via_gmweb_uses_request_id_endpoint(self):
        class FakeResponse:
            status_code = 200
            content = b'{}'

            @staticmethod
            def json():
                return {
                    'ok': True,
                    'requestId': 'send_123',
                    'status': 'cancelled',
                    'state': 'cancelled',
                    'terminal': True,
                }

        with patch('app.requests.post', return_value=FakeResponse()) as post:
            result = _cancel_sms_via_gmweb(
                'send_123',
                {'base_url': 'https://gmweb.test', 'api_key': 'gmw_secret', 'timeout_seconds': 7},
            )

        self.assertTrue(result['cancelled'])
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://gmweb.test/send/cancel/send_123')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer gmw_secret')

    def test_disable_cancel_marks_pending_gateway_and_local_sms(self):
        for key, value in (
            (SMS_GMWEB_BASE_URL_KEY, 'https://gmweb.test'),
            (SMS_GMWEB_API_KEY_KEY, 'gmw_secret'),
            (SMS_GMWEB_TIMEOUT_KEY, '7'),
        ):
            db.session.merge(SystemConfig(key=key, value=value))
        db.session.add(SmsSendLog(
            email='g326-0912464258',
            server_id=8,
            server_name='ECO1',
            state='ended',
            recipient='9891***258',
            status='queued',
            request_id='send_123',
            gateway_job_id='413',
            terminal=False,
            segment_count=2,
            message_encoding='UCS-2',
        ))
        db.session.add(PendingSms(
            email='g326-0912464258',
            server_id=8,
            server_name='ECO1',
            event_name='renew',
            recipient='+98912464258',
            text='pending text',
        ))
        db.session.commit()

        class FakeResponse:
            status_code = 200
            content = b'{}'

            @staticmethod
            def json():
                return {
                    'ok': True,
                    'requestId': 'send_123',
                    'status': 'cancelled',
                    'state': 'cancelled',
                    'terminal': True,
                }

        with patch('app.requests.post', return_value=FakeResponse()) as post:
            result = _cancel_pending_sms_for_account(8, 'g326-0912464258')

        self.assertEqual(result['gateway_cancelled'], 1)
        self.assertEqual(result['local_cancelled'], 1)
        post.assert_called_once()

        gateway_row = SmsSendLog.query.filter_by(request_id='send_123').one()
        self.assertEqual(gateway_row.status, 'cancelled')
        self.assertEqual(gateway_row.gateway_state, 'cancelled')
        self.assertEqual(gateway_row.stage, 'cancelled_by_eve')
        self.assertTrue(gateway_row.terminal)
        self.assertFalse(gateway_row.successful)
        self.assertEqual(PendingSms.query.count(), 0)
        self.assertEqual(SmsSendLog.query.filter_by(status='cancelled').count(), 2)

    def test_renew_cancel_only_stale_automation_sms(self):
        for key, value in (
            (SMS_GMWEB_BASE_URL_KEY, 'https://gmweb.test'),
            (SMS_GMWEB_API_KEY_KEY, 'gmw_secret'),
            (SMS_GMWEB_TIMEOUT_KEY, '7'),
        ):
            db.session.merge(SystemConfig(key=key, value=value))
        rows = [
            SmsSendLog(
                email='stale-user',
                server_id=12,
                server_name='ECO2',
                state='ended',
                recipient='9891***001',
                status='queued',
                request_id='send_stale',
                terminal=False,
                segment_count=1,
            ),
            SmsSendLog(
                email='stale-user',
                server_id=12,
                server_name='ECO2',
                state='renew',
                recipient='9891***001',
                status='queued',
                request_id='send_renew',
                terminal=False,
                segment_count=1,
            ),
            SmsSendLog(
                email='stale-user',
                server_id=12,
                server_name='ECO2',
                state='created',
                recipient='9891***001',
                status='queued',
                request_id='send_created',
                terminal=False,
                segment_count=1,
            ),
        ]
        db.session.add_all(rows)
        db.session.add(PendingSms(
            email='stale-user',
            server_id=12,
            server_name='ECO2',
            event_name='renew',
            recipient='+98910000001',
            text='new renew confirmation',
        ))
        db.session.commit()

        class FakeResponse:
            status_code = 200
            content = b'{}'

            @staticmethod
            def json():
                return {'ok': True, 'status': 'cancelled', 'state': 'cancelled', 'terminal': True}

        with patch('app.requests.post', return_value=FakeResponse()) as post:
            result = _cancel_stale_account_sms(12, 'stale-user', reason='renew_success')

        self.assertEqual(result['gateway_cancelled'], 1)
        self.assertEqual(result['local_cancelled'], 0)
        post.assert_called_once()
        args, _kwargs = post.call_args
        self.assertEqual(args[0], 'https://gmweb.test/send/cancel/send_stale')

        self.assertEqual(SmsSendLog.query.filter_by(request_id='send_stale').one().status, 'cancelled')
        self.assertEqual(SmsSendLog.query.filter_by(request_id='send_renew').one().status, 'queued')
        self.assertEqual(SmsSendLog.query.filter_by(request_id='send_created').one().status, 'queued')
        self.assertEqual(PendingSms.query.filter_by(event_name='renew').count(), 1)

    def test_sms_scan_stops_before_sending_when_gateway_unpaired(self):
        for key, value in (
            (SMS_GMWEB_BASE_URL_KEY, 'https://gmweb.test'),
            (SMS_GMWEB_API_KEY_KEY, 'gmw_secret'),
            (SMS_GMWEB_TIMEOUT_KEY, '7'),
            ('sms_automation_enabled', 'true'),
        ):
            db.session.merge(SystemConfig(key=key, value=value))
        db.session.commit()

        class FakeResponse:
            status_code = 503

        with patch('app.requests.get', return_value=FakeResponse()) as get, \
             patch('app._send_sms_via_gmweb') as send:
            ready, reason, status = _sms_gateway_ready({
                'base_url': 'https://gmweb.test',
                'api_key': 'gmw_secret',
                'timeout_seconds': 7,
            })
            result = _run_sms_depletion_scan(
                job_id='unpaired-test',
                triggered_by='manual',
                states=['low_volume', 'ended'],
            )

        self.assertFalse(ready)
        self.assertEqual(reason, 'gateway_not_paired')
        self.assertEqual(status, 503)
        self.assertEqual(result['reason'], 'gateway_not_paired')
        send.assert_not_called()
        get.assert_called()

    def test_sms_gateway_429_keeps_rate_limit_details(self):
        class FakeResponse:
            status_code = 429
            content = b'{}'
            headers = {'retry-after': '60'}

            def json(self):
                return {
                    'error': 'send_rate_limited',
                    'reason': 'per_minute_limit',
                    'limits': {'minute': 10, 'hour': 100},
                    'used': {'minute': 10, 'hour': 10},
                }

        with patch('app.requests.post', return_value=FakeResponse()):
            result = _send_sms_via_gmweb(
                '989120000000',
                'hello',
                {
                    'base_url': 'https://gmweb.test',
                    'api_key': 'gmw_secret',
                    'timeout_seconds': 7,
                },
            )

        self.assertFalse(result['sent'])
        self.assertEqual(result['status_code'], 429)
        self.assertIn('send_rate_limited', result['reason'])
        self.assertIn('per_minute_limit', result['reason'])
        self.assertIn('minute 10/10', result['reason'])
        self.assertIn('retry_after=60s', result['reason'])

    def test_sms_daily_limit_counts_completed_segments_only(self):
        db.session.add_all([
            SmsSendLog(
                email='failed-unpaired',
                server_id=1,
                server_name='ECO1',
                state='low_volume',
                recipient='9891***001',
                status='failed',
                gateway_state='checking_paired',
                stage='failed',
                request_id='send_failed',
                terminal=True,
                successful=False,
                segment_count=2,
            ),
            SmsSendLog(
                email='queued-user',
                server_id=1,
                server_name='ECO1',
                state='near_expiry',
                recipient='9891***002',
                status='queued',
                gateway_state='queued',
                stage='queued',
                request_id='send_queued',
                terminal=False,
                segment_count=3,
            ),
            SmsSendLog(
                email='completed-user',
                server_id=1,
                server_name='ECO1',
                state='ended',
                recipient='9891***003',
                status='sent',
                gateway_state='completed',
                stage='completed',
                request_id='send_completed',
                terminal=True,
                successful=True,
                segment_count=1,
            ),
            SmsSendLog(
                email='legacy-sent-user',
                server_id=1,
                server_name='ECO1',
                state='renew',
                recipient='9891***004',
                status='sent',
                stage='sent',
                terminal=True,
                segment_count=1,
            ),
            SmsSendLog(
                email='skipped-user',
                server_id=1,
                server_name='ECO1',
                state='expired',
                recipient='9891***005',
                status='skipped',
                gateway_state='daily_limit_reached',
                stage='skipped',
                terminal=True,
                successful=False,
                segment_count=4,
            ),
        ])
        db.session.commit()

        stats = _sms_db_segment_stats_today()

        self.assertEqual(stats['submitted'], 6)
        self.assertEqual(stats['completed'], 2)
        self.assertEqual(stats['failed'], 6)
        self.assertEqual(stats['inflight'], 3)
        self.assertEqual(_sms_db_segments_used_today(), 2)
        self.assertTrue(_sms_reserve_daily_segments(1, 3))
        self.assertFalse(_sms_reserve_daily_segments(2, 3))


    def _make_telegram_operation_records(self, suffix='ops'):
        reviewer = Admin(
            username=f'claim-test-{suffix}-admin', role='superadmin',
            is_superadmin=True, enabled=True,
        )
        reviewer.set_password('StrongClaimPassword123!')
        server = Server(
            name=f'Operation Server {suffix}', host=f'https://{suffix}.example.test',
            username='u', password='p', enabled=True,
        )
        package = Package(name=f'Operation Package {suffix}', days=30, volume=20, price=230000)
        customer = CustomerAccount(
            display_name='Telegram Customer', primary_phone='989195292411', preferred_language='fa',
        )
        bot = TelegramBotInstance(
            scope_key=f'operation-{suffix}', display_name='Operation Bot', enabled=True,
            test_mode=True, token_encrypted='encrypted-token',
        )
        db.session.add_all([reviewer, server, package, customer, bot])
        db.session.flush()
        identity = TelegramIdentity(
            customer_id=customer.id, telegram_user_id=215614184,
            telegram_chat_id=215614184, username='mahna_test', phone_normalized='989195292411',
        )
        ownership = ServiceOwnership(
            customer_id=customer.id, server_id=server.id,
            client_uuid=f'client-{suffix}', client_email_snapshot=f'g-{suffix}-09195292411',
            verification_method='admin', verified_at=datetime.utcnow(),
        )
        db.session.add_all([identity, ownership])
        db.session.flush()
        support = TelegramServiceRequest(
            bot_instance_id=bot.id, telegram_user_id=identity.telegram_user_id,
            customer_id=customer.id, service_ownership_id=ownership.id,
            request_type='support', note='I need help', status='pending',
        )
        purchase = TelegramPurchaseRequest(
            bot_instance_id=bot.id, telegram_user_id=identity.telegram_user_id,
            customer_id=customer.id, server_id=server.id, package_id=package.id,
            amount=230000, receipt_file_id='receipt-file', receipt_kind='photo',
            source_chat_id=identity.telegram_chat_id, source_message_id=99, status='pending',
        )
        db.session.add_all([support, purchase])
        db.session.flush()
        db.session.add_all([
            TelegramServiceRequestMessage(
                request_id=support.id, sender_type='customer', message='I need help',
            ),
            TelegramPurchaseRequestDetail(
                request_id=purchase.id, account_name=f'tg-{suffix}-2411',
                allocation_strategy='least_clients',
            ),
        ])
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reviewer.id
        return client, reviewer, bot, support, purchase

    def test_telegram_operations_inbox_lists_purchase_and_support(self):
        client, _, _, support, purchase = self._make_telegram_operation_records('inbox')

        response = client.get('/api/telegram-operations')
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['success'])
        page = client.get('/telegram-operations')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Telegram Operations', page.data)
        self.assertEqual({item['kind'] for item in payload['items']}, {'purchase', 'support'})
        self.assertEqual(payload['counters']['waiting_review'], 1)
        self.assertEqual(payload['counters']['open_support'], 1)
        support_item = next(item for item in payload['items'] if item['id'] == support.id
                            and item['kind'] == 'support')
        purchase_item = next(item for item in payload['items'] if item['id'] == purchase.id
                             and item['kind'] == 'purchase')
        self.assertEqual(support_item['messages'][0]['message'], 'I need help')
        self.assertEqual(purchase_item['telegram_username'], 'mahna_test')
        self.assertIn(f'/purchases/{purchase.id}/receipt', purchase_item['receipt_url'])

    def test_telegram_support_reply_is_delivered_and_persisted(self):
        client, reviewer, _, support, _ = self._make_telegram_operation_records('reply')
        api = FakeTelegramApi()

        with patch('app._telegram_bot_api_client', return_value=api):
            response = client.post(
                f'/api/telegram-operations/services/{support.id}/reply',
                json={'message': 'Your service was checked.', 'complete': True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        db.session.refresh(support)
        self.assertEqual(support.status, 'completed')
        saved = TelegramServiceRequestMessage.query.filter_by(
            request_id=support.id, sender_type='admin', admin_id=reviewer.id,
        ).one()
        self.assertEqual(saved.message, 'Your service was checked.')
        self.assertIn('Your service was checked.', api.messages[0]['text'])

    def test_telegram_support_reply_accepts_attachment_and_exposes_conversation_state(self):
        client, reviewer, _, support, _ = self._make_telegram_operation_records('reply-file')
        api = FakeTelegramApi()

        with patch('app._telegram_bot_api_client', return_value=api):
            response = client.post(
                f'/api/telegram-operations/services/{support.id}/reply',
                data={
                    'message': 'Please use this screenshot.',
                    'complete': 'false',
                    'attachment': (io.BytesIO(b'png-bytes'), 'answer.png'),
                },
                content_type='multipart/form-data',
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()['item']
        self.assertEqual(payload['support_state'], 'waiting_customer')
        self.assertEqual(payload['telegram_private_url'], 'https://t.me/mahna_test')
        saved = TelegramServiceRequestMessage.query.filter_by(
            request_id=support.id, sender_type='admin', admin_id=reviewer.id,
        ).one()
        self.assertEqual(saved.attachment_file_id, 'uploaded-photo')
        self.assertEqual(saved.attachment_name, 'answer.png')
        self.assertEqual(api.uploads[0]['chat_id'], 215614184)
        self.assertEqual(api.uploads[0]['caption'], 'Please use this screenshot.')

        api.download_file = MagicMock(return_value=(
            b'png-bytes', 'image/png', 'answer.png', 'test-route',
        ))
        with patch('app._telegram_bot_api_client', return_value=api):
            attachment = client.get(
                f'/api/telegram-operations/services/{support.id}/messages/{saved.id}/attachment',
            )
        self.assertEqual(attachment.status_code, 200)
        self.assertEqual(attachment.data, b'png-bytes')
        self.assertIn('inline', attachment.headers.get('Content-Disposition', ''))
        self.assertIn('no-store', attachment.headers.get('Cache-Control', ''))
        api.download_file.assert_called_once_with('uploaded-photo', max_bytes=20 * 1024 * 1024)

    def test_telegram_admin_can_reply_to_support_with_media_and_customer_can_view_history(self):
        _, reviewer, bot, support, _ = self._make_telegram_operation_records('bot-support')
        reviewer.telegram_id = '9001'
        bot.test_mode = True
        db.session.add(TelegramBotTestUser(
            bot_instance_id=bot.id, telegram_user_id=215614184,
            label='support customer', enabled=True,
        ))
        db.session.commit()
        api = FakeTelegramApi()

        process_update(api, bot, {'update_id': 301, 'callback_query': {
            'id': 'admin-reply', 'data': f'admin-support:{support.id}:reply',
            'from': {'id': 9001},
            'message': {'chat': {'id': 9001, 'type': 'private'}},
        }})
        process_update(api, bot, {'update_id': 302, 'message': {
            'message_id': 77, 'caption': 'Annotated answer',
            'photo': [{'file_id': 'small'}, {
                'file_id': 'admin-photo', 'file_unique_id': 'admin-photo-u',
                'file_size': 1200, 'width': 600, 'height': 400,
            }],
            'from': {'id': 9001, 'first_name': 'Admin'},
            'chat': {'id': 9001, 'type': 'private'},
        }})

        self.assertTrue(any(copy['chat_id'] == 215614184 and copy['message_id'] == 77
                            for copy in api.copies))
        saved = TelegramServiceRequestMessage.query.filter_by(
            request_id=support.id, sender_type='admin', admin_id=reviewer.id,
        ).one()
        self.assertEqual(saved.message, 'Annotated answer')
        self.assertEqual(saved.attachment_file_id, 'admin-photo')

        process_update(api, bot, {'update_id': 303, 'message': {
            'message_id': 78, 'text': COPY['fa']['menu_support_requests'],
            'from': {'id': 215614184, 'first_name': 'Customer'},
            'chat': {'id': 215614184, 'type': 'private'},
        }})
        self.assertTrue(any(
            message.get('reply_markup', {}).get('inline_keyboard', [[{}]])[0][0].get(
                'callback_data'
            ) == f'support-ticket:{support.id}'
            for message in api.messages
        ))
        process_update(api, bot, {'update_id': 304, 'callback_query': {
            'id': 'customer-ticket', 'data': f'support-ticket:{support.id}',
            'from': {'id': 215614184},
            'message': {'chat': {'id': 215614184, 'type': 'private'}},
        }})
        self.assertTrue(any('Annotated answer' in message['text'] for message in api.messages))
        self.assertTrue(any(media['chat_id'] == 215614184 and media['file_id'] == 'admin-photo'
                            for media in api.media))

    def test_telegram_operations_page_supports_deep_link_and_thread_composer(self):
        client, _, _, support, _ = self._make_telegram_operation_records('support-ui')
        page = client.get(f'/telegram-operations?kind=support&request={support.id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'initialRequest', page.data)
        self.assertIn(b'Open Telegram PV', page.data)
        self.assertIn(b'Attach image or file', page.data)
        self.assertIn(b'Send & close', page.data)
        self.assertNotIn(b'Send & complete', page.data)
        self.assertIn(b"part.type !== 'era'", page.data)

    def test_telegram_purchase_approve_provisions_and_notifies_customer(self):
        client, reviewer, _, _, purchase = self._make_telegram_operation_records('approve')
        api = FakeTelegramApi()
        provision_result = {'client': {'sub_link': 'https://example.test/sub/created'}}

        with patch('telegram_bot_worker._execute_purchase_request',
                   return_value=(True, provision_result)) as execute, \
                patch('app._telegram_bot_api_client', return_value=api):
            response = client.post(
                f'/api/telegram-operations/purchases/{purchase.id}/approve', json={},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        db.session.refresh(purchase)
        self.assertEqual(purchase.status, 'completed')
        execute.assert_called_once_with(purchase, reviewer)
        self.assertIn('https://example.test/sub/created', api.messages[0]['text'])

    def test_telegram_purchase_receipt_streams_without_cache(self):
        client, _, _, _, purchase = self._make_telegram_operation_records('receipt')
        api = MagicMock()
        api.download_file.return_value = (b'receipt-bytes', 'image/jpeg', 'receipt.jpg', 'proxy')

        with patch('app._telegram_bot_api_client', return_value=api):
            response = client.get(
                f'/api/telegram-operations/purchases/{purchase.id}/receipt',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'receipt-bytes')
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))
        self.assertEqual(response.headers.get('X-Content-Type-Options'), 'nosniff')
        api.download_file.assert_called_once_with('receipt-file')

    def test_telegram_operations_reseller_scope_blocks_unowned_requests(self):
        client, reviewer, bot, support, purchase = self._make_telegram_operation_records('scope')
        reviewer.role = 'reseller'
        reviewer.is_superadmin = False
        db.session.commit()

        blocked = client.get('/api/telegram-operations').get_json()
        blocked_receipt = client.get(
            f'/api/telegram-operations/purchases/{purchase.id}/receipt',
        )
        self.assertEqual(blocked['items'], [])
        self.assertEqual(blocked_receipt.status_code, 403)

        bot.owner_admin_id = reviewer.id
        support.ownership.reseller_id = reviewer.id
        db.session.commit()
        allowed = client.get('/api/telegram-operations').get_json()
        self.assertEqual(len(allowed['items']), 2)


if __name__ == '__main__':
    unittest.main()
