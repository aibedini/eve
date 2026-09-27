"""Recovery endpoints require an operator preview and never submit SMS."""
import unittest
from unittest import mock

from tests.test_telemetry_state_transitions import app_module, db
from panel.models import Admin, ServiceNotificationEvent
from panel.services import notification_recovery


class RecoveryRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username='recovery-route-admin', password_hash='x',
                          role='superadmin', is_superadmin=True, enabled=True)
        db.session.add(cls.admin)
        db.session.commit()
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
            sess['role'] = 'superadmin'
            sess['is_superadmin'] = True

    def test_preview_requires_authentication(self):
        self.assertNotEqual(self.client.get('/api/sms/recovery/preview').status_code, 200)

    def test_preview_is_bounded_and_activation_requires_signed_token(self):
        self._login()
        sample = {'service_key': 'test-key', 'state': 'expired', 'generation': 1,
                  'state_version': 2, 'recoverable': True}
        with mock.patch.object(notification_recovery, 'preview', return_value={
                'window': '7d', 'current_terminal_accounts': 1, 'inspected': 1,
                'truncated': False, 'counts': {}, 'candidates': [sample]}), \
             mock.patch.object(notification_recovery, 'activate', return_value='created') as activate:
            response = self.client.get('/api/sms/recovery/preview?window=7d')
            self.assertEqual(response.status_code, 200)
            token = response.json['candidates'][0]['preview_token']
            self.assertEqual(activate.call_count, 0)
            forged = self.client.post('/api/sms/recovery/activate', json={
                'preview_tokens': ['forged']})
            self.assertFalse(forged.json['success'])
            over_limit = self.client.post('/api/sms/recovery/activate', json={
                'preview_tokens': [token] * 21})
            self.assertFalse(over_limit.json['success'])
            result = self.client.post('/api/sms/recovery/activate', json={
                'preview_tokens': [token]})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['direct_sends'], 0)
            self.assertEqual(result.json['created'], 1)
            activate.assert_called_once()

    def test_coverage_diagnostic_is_read_only_and_rejects_invalid_bucket(self):
        self._login()
        result = {'current_terminal_accounts': 0, 'counts': {}, 'by_state': {},
                  'known_age_missing_obligations': 0,
                  'unknown_age_missing_obligations': 0,
                  'historical_missing_obligations': 0,
                  'fresh_gap_count': 0,
                  'selected_known_age_missing': 0,
                  'oldest_known_missing_age_seconds': None,
                  'needs_notification': 0, 'selected_total': 0,
                  'offset': 0, 'has_more': False, 'rows': []}
        with mock.patch.object(notification_recovery, 'accounting', return_value=result) as census:
            response = self.client.get('/api/sms/notification-coverage?bucket=missing_obligation')
            self.assertTrue(response.json['success'])
            self.assertEqual(response.json['fresh_gap_count'], 0)
            census.assert_called_once()
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        invalid = self.client.get('/api/sms/notification-coverage?bucket=made_up')
        self.assertFalse(invalid.json['success'])

    def test_retry_now_wakes_same_obligation_without_direct_send(self):
        self._login()
        event = ServiceNotificationEvent(
            event_id='test-retry-now', service_key='test-retry-service', server_id=4,
            state='volume_ended', notification_kind='volume_ended',
            state_version=1, lifecycle_generation=0, source='transition',
            status='gateway_accepted', attempt_count=2,
            gateway_request_id='existing-gateway-request')
        db.session.add(event)
        db.session.commit()
        try:
            response = self.client.post(
                '/api/sms/notification-obligations/test-retry-now/retry-now')
            self.assertTrue(response.json['success'])
            self.assertEqual(response.json['direct_sends'], 0)
            self.assertEqual(response.json['action'], 'reconcile_existing_request')
            self.assertEqual(ServiceNotificationEvent.query.filter_by(
                service_key='test-retry-service').count(), 1)
            self.assertEqual(event.status, 'gateway_accepted')
            self.assertIsNotNone(event.next_attempt_at)
        finally:
            db.session.delete(event)
            db.session.commit()

    def test_waiting_gateway_uses_real_worker_reason_codes(self):
        self._login()
        event = ServiceNotificationEvent(
            event_id='test-gateway-wait', service_key='test-gateway-service', server_id=4,
            state='expired', notification_kind='expired', state_version=1,
            lifecycle_generation=0, source='transition', status='retry',
            attempt_count=1, last_error='gateway_not_paired')
        db.session.add(event)
        db.session.commit()
        try:
            response = self.client.get('/api/sms/notification-debt?filter=waiting_gateway')
            self.assertTrue(response.json['success'])
            self.assertEqual(response.json['obligation_summary']['waiting_gateway'], 1)
            self.assertEqual(response.json['obligations_total'], 1)
            self.assertEqual(response.json['obligations'][0]['event_id'], event.event_id)
        finally:
            db.session.delete(event)
            db.session.commit()
