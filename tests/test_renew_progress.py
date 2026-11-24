"""Durable progress is private, read-only, and distinct from SMS delivery."""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
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
from panel.extensions import db, limiter
from panel.models import Admin, ClientOperation, SmsSendLog, SmsGatewayEvent
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

    def test_explicit_gateway_queue_is_never_promoted_by_generic_success_flags(self):
        from panel.jobs.messaging import _sms_accepted_status
        self.assertEqual(_sms_accepted_status({'status':'queued', 'request_id':'gateway-one',
            'terminal':True, 'successful':True}), 'queued')

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

    def test_manual_review_and_queue_never_become_automatic_retryable_failures(self):
        row = SimpleNamespace(carrier_state=None, status='manual_review', terminal=True, successful=False,
                              request_id='gateway-1', reason='unverified_manual_review')
        view = renew_progress.snapshot(self.operation(), row)
        self.assertEqual(view['sms']['status'], 'manual_review')
        self.assertFalse(view['sms']['retryable'])
        row.status = 'queued'; row.terminal = False
        view = renew_progress.snapshot(self.operation(), row)
        self.assertEqual(view['sms']['status'], 'queued')
        self.assertEqual(view['sms']['request_id'], 'gateway-1')
        self.assertTrue(view['pending'])
        row.successful = True
        self.assertEqual(renew_progress.snapshot(self.operation(), row)['sms']['status'], 'queued')

    def test_verified_unscheduled_sms_does_not_claim_to_be_waiting_for_account_verification(self):
        view = renew_progress.snapshot(self.operation(response_json=json.dumps({
            'business_finalized': True, 'final_state': 'APPLIED_ACTIVE', 'tpl_vars': {'days': 30}})))
        self.assertEqual(view['sms']['status'], 'dispatch_pending')

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
        limiter.reset()
        db.session.remove()
        ClientOperation.query.delete()
        SmsSendLog.query.delete()
        SmsGatewayEvent.query.delete()
        Admin.query.delete()
        self.admin = Admin(username='progress-owner', password_hash='x', role='superadmin', is_superadmin=True)
        self.other = Admin(username='progress-other', password_hash='x', role='superadmin', is_superadmin=True)
        db.session.add_all([self.admin, self.other])
        db.session.commit()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session.update(admin_id=self.admin.id, admin_username=self.admin.username,
                           role='superadmin', is_superadmin=True)

    def tearDown(self):
        limiter.reset()
        db.session.remove()

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
            self.assertEqual(response.json['history']['time_range'], 'all')
            self.assertEqual(response.json['history']['scope'], 'current_user')
            self.assertEqual(self.client.get('/api/client-operations/renewals/someone-else').status_code, 404)
        self.assertEqual(mine.response_json, before)

    def test_progress_polling_outlives_the_old_500_per_hour_default(self):
        self.operation()
        for url in ('/api/client-operations/renewals', '/api/client-operations/renewals/mine'):
            for _ in range(520):
                response = self.client.get(url)
                status = int(response.headers.get('X-Eve-Status') or response.status_code)
                self.assertEqual(status, 200, (url, status))
                self.assertTrue(response.json['success'])

    def test_sms_recheck_reads_only_the_matching_gateway_record_without_resending(self):
        op = self.operation()
        row = SmsSendLog(email=op.client_email, server_id=0, state='renew', status='queued',
                         request_id='gateway-one',terminal=False, eve_notification_id=renew_progress.notification_id(op))
        db.session.add(row); db.session.commit()
        before = op.response_json
        with patch('panel.jobs.messaging._refresh_pending_sms_statuses', return_value=0) as refresh, \
                patch.object(renew_notifications, 'send_renewal_confirmation_once') as send:
            response = self.client.post('/api/client-operations/renewals/mine/recheck-sms')
            self.assertTrue(response.json['success'])
            self.assertTrue(response.json['gateway_checked'])
            refresh.assert_called_once_with(log_id=row.id)
            send.assert_not_called()
        self.assertEqual(op.response_json, before)
        self.assertEqual(response.json['operation']['sms']['status'], 'queued')

    def test_sms_recheck_distinguishes_gateway_failure_from_renewal_failure(self):
        op = self.operation()
        db.session.add(SmsSendLog(email=op.client_email,server_id=0,state='renew',status='queued',
            request_id='gateway-one',terminal=False,eve_notification_id=renew_progress.notification_id(op)))
        db.session.commit()
        with patch('panel.jobs.messaging._refresh_pending_sms_statuses', side_effect=RuntimeError('unavailable')):
            response = self.client.post('/api/client-operations/renewals/mine/recheck-sms')
            self.assertFalse(response.json['success'])
            self.assertEqual(response.json['code'], 'sms_gateway_status_unavailable')
        self.assertTrue(renew_progress.snapshot(op)['confirmed'])
        self.assertEqual(self.client.post('/api/client-operations/renewals/not-mine/recheck-sms').status_code, 404)

    def test_confirmed_opt_out_status_rechecks_locally_without_a_gateway_request(self):
        op = self.operation()
        db.session.add(SmsSendLog(email=op.client_email, server_id=0, state='renew', status='skipped',
            reason='opted_out_recheck', eve_notification_id=renew_progress.notification_id(op)))
        db.session.commit()
        with patch('panel.jobs.messaging._refresh_pending_sms_statuses') as refresh:
            response = self.client.post('/api/client-operations/renewals/mine/recheck-sms')
            refresh.assert_not_called()
        self.assertTrue(response.json['success'])
        self.assertFalse(response.json['gateway_checked'])
        self.assertTrue(response.json['operation']['confirmed'])
        self.assertFalse(response.json['operation']['sms']['retryable'])

    def test_explicit_recheck_reads_delivery_receipt_after_gateway_acceptance(self):
        from panel.jobs import messaging
        op = self.operation()
        row = SmsSendLog(email=op.client_email,server_id=0,state='renew',status='sent',request_id='gateway-one',
                        terminal=True,successful=True,eve_notification_id=renew_progress.notification_id(op))
        other = SmsSendLog(email='unrelated',server_id=0,state='renew',status='queued',request_id='gateway-two',terminal=False)
        db.session.add_all([row,other]);db.session.commit()
        result = SimpleNamespace(status_code=200,json=lambda:{'status':'sent','terminal':True,'successful':True,
            'verificationStatus':'confirmed','carrierStatus':{'status':'delivered','evidence':'carrier_receipt'}})
        with patch.object(messaging,'_get_sms_runtime_settings',return_value={}), \
                patch.object(messaging,'_get_sms_provider_settings',return_value={
                    'base_url':'https://gateway.invalid','api_key':'test-only','timeout_seconds':15}), \
                patch.object(messaging.requests,'get',return_value=result) as read:
            response = self.client.post('/api/client-operations/renewals/mine/recheck-sms')
            self.assertTrue(response.json['success'])
            self.assertEqual(response.json['operation']['sms']['status'],'delivered')
            self.assertEqual(read.call_count,1)
            self.assertIn('gateway-one',read.call_args.args[0])
            self.assertEqual(read.call_args.kwargs['timeout'],8)
        db.session.refresh(other)
        self.assertEqual(other.status,'queued')

    def test_ownership_revocation_hides_ledger_and_detail(self):
        self.operation()
        with patch('panel.routes.clients._renew_operation_accessible', return_value=False):
            self.assertEqual(self.client.get('/api/client-operations/renewals').json['operations'], [])
            self.assertEqual(self.client.get('/api/client-operations/renewals/mine').status_code, 404)

    def test_background_poll_reads_carrier_delivery_after_terminal_gateway_send(self):
        from panel.jobs import messaging
        op = self.operation()
        row = SmsSendLog(email=op.client_email, server_id=0, state='renew', status='sent',
            request_id='accepted-one', terminal=True, successful=True, carrier_state='pending',
            eve_notification_id=renew_progress.notification_id(op))
        delivered = SmsSendLog(email='done', server_id=0, state='renew', status='sent',
            request_id='already-delivered', terminal=True, successful=True, carrier_state='delivered')
        db.session.add_all([row, delivered]); db.session.commit()
        result = SimpleNamespace(status_code=200, json=lambda: {'status':'sent', 'terminal':True,
            'successful':True, 'verificationStatus':'confirmed', 'carrierStatus':{'status':'delivered'}})
        with patch.object(messaging, '_get_sms_runtime_settings', return_value={}), \
                patch.object(messaging, '_get_sms_provider_settings', return_value={
                    'base_url':'https://gateway.invalid', 'api_key':'test'}), \
                patch.object(messaging.requests, 'get', return_value=result) as read:
            messaging._refresh_pending_sms_statuses()
            self.assertEqual(read.call_count, 1)
            self.assertIn('accepted-one', read.call_args.args[0])
            self.assertEqual(renew_progress.snapshot(op, row)['sms']['status'], 'delivered')
            messaging._refresh_pending_sms_statuses()
            self.assertEqual(read.call_count, 1, 'completed delivery must stop polling')

    def test_missing_old_delivery_report_cannot_turn_a_confirmed_send_into_failure(self):
        from panel.jobs import messaging
        op = self.operation()
        row = SmsSendLog(email=op.client_email, server_id=0, state='renew', status='sent',
            request_id='old-confirmed', terminal=True, successful=True,
            created_at=datetime.utcnow() - timedelta(hours=7))
        db.session.add(row); db.session.commit()
        with patch.object(messaging, '_get_sms_runtime_settings', return_value={}), \
                patch.object(messaging.requests, 'get') as read:
            messaging._refresh_pending_sms_statuses()
            read.assert_not_called()
        self.assertTrue(row.successful)
        self.assertEqual(row.carrier_state, 'unavailable')
        self.assertEqual(renew_progress.snapshot(op, row)['sms']['status'], 'accepted')
        self.assertFalse(renew_progress.snapshot(op, row)['sms']['retryable'])

    def test_renewal_reads_the_signed_receipt_used_by_sms_center(self):
        op = self.operation()
        row = SmsSendLog(email=op.client_email, server_id=0, state='renew', status='queued',
            request_id='signed-message', terminal=False,
            eve_notification_id=renew_progress.notification_id(op))
        event = SmsGatewayEvent(event_id='receipt-one', trace_id='trace-one',
            message_id='signed-message', eve_notification_id=renew_progress.notification_id(op),
            event_type='sms.delivered', carrier_status='delivered', evidence='android_dlr',
            occurred_at=datetime.utcnow(), attempt=1)
        db.session.add_all([row, event]); db.session.commit()
        response = self.client.get('/api/client-operations/renewals/mine')
        self.assertEqual(response.json['operation']['sms']['status'], 'delivered')
        self.assertEqual(response.json['operation']['sms']['delivery_evidence'], 'android_dlr')
        history = self.client.get('/api/client-operations/renewals').json['operations'][0]
        self.assertEqual(history['sms']['status'], 'delivered')
        event.diagnostics_json = json.dumps({'all_segments_delivered': False})
        db.session.commit()
        response = self.client.get('/api/client-operations/renewals/mine')
        self.assertNotEqual(response.json['operation']['sms']['status'], 'delivered')

    def test_delivered_receipt_cannot_be_reverted_by_a_stale_gateway_poll(self):
        from panel.jobs import messaging
        row = SmsSendLog(email='done', server_id=0, state='renew', status='sent',
            request_id='confirmed-delivery', terminal=True, successful=True, carrier_state='delivered')
        db.session.add(row); db.session.commit()
        result = SimpleNamespace(status_code=200, json=lambda: {'status':'sent', 'terminal':True,
            'successful':True, 'carrierStatus':{'status':'pending'}})
        with patch.object(messaging, '_get_sms_runtime_settings', return_value={}), \
                patch.object(messaging, '_get_sms_provider_settings', return_value={
                    'base_url':'https://gateway.invalid', 'api_key':'test'}), \
                patch.object(messaging.requests, 'get', return_value=result):
            messaging._refresh_pending_sms_statuses(log_id=row.id)
        self.assertEqual(row.carrier_state, 'delivered')

    def test_batched_evidence_keeps_each_message_and_older_terminal_receipts(self):
        from panel.services.sms_delivery import _events_for_logs, _project_evidence
        busy = SmsSendLog(email='busy', server_id=0, state='renew', status='queued', request_id='busy')
        quiet = SmsSendLog(email='quiet', server_id=0, state='renew', status='queued', correlation_id='quiet-trace')
        db.session.add_all([busy, quiet]); db.session.flush()
        now = datetime.utcnow()
        rows = [SmsGatewayEvent(event_id='queue-%s' % index, trace_id='busy-trace', message_id='busy',
            event_type='gateway.accepted', occurred_at=now + timedelta(seconds=index), attempt=1)
            for index in range(600)]
        rows.extend([
            SmsGatewayEvent(event_id='old-receipt', trace_id='busy-trace', message_id='busy',
                event_type='sms.delivered', carrier_status='delivered', occurred_at=now - timedelta(days=1), attempt=1),
            SmsGatewayEvent(event_id='quiet-failure', trace_id='quiet-trace', message_id='quiet-message',
                event_type='send.failed', occurred_at=now, attempt=1)])
        db.session.add_all(rows); db.session.commit()
        events = _events_for_logs([busy, quiet])
        self.assertEqual(len(events[busy.id]), 501)
        self.assertEqual([item.event_id for item in events[quiet.id]], ['quiet-failure'])
        self.assertEqual(_project_evidence(events[busy.id], busy)[1]['state'], 'delivered')
        renew_progress.notification_logs([])  # Empty history has no evidence work.

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
