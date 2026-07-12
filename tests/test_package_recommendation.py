import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from sqlalchemy.exc import IntegrityError
from cryptography.fernet import Fernet


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    Admin,
    CustomerAccount,
    GLOBAL_SERVER_DATA,
    PendingSms,
    RenewalEvent,
    OwnershipClaim,
    OwnershipClaimItem,
    Server,
    ServiceDelegation,
    ServiceOwnership,
    TelegramIdentity,
    TelegramBotInstance,
    TelegramProxyEndpoint,
    SMS_GMWEB_API_KEY_KEY,
    SMS_GMWEB_BASE_URL_KEY,
    SMS_GMWEB_TIMEOUT_KEY,
    SmsSendLog,
    SystemConfig,
    app,
    db,
    add_cached_client,
    _build_subscription_package_recommendation,
    _cancel_pending_sms_for_account,
    _cancel_sms_via_gmweb,
    _cancel_stale_account_sms,
    _run_sms_depletion_scan,
    _sms_db_segment_stats_today,
    _sms_db_segments_used_today,
    _sms_gateway_ready,
    _sms_reserve_daily_segments,
    _select_subscription_package,
    normalize_iran_mobile,
    review_ownership_claim_item,
    _telegram_bot_diagnostic,
    _telegram_proxy_from_payload,
)


PACKAGES = [
    {'id': 1, 'name': '10 GB', 'days': 31, 'volume': 10, 'price': 150_000},
    {'id': 2, 'name': '20 GB', 'days': 31, 'volume': 20, 'price': 270_000},
    {'id': 3, 'name': '30 GB', 'days': 31, 'volume': 30, 'price': 390_000},
    {'id': 4, 'name': '50 GB', 'days': 31, 'volume': 50, 'price': 600_000},
]


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
        TelegramProxyEndpoint.query.delete()
        TelegramBotInstance.query.delete()
        OwnershipClaimItem.query.delete()
        OwnershipClaim.query.delete()
        TelegramIdentity.query.delete()
        ServiceDelegation.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        SmsSendLog.query.delete()
        PendingSms.query.delete()
        SystemConfig.query.filter(SystemConfig.key.in_([
            SMS_GMWEB_BASE_URL_KEY,
            SMS_GMWEB_API_KEY_KEY,
            SMS_GMWEB_TIMEOUT_KEY,
        ])).delete(synchronize_session=False)
        Admin.query.filter(Admin.username.like('claim-test-%')).delete(synchronize_session=False)
        db.session.commit()

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

            with patch('app._telegram_get_me', side_effect=fake_get_me):
                result = _telegram_bot_diagnostic(bot, route='configured')

        self.assertTrue(result['success'])
        self.assertEqual(result['route'], 'direct')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(proxy.health_status, 'failed')
        self.assertEqual(bot.bot_username, 'eve_test_bot')

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
            loaded = client.get('/api/settings/telegram-bots').get_json()
            settings_page = client.get('/settings')

        self.assertTrue(saved['success'])
        self.assertTrue(loaded['success'])
        self.assertTrue(loaded['bot']['token_configured'])
        self.assertNotIn('bot_token', loaded['bot'])
        self.assertNotIn('token_encrypted', loaded['bot'])
        self.assertEqual(settings_page.status_code, 200)
        self.assertIn(b'tab-telegram_bots', settings_page.data)

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


if __name__ == '__main__':
    unittest.main()
