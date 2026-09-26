"""Recovery endpoints require an operator preview and never submit SMS."""
import unittest
from unittest import mock

from tests.test_telemetry_state_transitions import app_module, db
from panel.models import Admin
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
