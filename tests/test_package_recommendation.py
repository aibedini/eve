import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    CustomerAccount,
    GLOBAL_SERVER_DATA,
    PendingSms,
    RenewalEvent,
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
        CustomerAccount.query.delete()
        SmsSendLog.query.delete()
        PendingSms.query.delete()
        SystemConfig.query.filter(SystemConfig.key.in_([
            SMS_GMWEB_BASE_URL_KEY,
            SMS_GMWEB_API_KEY_KEY,
            SMS_GMWEB_TIMEOUT_KEY,
        ])).delete(synchronize_session=False)
        db.session.commit()

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
