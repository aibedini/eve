"""A deferred candidate is a DECISION and must be durable without a send log.

The production bug these tests pin, in three parts:

* SmsScanDecision was written only from ``_sms_log_row``, so a candidate that was
  deferred or suppressed BEFORE any send attempt had no durable row at all --
  "candidate matched, Android offline, nothing recorded" was indistinguishable
  from "no candidate existed";
* that write returned early whenever no ``SmsScanRun`` matched the run id, and
  the depletion pipeline uses ``evt-<event_id>`` run ids and creates no
  SmsScanRun, so the one pipeline that actually sends recorded NO decisions;
* it refused to update an existing row, so a candidate the pipeline had already
  deferred could never gain its attempt evidence.

SmsSendLog is attempt evidence. SmsScanDecision is candidate/decision evidence.
The tests below drive the real delivery gate and assert both, because the whole
point is that the decision exists when the send never happened.
"""
import base64
import os
import tempfile
import unittest
from datetime import datetime

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL',
                      'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
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
    SmsScanDecision,
    SmsScanRun,
    SmsSendLog,
)
from panel.services import lifecycle  # noqa: E402

SERVER_ID = 7
EMAIL = 'h34-09195758193@example.com'
RUN_ID = 'evt-candidate-1'


class CandidateDecisionTests(unittest.TestCase):
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
        from panel.jobs import messaging
        self.messaging = messaging
        SmsScanDecision.query.delete()
        SmsScanRun.query.delete()
        SmsSendLog.query.delete()
        ServiceNotificationEvent.query.delete()
        ServiceObservedState.query.delete()
        db.session.commit()
        self.key = lifecycle.make_service_key(SERVER_ID, 'uuid-candidate')

    # ── fixtures ──────────────────────────────────────────────────────────────

    def _open(self, run_id=RUN_ID, **kw):
        kw.setdefault('service_key', self.key)
        kw.setdefault('state', 'ended')
        kw.setdefault('server_id', SERVER_ID)
        kw.setdefault('client_email', EMAIL)
        return self.messaging._sms_open_candidate_decision(run_id, **kw)

    def _event(self, *, canonical_state='volume_ended', status='pending',
               last_error=None, email=EMAIL):
        event = ServiceNotificationEvent(
            event_id='st:candidate-%d' % id(self), service_key=self.key,
            server_id=SERVER_ID, client_uuid='uuid-candidate', client_email=email,
            state=canonical_state, previous_state='active',
            notification_kind=canonical_state, state_version=1,
            lifecycle_generation=3, observed_at=datetime.utcnow(),
            source='transition', status=status, attempt_count=0,
            next_attempt_at=datetime.utcnow(), last_error=last_error,
            idempotency_key='depletion-%d' % id(self),
            created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.session.add(event)
        db.session.commit()
        return event

    def _send_log(self, status='sent'):
        row = SmsSendLog(email=EMAIL, server_id=SERVER_ID, server_name='Srv',
                         state='ended', recipient='0919***8193', status=status,
                         job_id=RUN_ID, request_id='req-1', gateway_job_id='job-1',
                         created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.session.add(row)
        db.session.flush()
        return row

    # ── the decision no longer needs a run or a send log ──────────────────────

    def test_a_decision_exists_without_any_run_or_send_log(self):
        decision = self._open()
        self.assertIsNotNone(decision.id)
        self.assertEqual(SmsScanRun.query.count(), 0)
        self.assertEqual(SmsSendLog.query.count(), 0)
        self.assertEqual(decision.run_id, RUN_ID)
        self.assertEqual(decision.client_email, EMAIL)
        self.assertEqual(decision.disposition, 'deferred')
        self.assertEqual(decision.reason_code, 'evaluation_pending')

    def test_the_pipeline_run_id_needs_no_manual_run(self):
        # The outbox uses 'evt-<event_id>' and never creates a SmsScanRun; that
        # early return is what made it record nothing.
        self._open(run_id='evt-abcdef0123456789')
        self.assertEqual(SmsScanRun.query.count(), 0)
        self.assertEqual(SmsScanDecision.query.count(), 1)

    def test_opening_twice_is_idempotent(self):
        first = self._open()
        second = self._open()
        self.assertEqual(SmsScanDecision.query.count(), 1)
        self.assertEqual(first.id, second.id)

    def test_two_services_in_one_run_are_two_candidates(self):
        self._open()
        self._open(service_key=lifecycle.make_service_key(SERVER_ID, 'uuid-other'))
        self.assertEqual(SmsScanDecision.query.count(), 2)

    # ── the send attempt enriches the decision instead of duplicating it ──────

    def test_a_send_attempt_updates_the_open_decision(self):
        decision = self._open()
        row = self._send_log(status='sent')
        self.messaging._sms_record_scan_decision(
            RUN_ID, row, {'serviceKey': self.key, 'generation': 3},
            'ended', '09195758193', 'sent', None, SERVER_ID, 'Srv', EMAIL)
        db.session.commit()
        self.assertEqual(SmsScanDecision.query.count(), 1)
        stored = db.session.get(SmsScanDecision, decision.id)
        self.assertEqual(stored.disposition, 'submitted')
        self.assertEqual(stored.sms_send_log_id, row.id)
        self.assertEqual(stored.gateway_request_id, 'req-1')
        self.assertEqual(stored.gateway_job_id, 'job-1')

    def test_a_send_attempt_without_a_prior_decision_still_records_one(self):
        row = self._send_log(status='failed')
        self.messaging._sms_record_scan_decision(
            RUN_ID, row, {'serviceKey': self.key}, 'ended', '09195758193',
            'failed', 'send_failed', SERVER_ID, 'Srv', EMAIL)
        db.session.commit()
        decision = SmsScanDecision.query.one()
        self.assertEqual(decision.disposition, 'failed_retryable')
        self.assertEqual(decision.reason_code, 'send_failed')

    def test_a_skipped_send_is_recorded_as_suppressed(self):
        row = self._send_log(status='skipped')
        self.messaging._sms_record_scan_decision(
            RUN_ID, row, {'serviceKey': self.key}, 'ended', '09195758193',
            'skipped', 'no_template', SERVER_ID, 'Srv', EMAIL)
        db.session.commit()
        self.assertEqual(SmsScanDecision.query.one().disposition, 'suppressed')

    # ── the outcome the event reached is written back onto the decision ───────

    def _finalized(self, *, status, last_error=None):
        decision = self._open()
        event = self._event(status=status, last_error=last_error)
        self.messaging._sms_finalize_candidate_decision(decision.id, event)
        db.session.expire_all()
        return db.session.get(SmsScanDecision, decision.id)

    def test_a_deferred_candidate_records_the_real_reason(self):
        stored = self._finalized(status='retry', last_error='cooldown_active')
        self.assertEqual(stored.disposition, 'deferred')
        self.assertEqual(stored.reason_code, 'cooldown_active')

    def test_a_suppressed_candidate_is_recorded_as_suppressed(self):
        stored = self._finalized(status='skipped', last_error='opted_out_recheck')
        self.assertEqual(stored.disposition, 'suppressed')
        self.assertEqual(stored.reason_code, 'opted_out_recheck')

    def test_a_shadowed_candidate_is_suppressed_not_failed(self):
        stored = self._finalized(status='shadowed', last_error='shadow_mode')
        self.assertEqual(stored.disposition, 'suppressed')

    def test_a_terminal_failure_is_recorded_as_failed(self):
        stored = self._finalized(status='failed_terminal', last_error='no_template')
        self.assertEqual(stored.disposition, 'failed_terminal')
        self.assertEqual(stored.reason_code, 'no_template')

    def test_a_confirmed_send_is_recorded_as_confirmed(self):
        self.assertEqual(self._finalized(status='sent').disposition, 'confirmed')

    def test_gateway_acceptance_is_outstanding_not_confirmed(self):
        stored = self._finalized(status='gateway_accepted')
        self.assertEqual(stored.disposition, 'submitted')

    def test_the_event_linkage_is_stored(self):
        decision = self._open()
        event = self._event(status='retry', last_error='sms_disabled')
        event.gateway_request_id = 'req-linked'
        db.session.commit()
        self.messaging._sms_finalize_candidate_decision(decision.id, event)
        db.session.expire_all()
        stored = db.session.get(SmsScanDecision, decision.id)
        self.assertEqual(stored.notification_event_id, event.event_id)
        self.assertEqual(stored.gateway_request_id, 'req-linked')
        self.assertIsNotNone(stored.next_attempt_at)

    # ── end to end through the real delivery gate ─────────────────────────────

    def test_a_pipeline_deferral_leaves_a_durable_decision(self):
        """The real gate, no mocking: SMS automation disabled defers the event."""
        event = self._event()
        outcome, stop = self.messaging._deliver_depletion_event(
            event, cfg={'enabled': False}, templates={}, cooldown_hours=24,
            job_id=None, shadow=False)
        self.assertEqual((outcome, stop), ('deferred', False))
        # The event says retry; the candidate decision must say WHY.
        self.assertEqual(event.status, 'retry')
        self.assertEqual(SmsScanDecision.query.count(), 1)
        decision = SmsScanDecision.query.one()
        self.assertEqual(decision.disposition, 'deferred')
        self.assertEqual(decision.reason_code, event.last_error or 'sms_disabled')
        self.assertEqual(decision.client_email, EMAIL)
        # No send was attempted, so there is no attempt evidence -- and the
        # decision exists anyway.
        self.assertEqual(SmsSendLog.query.count(), 0)
        self.assertIsNone(decision.sms_send_log_id)

    def test_a_pipeline_deferral_needs_no_manual_run(self):
        event = self._event()
        self.messaging._deliver_depletion_event(
            event, cfg={'enabled': False}, templates={}, cooldown_hours=24,
            job_id=None, shadow=False)
        self.assertEqual(SmsScanRun.query.count(), 0)
        self.assertEqual(SmsScanDecision.query.count(), 1)

    def test_an_event_without_identity_creates_no_candidate(self):
        """client_email is NOT NULL, so a candidate without identity must not be
        invented; the event still records the skip."""
        event = self._event(email='')
        outcome, stop = self.messaging._deliver_depletion_event(
            event, cfg={'enabled': True}, templates={}, cooldown_hours=24,
            job_id=None, shadow=False)
        self.assertEqual((outcome, stop), ('skipped', False))
        self.assertEqual(SmsScanDecision.query.count(), 0)

    def test_delivering_the_same_event_twice_is_one_candidate(self):
        event = self._event()
        for _ in range(2):
            self.messaging._deliver_depletion_event(
                event, cfg={'enabled': False}, templates={}, cooldown_hours=24,
                job_id=None, shadow=False)
        self.assertEqual(SmsScanDecision.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
