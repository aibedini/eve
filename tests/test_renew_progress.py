"""Durable progress is private, read-only, and distinct from SMS delivery."""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_db.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _db.name.replace(os.sep, '/'))
os.environ.setdefault('SESSION_SECRET', 'renew-progress-test')
os.environ.setdefault('SERVER_PASSWORD_KEY', base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['FLASK_ENV'] = 'development'

import app as app_module
from panel.extensions import db
from panel.models import Admin, ClientOperation, SmsSendLog
from panel.services import renew_progress, renew_notifications, client_operations


class ProgressContractTests(unittest.TestCase):
    def operation(self, **overrides):
        values = dict(idempotency_key='renew-test', server_id=1, inbound_id=2,
                      client_email='account@example.com', state='completed', amount=100,
                      expected_json='{}', response_json=json.dumps({
                          'business_finalized': True, 'final_state': 'APPLIED_ACTIVE',
                          'copy_text': 'customer info', 'renew_sms_sent_at': 'now'}),
                      created_at=datetime(2026, 11, 14), updated_at=datetime(2026, 11, 14), error=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_worker_scheduled_is_not_gateway_acceptance_or_delivery(self):
        view = renew_progress.snapshot(self.operation())
        self.assertEqual(view['sms']['status'], 'sending')
        self.assertTrue(view['confirmed'])
        self.assertFalse(view['sms']['retryable'])
        self.assertTrue(view['created_at'].endswith('+00:00'))

    def test_list_summary_does_not_repeat_customer_text_or_template_variables(self):
        view = renew_progress.snapshot(self.operation(), detail=False)
        self.assertNotIn('copy_text', view)
        self.assertNotIn('tpl_vars', view)
        self.assertNotIn('timing', view)

    def test_delivery_failure_never_turns_renewal_red(self):
        row = SimpleNamespace(carrier_state=None, status='failed', terminal=True, successful=False)
        view = renew_progress.snapshot(self.operation(), row)
        self.assertTrue(view['confirmed'])
        self.assertTrue(view['attention'])
        self.assertTrue(view['sms']['retryable'])
        row.terminal = None
        self.assertFalse(renew_progress.snapshot(self.operation(), row)['sms']['retryable'])

    def test_unconfirmed_operation_never_exposes_customer_confirmation(self):
        view = renew_progress.snapshot(self.operation(response_json=json.dumps({
            'copy_text': 'stale successful text', 'verify': 'invalid legacy shape'}),
            state='needs_reconciliation'))
        self.assertFalse(view['confirmed'])
        self.assertIsNone(view['copy_text'])
        self.assertTrue(view['attention'])
        self.assertFalse(view['pending'])

    def test_gateway_and_carrier_status_are_distinct(self):
        row = SimpleNamespace(carrier_state=None, status='sent', terminal=True, successful=True)
        self.assertEqual(renew_progress.snapshot(self.operation(), row)['sms']['status'], 'accepted')
        row.carrier_state = 'delivered'
        self.assertEqual(renew_progress.snapshot(self.operation(), row)['sms']['status'], 'delivered')
        row.carrier_state = 'failed'
        self.assertEqual(renew_progress.snapshot(self.operation(), row)['sms']['status'], 'failed')
        self.assertFalse(renew_progress.snapshot(self.operation(), row)['sms']['retryable'])

    def test_gateway_identity_is_stable_and_changes_only_for_explicit_retry(self):
        op = self.operation()
        original = renew_notifications.notification_key(op)
        self.assertEqual(original, renew_notifications.notification_key(op))
        self.assertNotIn(op.client_email, original)
        op.response_json = json.dumps({'business_finalized': True, 'renew_sms_attempt': 1})
        self.assertNotEqual(original, renew_notifications.notification_key(op))


class ProgressRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = app_module.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()

    def setUp(self):
        ClientOperation.query.delete()
        SmsSendLog.query.delete()
        Admin.query.delete()
        self.admin = Admin(username='progress-owner', password_hash='x', role='superadmin', is_superadmin=True)
        self.other = Admin(username='progress-other', password_hash='x', role='superadmin', is_superadmin=True)
        db.session.add_all([self.admin, self.other])
        db.session.commit()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session.update(admin_id=self.admin.id, admin_username=self.admin.username,
                           role='superadmin', is_superadmin=True)

    def operation(self, key='mine', owner=None, state='completed'):
        op = ClientOperation(idempotency_key=key, request_hash='h', action='renew',
                             admin_id=(owner or self.admin).id, server_id=None, inbound_id=2,
                             client_email='client@example.com', state=state, amount=100,
                             response_json=json.dumps({'business_finalized': True,
                                 'final_state': 'APPLIED_ACTIVE', 'copy_text': 'info',
                                 'tpl_vars': {'email': 'client@example.com'},
                                 'renew_sms_sent_at': 'scheduled'}))
        db.session.add(op)
        db.session.commit()
        return op

    def test_progress_reads_only_the_owners_ledger_and_never_calls_panel(self):
        mine = self.operation()
        self.operation('someone-else', self.other)
        before = mine.response_json
        with patch('app.get_xui_session', side_effect=AssertionError('progress must not call panel')):
            response = self.client.get('/api/client-operations/renewals')
            self.assertEqual(response.status_code, 200)
            self.assertEqual([row['operation_id'] for row in response.json['operations']], ['mine'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(self.client.get('/api/client-operations/renewals/someone-else').status_code, 404)
        self.assertEqual(mine.response_json, before)

    def test_ownership_revocation_hides_ledger_and_detail(self):
        self.operation()
        with patch('panel.routes.clients._renew_operation_accessible', return_value=False):
            self.assertEqual(self.client.get('/api/client-operations/renewals').json['operations'], [])
            self.assertEqual(self.client.get('/api/client-operations/renewals/mine').status_code, 404)

    def test_retry_is_blocked_for_unknown_or_inflight_sms(self):
        self.operation()
        with patch.object(renew_notifications, 'send_renewal_confirmation_once') as sender:
            response = self.client.post('/api/client-operations/renewals/mine/retry-sms')
            self.assertEqual(response.json['code'], 'sms_not_retryable')
            self.assertFalse(response.json['success'])
            sender.assert_not_called()

    def test_failed_sms_retry_is_operation_scoped_and_reserved_once(self):
        op = self.operation()
        row = SmsSendLog(email=op.client_email, server_id=0, state='renew', status='failed',
                         terminal=True, successful=False, eve_notification_id=renew_progress.notification_id(op))
        db.session.add(row)
        db.session.commit()
        before_amount = op.amount
        with patch.object(renew_notifications, 'send_renewal_confirmation_once', return_value={'sent': True}) as sender, \
                patch('panel.jobs.renew_activation._default_read_layers',
                      return_value=lambda: SimpleNamespace(final_state='APPLIED_ACTIVE')):
            response = self.client.post('/api/client-operations/renewals/mine/retry-sms')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['success'])
            self.assertEqual(sender.call_count, 1)
            duplicate = self.client.post('/api/client-operations/renewals/mine/retry-sms')
            self.assertEqual(duplicate.json['code'], 'sms_not_retryable')
            self.assertFalse(duplicate.json['success'])
            self.assertEqual(sender.call_count, 1)
        db.session.refresh(op)
        self.assertEqual(op.amount, before_amount)
        self.assertEqual(op.state, 'completed')
        self.assertEqual(ClientOperation.query.count(), 1)

    def test_completion_preserves_scheduled_notification_marker(self):
        op = self.operation()
        client_operations.complete_client_operation(op, {'business_finalized': True, 'final_state': 'APPLIED_ACTIVE'})
        self.assertEqual(client_operations.operation_response(op)['renew_sms_sent_at'], 'scheduled')

    def test_historical_success_cannot_send_sms_when_current_activation_is_unknown(self):
        op = self.operation()
        db.session.add(SmsSendLog(email=op.client_email, server_id=0, state='renew', status='failed',
                                 terminal=True, successful=False,
                                 eve_notification_id=renew_progress.notification_id(op)))
        db.session.commit()
        with patch('panel.jobs.renew_activation._default_read_layers', return_value=None), \
                patch.object(renew_notifications, 'send_renewal_confirmation_once') as sender:
            response = self.client.post('/api/client-operations/renewals/mine/retry-sms')
            self.assertFalse(response.json['success'])
            self.assertEqual(response.json['code'], 'sms_activation_not_confirmed')
            sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
