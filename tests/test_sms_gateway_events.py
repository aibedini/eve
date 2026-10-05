"""GMweb callback authentication, replay, and evidence semantics."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch


_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault('SERVER_PASSWORD_KEY',
                      base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import app  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.models import Admin, SmsGatewayEvent, SmsSendLog  # noqa: E402
from panel.routes.sms_gateway_events import _project_evidence, _delivery_pipeline  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def _now_iso():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _event(event_id, kind, minute, carrier=None, received_minute=None):
    return SimpleNamespace(
        event_id=event_id, event_type=kind,
        occurred_at=datetime(2026, 9, 27, 9, minute),
        received_at=datetime(2026, 9, 27, 9, received_minute if received_minute is not None else minute),
        carrier_status=carrier, evidence='android_dlr' if carrier else None)


class GatewayEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.context.pop()

    def setUp(self):
        SmsGatewayEvent.query.delete()
        db.session.commit()
        self.client = app.test_client()
        self.secret = 'test-gmweb-callback-secret-at-least-32-bytes'
        # Clock-relative: the carrier projection derives an `unconfirmed` reading
        # after the wait window, so a fixed submission time would make these
        # assertions depend on when the suite runs.
        self.payload = {
            'event_id': 'evt_test_1', 'trace_id': 'trc_test_1',
            'message_id': 'send_42', 'eve_notification_id': 'eve_notif_7',
            'type': 'send.sent', 'occurred_at': _now_iso(),
            'attempt': 1, 'device_id': 'android-02', 'reason_code': None, 'stage': None,
        }

    def _post(self, *, body=None, timestamp=None, signature=None):
        raw = json.dumps(body or self.payload, separators=(',', ':')).encode()
        timestamp = str(timestamp or int(time.time()))
        delivery_id = (body or self.payload)['event_id']
        digest = hmac.new(self.secret.encode(),
                          timestamp.encode() + b'.' + delivery_id.encode() + b'.' + raw,
                          hashlib.sha256).hexdigest()
        headers = {'X-GMweb-Timestamp': timestamp,
                   'X-GMweb-Delivery-Id': delivery_id,
                   'X-GMweb-Signature': signature or f'sha256={digest}'}
        with patch.dict(os.environ, {'EVE_SMS_EVENTS_SECRET': self.secret}):
            return self.client.post('/internal/gmweb/sms/events', data=raw,
                                    headers=headers, content_type='application/json')

    def _login_superadmin(self):
        admin = Admin.query.filter_by(username='sms-center-shell-admin').first()
        if admin is None:
            admin = Admin(username='sms-center-shell-admin', password_hash='x',
                          role='superadmin', is_superadmin=True, enabled=True)
            db.session.add(admin)
            db.session.commit()
        with self.client.session_transaction() as browser_session:
            browser_session['admin_id'] = admin.id
            browser_session['admin_username'] = admin.username
            browser_session['role'] = admin.role
            browser_session['is_superadmin'] = True

    def test_signed_event_and_identical_replay(self):
        self.assertEqual(self._post().json, {'accepted': True, 'duplicate': False})
        self.assertEqual(self._post().json, {'accepted': True, 'duplicate': True})
        self.assertEqual(SmsGatewayEvent.query.count(), 1)
        row = db.session.get(SmsGatewayEvent, 'evt_test_1')
        self.assertEqual(row.event_type, 'send.sent')
        self.assertEqual(row.eve_notification_id, 'eve_notif_7')

    def test_rejects_bad_signature_stale_timestamp_and_conflict(self):
        self.assertEqual(self._post(signature='sha256=' + '0' * 64).status_code, 401)
        self.assertEqual(self._post(timestamp=int(time.time()) - 301).status_code, 401)
        self.assertEqual(SmsGatewayEvent.query.count(), 0)
        self.assertEqual(self._post().status_code, 200)
        conflicting = {**self.payload, 'type': 'send.failed'}
        self.assertEqual(self._post(body=conflicting).status_code, 409)
        self.assertEqual(SmsGatewayEvent.query.count(), 1)

    def test_acceptance_is_not_delivery(self):
        self.payload['type'] = 'gateway.accepted'
        self.assertEqual(self._post().status_code, 200)
        row = db.session.get(SmsGatewayEvent, 'evt_test_1')
        self.assertEqual(row.event_type, 'gateway.accepted')
        self.assertNotEqual(row.event_type, 'send.sent')

    def test_carrier_receipt_is_separate_positive_evidence(self):
        self.payload['type'] = 'sms.delivered'
        self.payload.update(request_id='send_42', gateway_request_id='android_42',
                            carrier_status='delivered', evidence='android_dlr')
        self.payload['occurred_at'] = '2026-09-27T09:00:00.000Z'
        self.assertEqual(self._post().status_code, 200)
        row = db.session.get(SmsGatewayEvent, 'evt_test_1')
        self.assertEqual(row.event_type, 'sms.delivered')
        self.assertEqual(row.gateway_request_id, 'android_42')
        self.assertEqual(row.carrier_status, 'delivered')

    def test_submission_and_carrier_projection_remain_independent_and_ordered(self):
        # A fixed `now` keeps the derived unconfirmed window out of this test:
        # 09:10 is inside the 900 s window that opened at the 09:01 submission.
        now = datetime(2026, 9, 27, 9, 10)
        normal = [_event('evt_1', 'gateway.accepted', 0),
                  _event('evt_2', 'send.sent', 1),
                  _event('evt_3', 'sms.delivered', 2, 'delivered')]
        submission, carrier = _project_evidence(normal, now=now)
        self.assertEqual(submission['state'], 'sent')
        self.assertEqual(carrier['state'], 'delivered')
        self.assertTrue(carrier['confirmed'])

        # Arrival order cannot override occurred_at ordering.
        submission, carrier = _project_evidence(list(reversed(normal)), now=now)
        self.assertEqual((submission['state'], carrier['state']), ('sent', 'delivered'))

        late_weaker = normal + [_event('evt_5', 'gateway.accepted', 4),
                                _event('evt_6', 'sms.delivery_failed', 5, 'failed')]
        submission, carrier = _project_evidence(late_weaker, now=now)
        self.assertEqual((submission['state'], carrier['state']), ('sent', 'delivered'))

        submission, carrier = _project_evidence(normal[:2], now=now)
        self.assertEqual(carrier['state'], 'pending')
        self.assertFalse(carrier['confirmed'])

        failed = normal[:2] + [_event('evt_4', 'sms.delivery_failed', 3, 'failed')]
        submission, carrier = _project_evidence(failed, now=now)
        self.assertEqual((submission['state'], carrier['state']), ('sent', 'failed'))

    def test_conflicting_carrier_state_is_rejected(self):
        self.payload.update(type='sms.delivered', carrier_status='failed')
        self.assertEqual(self._post().status_code, 400)
        self.assertEqual(SmsGatewayEvent.query.count(), 0)

    def test_delivery_reconciliation_compares_without_mutating_callback_journal(self):
        self._login_superadmin()
        self.payload.update(type='sms.delivered', carrier_status='delivered')
        self.assertEqual(self._post().status_code, 200)
        before = SmsGatewayEvent.query.count()
        remote = {
            'ok': True, 'available': True, 'limit': 25,
            'events': [
                {'eventId': 'evt_test_1', 'status': 'delivered'},
                {'eventId': 'evt_remote_only', 'status': 'failed'},
            ],
        }
        with patch('app._get_sms_runtime_settings', return_value={
                'base_url': 'https://gmweb.example', 'api_key': 'secret'}), patch(
                'panel.routes.messaging.gmweb_contract.fetch_delivery_events',
                return_value=remote):
            response = self.client.get('/api/sms/delivery-events?limit=25')
        self.assertEqual(response.status_code, 200)
        comparison = response.json['comparison']
        self.assertEqual(comparison['matched'], 1)
        self.assertEqual(comparison['remote_only_event_ids'], ['evt_remote_only'])
        self.assertEqual(comparison['mutated_local_events'], 0)
        self.assertEqual(SmsGatewayEvent.query.count(), before)

    def test_sms_center_template_compiles(self):
        template = app.jinja_env.get_template('sms_center.html')
        self.assertIsNotNone(template)

    def test_sent_log_without_receipt_is_carrier_pending(self):
        self._login_superadmin()
        log = SmsSendLog(email='pending@example.test', server_id=0,
                         state='ended', status='sent', request_id='pending-test',
                         carrier_state='unavailable')
        db.session.add(log)
        db.session.commit()
        try:
            response = self.client.get('/api/sms/logs?q=pending@example.test')
            self.assertEqual(response.status_code, 200)
            row = next(item for item in response.json['logs'] if item['id'] == log.id)
            self.assertEqual(row['carrier_state'], 'pending')
            self.assertEqual(row['carrier_evidence'], 'awaiting_carrier_receipt')
            log.carrier_state = 'delivered'
            db.session.commit()
            response = self.client.get('/api/sms/logs?q=pending@example.test')
            row = next(item for item in response.json['logs'] if item['id'] == log.id)
            self.assertEqual(row['carrier_state'], 'delivered')
            log.carrier_state = 'unavailable'
            log.carrier_evidence = 'device_unsupported'
            db.session.commit()
            response = self.client.get('/api/sms/logs?q=pending@example.test')
            row = next(item for item in response.json['logs'] if item['id'] == log.id)
            self.assertEqual(row['carrier_state'], 'unavailable')
        finally:
            db.session.delete(log)
            db.session.commit()

    def test_list_and_timeline_share_delivered_projection_and_pipeline(self):
        self._login_superadmin()
        log = SmsSendLog(email='delivery@example.test', server_id=0,
                         state='ended', status='sent', request_id='send_42',
                         gateway_job_id='job_42', carrier_state='unavailable')
        db.session.add(log)
        db.session.commit()
        try:
            self.assertEqual(self._post().status_code, 200)
            delivered = dict(self.payload, event_id='evt_delivery_42',
                             type='sms.delivered', carrier_status='delivered',
                             evidence='android_dlr', segment_index=0,
                             segment_count=1, all_segments_delivered=True,
                             carrier_result_code=0, carrier_result='DELIVRD',
                             android_delivery_received_at='2026-09-27T06:00:10Z',
                             gmweb_delivery_received_at='2026-09-27T06:00:11Z')
            self.assertEqual(self._post(body=delivered).status_code, 200)
            list_response = self.client.get('/api/sms/logs?q=delivery@example.test')
            timeline = self.client.get(f'/api/sms/messages/{log.id}/timeline')
            self.assertEqual(list_response.status_code, 200)
            self.assertEqual(timeline.status_code, 200)
            summary = next(row for row in list_response.json['logs'] if row['id'] == log.id)
            self.assertEqual(summary['carrier_state'], 'delivered')
            self.assertEqual(summary['delivery']['carrier'], timeline.json['carrier'])
            self.assertEqual(timeline.json['submission']['state'], 'sent')
            self.assertEqual(timeline.json['carrier']['diagnostics']['carrier_result_code'], 0)
            self.assertEqual(timeline.json['carrier']['diagnostics']['gmweb_delivery_received_at'],
                             '2026-09-27T06:00:11Z')
            self.assertEqual(timeline.json['pipeline'][-1]['state'], 'confirmed')
            self.assertEqual(timeline.json['pipeline'][-2]['state'], 'confirmed')
            self.assertEqual(self._post(body=delivered).json,
                             {'accepted': True, 'duplicate': True})
            timeline = self.client.get(f'/api/sms/messages/{log.id}/timeline')
            self.assertEqual(sum(row['type'] == 'sms.delivered'
                                 for row in timeline.json['gateway_events']), 1)
        finally:
            db.session.delete(log)
            db.session.commit()

    def test_submission_only_pipeline_waits_for_carrier_without_fake_confirmations(self):
        self._login_superadmin()
        log = SmsSendLog(email='submission@example.test', server_id=0,
                         state='ended', status='sent', request_id='send_42',
                         gateway_job_id='job_42', carrier_state='unavailable')
        db.session.add(log)
        db.session.commit()
        try:
            self.assertEqual(self._post().status_code, 200)
            timeline = self.client.get(f'/api/sms/messages/{log.id}/timeline').json
            self.assertEqual(timeline['carrier']['state'], 'pending')
            states = [step['state'] for step in timeline['pipeline']]
            self.assertEqual(states[:2], ['confirmed', 'confirmed'])
            self.assertEqual(states[5:], ['waiting', 'waiting', 'waiting'])
            self.assertEqual(timeline['pipeline'][5]['at'], None)
            # Android cannot have submitted to SmsManager without accepting the
            # job, so this stage must never contradict the submission below it.
            self.assertEqual(timeline['pipeline'][2]['state'], 'confirmed')
            self.assertEqual(timeline['pipeline'][2]['evidence'],
                             'implied_by_android_submission')
            self.assertNotIn('not_reported', states[3:5])
        finally:
            db.session.delete(log)
            db.session.commit()

    def test_explicit_unsupported_and_within_window_pending_remain_distinct(self):
        sent = SimpleNamespace(event_id='evt_sent', event_type='send.sent',
                               occurred_at=datetime(2020, 1, 1),
                               received_at=datetime(2020, 1, 1),
                               carrier_status=None, evidence=None)
        waiting_log = SimpleNamespace(status='sent', carrier_state='unavailable',
                                      carrier_evidence=None)
        # Inside the wait window the message is still simply pending ...
        self.assertEqual(_project_evidence([sent], waiting_log,
                                           now=datetime(2020, 1, 1, 0, 5))[1]['state'], 'pending')
        unsupported_log = SimpleNamespace(status='sent', carrier_state='unavailable',
                                          carrier_evidence='device_unsupported')
        self.assertEqual(_project_evidence([sent], unsupported_log,
                                           now=datetime(2020, 1, 1, 0, 5))[1]['state'],
                         'unavailable')
        # ... and past it the same missing evidence reads as unconfirmed, never
        # as delivered, failed or unavailable.
        carrier = _project_evidence([sent], waiting_log, now=datetime(2020, 1, 1, 1))[1]
        self.assertEqual(carrier['state'], 'unconfirmed')
        self.assertFalse(carrier['confirmed'])
        self.assertEqual(carrier['evidence'], 'no_carrier_receipt_within_window')
        self.assertEqual(carrier['timeout_seconds'], 900)
        self.assertIsNone(carrier.get('occurred_at'))

    def test_late_failure_cannot_erase_verified_delivered_log(self):
        failure = SimpleNamespace(event_id='evt_failure', event_type='sms.delivery_failed',
                                  occurred_at=datetime(2026, 9, 27),
                                  carrier_status='failed', evidence='android_dlr')
        log = SimpleNamespace(status='sent', carrier_state='delivered',
                              carrier_evidence='carrier_dlr', carrier_occurred_at=None)
        self.assertEqual(_project_evidence([failure], log)[1]['state'], 'delivered')

    def test_multipart_receipt_requires_explicit_aggregate_completion(self):
        sent = SimpleNamespace(event_id='evt_sent', event_type='send.sent',
                               occurred_at=datetime(2026, 9, 27, 6),
                               received_at=datetime(2026, 9, 27, 6),
                               carrier_status=None, evidence=None)
        part = SimpleNamespace(event_id='evt_part', event_type='sms.delivered',
                               occurred_at=datetime(2026, 9, 27, 6, 1),
                               received_at=datetime(2026, 9, 27, 6, 1),
                               carrier_status='delivered', evidence='android_dlr',
                               diagnostics_json=json.dumps({'all_segments_delivered': False}))
        now = datetime(2026, 9, 27, 6, 5)
        self.assertEqual(_project_evidence([sent, part], now=now)[1]['state'], 'pending')
        part.diagnostics_json = json.dumps({'all_segments_delivered': True})
        self.assertEqual(_project_evidence([sent, part], now=now)[1]['state'], 'delivered')

    # ── signed Android carrier result code (GMweb/Android contract) ─────────
    #
    # Android passes the raw PendingIntent callback result code through
    # (SmsStatusReceiver.kt: `callbackResultCode = resultCode`, success ==
    # Activity.RESULT_OK == -1) and GMweb relays it unchanged inside the signed
    # diagnostics. EVE validated it with the UNSIGNED counters, answered HTTP 400
    # invalid_event for every real receipt and GMweb dead-lettered the callback,
    # so the carrier state stayed pending for ever.

    def _delivered_payload(self, **overrides):
        body = dict(self.payload, event_id='evt_delivery_1', type='sms.delivered',
                    carrier_status='delivered', evidence='android_dlr',
                    occurred_at=_now_iso())
        body.update(overrides)
        return body

    def test_signed_android_carrier_result_code_is_accepted(self):
        for code in (-1, 0, 1, -1000000, 1000000):
            event_id = 'evt_code_%s' % ('neg1' if code == -1 else code)
            body = self._delivered_payload(event_id=event_id, carrier_result_code=code)
            self.assertEqual(self._post(body=body).status_code, 200, code)
            row = db.session.get(SmsGatewayEvent, event_id)
            self.assertIsNotNone(row, code)
            self.assertEqual(json.loads(row.diagnostics_json)['carrier_result_code'], code)
            self.assertEqual(row.carrier_status, 'delivered')

    def test_carrier_result_code_outside_the_signed_range_is_rejected(self):
        for code in (-1000001, 1000001):
            body = self._delivered_payload(event_id=f'evt_code_{code}',
                                           carrier_result_code=code)
            self.assertEqual(self._post(body=body).status_code, 400, code)
        self.assertEqual(SmsGatewayEvent.query.count(), 0)

    def test_boolean_is_not_an_integer_carrier_result_code(self):
        body = self._delivered_payload(event_id='evt_code_bool', carrier_result_code=True)
        self.assertEqual(self._post(body=body).status_code, 400)
        self.assertEqual(SmsGatewayEvent.query.count(), 0)

    def test_unsigned_diagnostics_still_reject_negative_values(self):
        body = self._delivered_payload(event_id='evt_code_negative_segment',
                                       carrier_result_code=-1, segment_index=-1)
        self.assertEqual(self._post(body=body).status_code, 400)
        body = self._delivered_payload(event_id='evt_code_negative_count',
                                       carrier_result_code=-1, segment_count=-1)
        self.assertEqual(self._post(body=body).status_code, 400)
        self.assertEqual(SmsGatewayEvent.query.count(), 0)

    def test_replay_of_a_signed_code_receipt_is_idempotent(self):
        body = self._delivered_payload(carrier_result_code=-1)
        self.assertEqual(self._post(body=body).json,
                         {'accepted': True, 'duplicate': False})
        self.assertEqual(self._post(body=body).json,
                         {'accepted': True, 'duplicate': True})
        self.assertEqual(SmsGatewayEvent.query.count(), 1)

    def test_signed_code_receipt_confirms_every_pipeline_stage(self):
        self._login_superadmin()
        log = SmsSendLog(email='signed-code@example.test', server_id=0, state='ended',
                         status='sent', request_id='send_42', gateway_job_id='job_42',
                         carrier_state='unavailable')
        db.session.add(log)
        db.session.commit()
        try:
            self.assertEqual(self._post().status_code, 200)  # send.sent
            queued = dict(self.payload, event_id='evt_queued_42', type='send.queued',
                          occurred_at='2026-09-27T05:59:00.000Z')
            self.assertEqual(self._post(body=queued).status_code, 200)
            delivered = self._delivered_payload(
                carrier_result_code=-1, segment_index=0, segment_count=1,
                all_segments_delivered=True, carrier_result='DELIVRD',
                android_delivery_received_at='2026-09-27T06:00:10Z',
                gmweb_delivery_received_at='2026-09-27T06:00:11Z')
            self.assertEqual(self._post(body=delivered).status_code, 200)
            timeline = self.client.get(f'/api/sms/messages/{log.id}/timeline').json
            self.assertEqual(timeline['carrier']['state'], 'delivered')
            self.assertEqual(timeline['carrier']['diagnostics']['carrier_result_code'], -1)
            self.assertEqual([step['state'] for step in timeline['pipeline'][5:]],
                             ['confirmed', 'confirmed', 'confirmed'])
            # "Android accepted job" is no longer hardcoded not_reported.
            self.assertEqual(timeline['pipeline'][2]['state'], 'confirmed')
            self.assertEqual(timeline['pipeline'][2]['evidence'],
                             'implied_by_android_submission')
            # "GMweb job created" now carries the queue event's own timestamp.
            self.assertEqual(timeline['pipeline'][1]['at'], '2026-09-27T05:59:00Z')
        finally:
            db.session.delete(log)
            db.session.commit()

    def test_explicit_gateway_acceptance_is_used_when_present(self):
        accepted = _event('evt_accepted', 'gateway.accepted', 0)
        submitted = _event('evt_sent', 'send.sent', 1)
        log = SimpleNamespace(status='sent', carrier_state='unavailable',
                              carrier_evidence=None, request_id='send_42',
                              gateway_job_id='job_42', created_at=datetime(2026, 9, 27, 8, 59))
        submission, carrier = _project_evidence([accepted, submitted], log,
                                                now=datetime(2026, 9, 27, 9, 5))
        steps = {step['name']: step for step in
                 _delivery_pipeline(log, [accepted, submitted], submission, carrier)}
        self.assertEqual(steps['Android accepted job']['state'], 'confirmed')
        self.assertEqual(steps['Android accepted job']['evidence'], 'gateway_acceptance')
        self.assertEqual(steps['Android accepted job']['at'], '2026-09-27T09:00:00Z')
        self.assertEqual(steps['GMweb job created']['at'], '2026-09-27T09:00:00Z')

    # ── derived unconfirmed state (display-only projection) ──────────────────

    def _sent_event(self, at=datetime(2026, 9, 27, 6, 0)):
        return SimpleNamespace(event_id='evt_sent', event_type='send.sent',
                               occurred_at=at, received_at=at,
                               carrier_status=None, evidence=None)

    def _waiting_log(self):
        return SimpleNamespace(status='sent', carrier_state='unavailable',
                               carrier_evidence=None, request_id='send_42',
                               gateway_job_id='job_42',
                               created_at=datetime(2026, 9, 27, 5, 59))

    def test_sent_without_receipt_turns_unconfirmed_after_the_wait_window(self):
        sent = self._sent_event()
        log = self._waiting_log()
        with patch.dict(os.environ, {'SMS_DLR_PENDING_TIMEOUT_SECONDS': '900'}):
            inside = _project_evidence([sent], log, now=datetime(2026, 9, 27, 6, 14))[1]
            self.assertEqual(inside['state'], 'pending')
            self.assertNotIn('timeout_seconds', inside)
            submission, carrier = _project_evidence([sent], log, now=datetime(2026, 9, 27, 6, 15))
        self.assertEqual(carrier['state'], 'unconfirmed')
        self.assertFalse(carrier['confirmed'])
        self.assertEqual(carrier['evidence'], 'no_carrier_receipt_within_window')
        self.assertEqual(carrier['timeout_seconds'], 900)
        steps = {step['name']: step for step in
                 _delivery_pipeline(log, [sent], submission, carrier)}
        self.assertEqual(steps['SmsManager submission']['state'], 'confirmed')
        self.assertEqual(steps['SENT callback ingested']['state'], 'confirmed')
        self.assertEqual(steps['Android accepted job']['state'], 'confirmed')
        for name in ('Carrier DELIVERY callback', 'GMweb delivery report received',
                     'EVE delivery receipt ingested'):
            self.assertEqual(steps[name]['state'], 'unconfirmed', name)
        self.assertEqual(steps['Carrier DELIVERY callback']['reason'],
                         'carrier_receipt_timeout')

    def test_wait_window_is_configurable_and_zero_disables_it(self):
        sent = self._sent_event()
        log = self._waiting_log()
        with patch.dict(os.environ, {'SMS_DLR_PENDING_TIMEOUT_SECONDS': '60'}):
            self.assertEqual(_project_evidence([sent], log,
                                               now=datetime(2026, 9, 27, 6, 0, 59))[1]['state'],
                             'pending')
            self.assertEqual(_project_evidence([sent], log,
                                               now=datetime(2026, 9, 27, 6, 1))[1]['state'],
                             'unconfirmed')
        with patch.dict(os.environ, {'SMS_DLR_PENDING_TIMEOUT_SECONDS': '0'}):
            self.assertEqual(_project_evidence([sent], log,
                                               now=datetime(2030, 1, 1))[1]['state'], 'pending')
        with patch.dict(os.environ, {'SMS_DLR_PENDING_TIMEOUT_SECONDS': 'not-a-number'}):
            self.assertEqual(_project_evidence([sent], log,
                                               now=datetime(2026, 9, 27, 6, 15))[1]['state'],
                             'unconfirmed')

    def test_timeout_never_overrides_a_real_receipt(self):
        sent = self._sent_event()
        receipt = SimpleNamespace(event_id='evt_delivered', event_type='sms.delivered',
                                  occurred_at=datetime(2026, 9, 27, 6, 2),
                                  received_at=datetime(2026, 9, 27, 6, 2),
                                  carrier_status='delivered',
                                  evidence='android_dlr')
        carrier = _project_evidence([sent, receipt], self._waiting_log(),
                                    now=datetime(2030, 1, 1))[1]
        self.assertEqual(carrier['state'], 'delivered')
        self.assertTrue(carrier['confirmed'])

    def test_list_and_timeline_agree_on_unconfirmed_after_the_window(self):
        self._login_superadmin()
        log = SmsSendLog(email='unconfirmed@example.test', server_id=0, state='ended',
                         status='sent', request_id='send_42', gateway_job_id='job_42',
                         carrier_state='unavailable')
        db.session.add(log)
        db.session.commit()
        try:
            old = (datetime.utcnow() - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            self.assertEqual(self._post(body=dict(self.payload, occurred_at=old)).status_code, 200)
            list_response = self.client.get('/api/sms/logs?q=unconfirmed@example.test')
            timeline = self.client.get(f'/api/sms/messages/{log.id}/timeline')
            self.assertEqual((list_response.status_code, timeline.status_code), (200, 200))
            summary = next(row for row in list_response.json['logs'] if row['id'] == log.id)
            self.assertEqual(summary['carrier_state'], 'unconfirmed')
            self.assertEqual(summary['delivery']['carrier'], timeline.json['carrier'])
            self.assertEqual(timeline.json['carrier']['state'], 'unconfirmed')
            self.assertEqual([step['state'] for step in timeline.json['pipeline'][5:]],
                             ['unconfirmed', 'unconfirmed', 'unconfirmed'])
        finally:
            db.session.delete(log)
            db.session.commit()

    def test_log_date_filter_converts_offset_before_querying_utc(self):
        self._login_superadmin()
        log = SmsSendLog(email='midnight@example.test', server_id=0,
                         state='ended', status='sent',
                         created_at=datetime(2026, 10, 4, 21, 0))
        db.session.add(log)
        db.session.commit()
        try:
            response = self.client.get('/api/sms/logs?'
                'from=2026-10-05T00%3A00%3A00%2B03%3A30&'
                'to=2026-10-06T00%3A00%3A00%2B03%3A30&'
                'q=midnight@example.test')
            self.assertEqual(response.status_code, 200)
            self.assertIn(log.id, [row['id'] for row in response.json['logs']])
        finally:
            db.session.delete(log)
            db.session.commit()

    def test_current_audience_preview_remains_available_when_automation_is_disabled(self):
        self._login_superadmin()
        result = {
            'preview': True, 'matched': 1, 'eligible_now': 0, 'deferred': 1,
            'suppressed': 0, 'invalid_recipient': 0, 'active_obligation': 0,
            'run_state': 'sms_disabled', 'reasons': {'sms_disabled': 1},
            'candidates': [{'email': 'x1', 'state': 'ended',
                            'disposition': 'deferred', 'reason_code': 'sms_disabled'}],
        }
        with patch('app._get_sms_runtime_settings', return_value={'enabled': False}), patch(
                'app._run_sms_depletion_scan', return_value=result) as preview, patch(
                'panel.core.redis_client.load_snapshot_from_redis', return_value=False), patch.dict(
                'panel.core.redis_client.GLOBAL_SERVER_DATA', {'last_update': datetime.utcnow().isoformat()}):
            response = self.client.post('/api/sms/scan/preview', json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['candidates'][0]['reason_code'], 'sms_disabled')
        preview.assert_called_once_with(triggered_by='preview', states=None, preview=True)

    def test_audience_preview_hydrates_shared_snapshot_and_distinguishes_missing_source(self):
        from panel.core.redis_client import GLOBAL_SERVER_DATA
        self._login_superadmin()
        result = {'matched': 2, 'eligible_now': 2, 'candidates': []}
        original = dict(GLOBAL_SERVER_DATA)
        try:
            GLOBAL_SERVER_DATA.update({'last_update': None, 'inbounds': []})
            def hydrate(*, force=False):
                GLOBAL_SERVER_DATA.update({
                    'last_update': datetime.utcnow().isoformat(),
                    'inbounds': [{'id': 1}, {'id': 2}],
                })
                return True
            with patch('app._get_sms_runtime_settings', return_value={}), patch(
                    'app._run_sms_depletion_scan', return_value=result) as scan, patch(
                    'panel.core.redis_client.load_snapshot_from_redis', side_effect=hydrate) as load:
                response = self.client.post('/api/sms/scan/preview', json={})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['matched'], 2)
                self.assertEqual(response.json['source']['inbounds'], 2)
                load.assert_called_once_with(force=False)
                self.client.post('/api/sms/scan/preview', json={'refresh_source': True})
                self.assertEqual(load.call_args.kwargs, {'force': False})
                self.assertEqual(scan.call_count, 2)
            GLOBAL_SERVER_DATA.update({'last_update': None, 'inbounds': []})
            with patch('app._get_sms_runtime_settings', return_value={}), patch(
                    'app._run_sms_depletion_scan', return_value=result) as scan, patch(
                    'panel.core.redis_client.load_snapshot_from_redis', return_value=False):
                response = self.client.post('/api/sms/scan/preview', json={})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json['source']['state'], 'unavailable')
                scan.assert_not_called()
                GLOBAL_SERVER_DATA['last_update'] = '2020-01-01T00:00:00'
                response = self.client.post('/api/sms/scan/preview', json={})
                self.assertEqual(response.json['source']['state'], 'stale')
                scan.assert_not_called()
            GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()
            with patch('app._get_sms_runtime_settings', return_value={}), patch(
                    'app._run_sms_depletion_scan', return_value={'matched': 0, 'candidates': []}), patch(
                    'panel.core.redis_client.load_snapshot_from_redis', return_value=False):
                response = self.client.post('/api/sms/scan/preview', json={})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['source']['state'], 'ready')
                self.assertEqual(response.json['matched'], 0)
        finally:
            GLOBAL_SERVER_DATA.clear()
            GLOBAL_SERVER_DATA.update(original)

    def test_audience_preview_reports_scanned_clients_and_detected_states(self):
        from panel.core.redis_client import GLOBAL_SERVER_DATA
        from panel.jobs.messaging import _run_sms_depletion_scan
        original = dict(GLOBAL_SERVER_DATA)
        try:
            GLOBAL_SERVER_DATA['inbounds'] = [{
                'server_id': 1, 'server_name': 'test',
                'clients': [{'email': 'expired-09120000000', 'enable': True,
                             'totalGB': 1024 ** 3, 'up': 0, 'down': 0,
                             'expiryTimestamp': 1}],
            }]
            with patch('panel.jobs.messaging._get_sms_runtime_settings', return_value={
                    'enabled': True, 'trigger_expired': True,
                    'depletion_expiry_days': 3, 'depletion_volume_gb': 2,
                    'cooldown_hours': {}, 'expired_max_age_days': 0,
                }), patch('panel.jobs.messaging._sms_gateway_ready', return_value=(True, None, 200)), patch(
                    'panel.jobs.messaging._sms_scan_snapshot', return_value={'state': 'idle'}), patch(
                    'app._get_monitor_settings', return_value={'filters': {}, 'templates': {}}):
                result = _run_sms_depletion_scan(preview=True)
            self.assertEqual(result['scanned'], 1)
            self.assertEqual(result['detected_states']['expired'], 1)
        finally:
            GLOBAL_SERVER_DATA.clear()
            GLOBAL_SERVER_DATA.update(original)

    def test_sms_center_keeps_superadmin_navigation(self):
        self._login_superadmin()
        response = self.client.get('/sms-center')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('>Servers</span>', html)
        self.assertIn('>Settings</span>', html)

    def test_sms_center_ui_uses_human_account_and_inline_disclosure(self):
        source = (ROOT / 'static' / 'sms-center.js').read_text(encoding='utf-8')
        self.assertIn('item.account || item.service_key', source)
        self.assertIn('sms-center-expansion hidden', source)
        self.assertIn('inlineDecisionLimit = 5', source)
        self.assertIn('statusField("EVE", log.status)', source)
        self.assertIn('statusField("Carrier", carrierStateForCard(log))', source)
        self.assertIn('mutated_local_events', source)
        self.assertIn('audience: loadAudience', source)
        self.assertIn('/api/sms/scan/preview', source)
        self.assertIn('Show all ${result.total || decisions.length} decisions', source)
        self.assertIn('No signed GMweb callback matches this message', source)
        self.assertNotIn('openModal(', source)

    def test_sms_center_renders_the_unconfirmed_carrier_state(self):
        source = (ROOT / 'static' / 'sms-center.js').read_text(encoding='utf-8')
        stylesheet = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
        self.assertIn('if (status === "unconfirmed") return "unconfirmed";', source)
        self.assertIn('no_carrier_receipt_within_window', source)
        self.assertIn('implied_by_android_submission', source)
        self.assertIn('.sms-status-unconfirmed', stylesheet)

    def test_sms_settings_deep_link_is_hash_aware(self):
        source = (ROOT / 'templates' / 'settings.html').read_text(encoding='utf-8')
        self.assertIn('data-settings-tab="sms"', source)
        self.assertIn("window.location.hash.startsWith('#tab-')", source)
        self.assertIn("switchTab('sms', this)", source)
        self.assertNotIn('event.currentTarget.classList.add', source)

    def test_outbound_notification_identity_is_preserved(self):
        from panel.jobs.messaging import _notification_meta
        meta = _notification_meta({
            'source': 'eve', 'serviceKey': 'eve:1:uuid',
            'notificationKind': 'volume_ended', 'generation': 1,
            'correlationId': 'trc_test_1', 'eveNotificationId': 'eve_notif_7',
        })
        self.assertEqual(meta['eveNotificationId'], 'eve_notif_7')
