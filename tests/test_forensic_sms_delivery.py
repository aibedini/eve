"""The forensic script must name the gate that refused the SMS, not a guess.

This seeds the exact production shape -- a `low_volume` SMS 20 hours ago, a
`volume_ended` transition today -- and asserts that the report the operator reads
points at the right gate in each of the four configurations that produce silence.
"""
import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.models import (  # noqa: E402
    ServiceNotificationEvent,
    ServiceObservedState,
    WhatsappBotLog,
)
from panel.services import lifecycle  # noqa: E402
from scripts import forensic_sms_delivery as forensic  # noqa: E402

GB = 1024 ** 3
SERVER_ID = 9
EMAIL = 'h34-09195758193'


class ForensicVerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        ServiceNotificationEvent.query.delete()
        ServiceObservedState.query.delete()
        WhatsappBotLog.query.delete()
        db.session.commit()
        self.key = lifecycle.make_service_key(SERVER_ID, 'uuid-forensic')
        db.session.add(ServiceObservedState(
            service_key=self.key, server_id=SERVER_ID, client_uuid='uuid-forensic',
            client_email=EMAIL, last_state='volume_ended', last_state_tag='ended',
            last_remaining_bytes=0, last_total_bytes=20 * GB, last_expiry_ms=0,
            state_version=3, last_observed_at=datetime.utcnow(),
            created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        self.event = ServiceNotificationEvent(
            event_id='st:forensic-1', service_key=self.key, server_id=SERVER_ID,
            client_uuid='uuid-forensic', client_email=EMAIL, state='volume_ended',
            previous_state='volume_low', notification_kind='volume_ended',
            state_version=3, lifecycle_generation=1, observed_at=datetime.utcnow(),
            source='transition', status='pending', attempt_count=1,
            next_attempt_at=datetime.utcnow(), idempotency_key='depletion-st:forensic-1',
            created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.session.add(self.event)
        db.session.commit()

    def _log(self, event_name, hours_ago):
        db.session.add(WhatsappBotLog(email=EMAIL, server_id=SERVER_ID,
                                      event=event_name,
                                      sent_at=datetime.utcnow() - timedelta(hours=hours_ago)))
        db.session.commit()

    #: The settings a working install has. The script reads the real config surface;
    #: the tests supply the values, and the gates under test are the delivery ones.
    CFG = {
        'enabled': True, 'provider': 'gmweb',
        'trigger_near_expiry': True, 'trigger_low_volume': True,
        'trigger_expired': True, 'trigger_ended': True,
        'cooldown_hours': {'near_expiry': 24, 'low_volume': 24,
                           'expired': 48, 'ended': 24},
        'quiet_enabled': False, 'quiet_start': 2, 'quiet_end': 8,
        'daily_limit': 200, 'hourly_limit': 0,
    }

    def _collect(self, email=EMAIL, server= SERVER_ID, hours=72, cfg=None):
        from unittest import mock
        from panel.jobs import messaging
        with mock.patch.object(messaging, '_get_sms_runtime_settings',
                               return_value=dict(cfg or self.CFG)):
            return forensic.collect(email, server, hours)

    def test_the_report_finds_the_account_and_names_the_cooldown_gate(self):
        # The 20-hour-old warning is inside the ended cooldown window: the script must
        # say the cooldown defers it AND that the matching row was a warning, which is
        # precisely the reading the code's own kind map now prevents.
        self._log('sms_low_volume', hours_ago=20)
        report = self._collect()
        self.assertTrue(report['found'])
        self.assertEqual(report['account']['email'], EMAIL)
        gates = {step['gate']: step for step in report['verdict']['gates']}
        self.assertIn('cooldown', gates)
        self.assertEqual(gates['cooldown']['result'], 'ok')
        self.assertEqual(report['verdict']['cooldown']['matching_log_events_in_window'], [])
        self.assertIn('sms_low_volume',
                      report['verdict']['prior_automation_events_ignored'])
        self.assertEqual([row['state'] for row in report['events']], ['volume_ended'])

    def test_a_same_kind_row_is_reported_as_the_deferral(self):
        self._log('sms_ended', hours_ago=3)
        report = self._collect()
        verdict = report['verdict']
        gates = {step['gate']: step for step in verdict['gates']}
        self.assertEqual(gates['cooldown']['result'], 'DEFERS')
        self.assertEqual(verdict['root_cause'], 'cooldown_active_deferred')
        self.assertEqual(verdict['cooldown']['matching_log_events_in_window'],
                         ['sms_ended'])
        self.assertGreater(verdict['cooldown']['remaining_seconds'], 20 * 3600)

    def test_a_disabled_trigger_is_named_as_the_root_cause(self):
        cfg = dict(self.CFG, trigger_ended=False)
        report = self._collect(cfg=cfg)
        wires = {step['gate']: step for step in report['verdict']['gates']}
        self.assertEqual(wires['per_state_trigger']['result'], 'BLOCKS')
        self.assertEqual(report['verdict']['root_cause'], 'trigger_disabled')
        # The walk stops at the first refusal instead of listing gates that no longer
        # matter, so the operator reads one reason.
        self.assertNotIn('cooldown', wires)

    def test_an_unknown_account_is_reported_as_not_found(self):
        report = self._collect(email='nobody@example.com')
        self.assertFalse(report['found'])
        self.assertEqual(report['observed_state'], [])

    def test_the_report_never_carries_credentials(self):
        # The whitelist is the guard: a gateway key must not be printable even if it
        # is configured, so the test asserts the whitelist itself.
        self.assertNotIn('api_key', forensic.SAFE_SETTINGS)
        self.assertNotIn('base_url', forensic.SAFE_SETTINGS)
        self.assertTrue(all(not name.endswith('key') for name in forensic.SAFE_SETTINGS))
        recipient_fields = [name for name in forensic.EVENT_FIELDS
                            if 'recipient' in name or 'phone' in name]
        self.assertEqual(recipient_fields, [])


if __name__ == '__main__':
    unittest.main()
