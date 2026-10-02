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
from datetime import datetime
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
from panel.models import Admin, SmsGatewayEvent  # noqa: E402
from panel.routes.sms_gateway_events import _project_evidence  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


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
        self.payload = {
            'event_id': 'evt_test_1', 'trace_id': 'trc_test_1',
            'message_id': 'send_42', 'eve_notification_id': 'eve_notif_7',
            'type': 'send.sent', 'occurred_at': '2026-09-27T06:00:00.000Z',
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
        def event(event_id, kind, minute, carrier=None):
            return SimpleNamespace(
                event_id=event_id, event_type=kind,
                occurred_at=datetime(2026, 9, 27, 9, minute),
                carrier_status=carrier, evidence='android_dlr' if carrier else None)

        normal = [event('evt_1', 'gateway.accepted', 0),
                  event('evt_2', 'send.sent', 1),
                  event('evt_3', 'sms.delivered', 2, 'delivered')]
        submission, carrier = _project_evidence(normal)
        self.assertEqual(submission['state'], 'sent')
        self.assertEqual(carrier['state'], 'delivered')
        self.assertTrue(carrier['confirmed'])

        # Arrival order cannot override occurred_at ordering.
        submission, carrier = _project_evidence(list(reversed(normal)))
        self.assertEqual((submission['state'], carrier['state']), ('sent', 'delivered'))

        late_weaker = normal + [event('evt_5', 'gateway.accepted', 4),
                                event('evt_6', 'sms.delivery_failed', 5, 'failed')]
        submission, carrier = _project_evidence(late_weaker)
        self.assertEqual((submission['state'], carrier['state']), ('sent', 'delivered'))

        # Physical submission is not a carrier DLR. Without an explicit pending
        # DLR signal the carrier layer is honestly "not exposed".
        submission, carrier = _project_evidence(normal[:2])
        self.assertEqual(carrier['state'], 'not_exposed')
        self.assertFalse(carrier['confirmed'])

        failed = normal[:2] + [event('evt_4', 'sms.delivery_failed', 3, 'failed')]
        submission, carrier = _project_evidence(failed)
        self.assertEqual((submission['state'], carrier['state']), ('sent', 'failed'))

    def test_legacy_history_and_missing_callback_are_not_conflated(self):
        now = datetime(2026, 10, 2, 10, 0, 0)
        legacy = SimpleNamespace(
            eve_notification_id=None, created_at=datetime(2026, 9, 9, 10, 0, 0),
            carrier_state=None, carrier_evidence=None)
        submission, carrier = _project_evidence([], legacy, now=now)
        self.assertEqual(submission['state'], 'legacy_record')
        self.assertFalse(submission['actionable'])
        self.assertEqual(submission['evidence'], 'historical_evidence_unavailable')
        self.assertEqual(carrier['state'], 'historical_unavailable')
        self.assertFalse(carrier['actionable'])

        recent = SimpleNamespace(
            eve_notification_id='eve_notif_recent', created_at=datetime(2026, 10, 2, 9, 58, 0),
            carrier_state=None, carrier_evidence=None)
        with patch.dict(os.environ, {'EVE_SMS_EVIDENCE_GRACE_SECONDS': '300'}):
            submission, carrier = _project_evidence([], recent, now=now)
        self.assertEqual(submission['state'], 'pending')
        self.assertFalse(submission['actionable'])
        self.assertEqual(carrier['state'], 'not_exposed')

        stale = SimpleNamespace(
            eve_notification_id='eve_notif_stale', created_at=datetime(2026, 10, 2, 9, 45, 0),
            carrier_state=None, carrier_evidence=None)
        with patch.dict(os.environ, {'EVE_SMS_EVIDENCE_GRACE_SECONDS': '300'}):
            submission, carrier = _project_evidence([], stale, now=now)
        self.assertEqual(submission['state'], 'callback_missing')
        self.assertTrue(submission['actionable'])
        self.assertEqual(submission['evidence'], 'callback_missing')
        self.assertEqual(carrier['state'], 'not_exposed')

    def test_explicit_carrier_pending_is_distinct_from_submission(self):
        event = SimpleNamespace(
            event_id='evt_sent', event_type='send.sent',
            occurred_at=datetime(2026, 10, 2, 9, 0), carrier_status='pending',
            evidence='android_dlr_pending')
        log = SimpleNamespace(
            eve_notification_id='eve_notif_10', created_at=datetime(2026, 10, 2, 8, 59),
            carrier_state=None, carrier_evidence=None)
        submission, carrier = _project_evidence([event], log, now=datetime(2026, 10, 2, 9, 2))
        self.assertEqual(submission['state'], 'sent')
        self.assertEqual(carrier['state'], 'pending')
        self.assertFalse(carrier['confirmed'])

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
        self.assertIn('statusField("Carrier", log.carrier_state || "not_exposed")', source)
        self.assertIn('mutated_local_events', source)
        self.assertIn('audience: loadAudience', source)
        self.assertIn('/api/sms/scan/preview', source)
        self.assertIn('Historical message: signed callback evidence was not collected', source)
        self.assertIn('Expected signed GMweb callback is missing', source)
        self.assertIn('/api/sms/evidence-health', source)
        self.assertIn('EVE send log', source)
        self.assertNotIn('openModal(', source)

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