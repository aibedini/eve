"""GMweb callback authentication, replay, and evidence semantics."""

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
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
from panel.models import SmsGatewayEvent  # noqa: E402


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
        delivery_id = 'dlv_evt_test_1'
        digest = hmac.new(self.secret.encode(),
                          timestamp.encode() + b'.' + delivery_id.encode() + b'.' + raw,
                          hashlib.sha256).hexdigest()
        headers = {'X-GMweb-Timestamp': timestamp,
                   'X-GMweb-Delivery-Id': delivery_id,
                   'X-GMweb-Signature': signature or f'sha256={digest}'}
        with patch.dict(os.environ, {'EVE_SMS_EVENTS_SECRET': self.secret}):
            return self.client.post('/internal/gmweb/sms/events', data=raw,
                                    headers=headers, content_type='application/json')

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
        self.payload['occurred_at'] = '2026-09-27T09:00:00.000Z'
        self.assertEqual(self._post().status_code, 200)
        row = db.session.get(SmsGatewayEvent, 'evt_test_1')
        self.assertEqual(row.event_type, 'sms.delivered')

    def test_sms_center_template_compiles(self):
        template = app.jinja_env.get_template('sms_center.html')
        self.assertIsNotNone(template)

    def test_outbound_notification_identity_is_preserved(self):
        from panel.jobs.messaging import _notification_meta
        meta = _notification_meta({
            'source': 'eve', 'serviceKey': 'eve:1:uuid',
            'notificationKind': 'volume_ended', 'generation': 1,
            'correlationId': 'trc_test_1', 'eveNotificationId': 'eve_notif_7',
        })
        self.assertEqual(meta['eveNotificationId'], 'eve_notif_7')
