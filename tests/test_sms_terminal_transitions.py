"""A warning must never kill the terminal notification that follows it.

The production bug these tests pin: an account that received the `low_volume`
warning yesterday reached `volume_ended` today, the pipeline detected the
transition and created the event -- and delivery refused it, because the cooldown
looked at ANY automated message for that (account, server) and then closed the
event as a terminal `skipped` with no retry. Two distinct defects:

* the cooldown was not scoped to a notification kind, so a warning consumed the
  budget of a different, more severe message;
* the cooldown branch made the event terminal instead of deferring it, so even
  after the window expired the transition was gone for good.

The tests below drive the real delivery gate (`_deliver_depletion_event`) against
a real database, and assert the OUTCOME (what reached the gateway) plus the
event's own durable status, because "deferred" and "dropped" look identical to a
customer and must not be identical in the ledger.
"""
import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock


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
    WhatsappBotLog,
)
from panel.services import lifecycle, telemetry_state  # noqa: E402

GB = 1024 ** 3
SERVER_ID = 7
EMAIL = 'h34-09195758193@example.com'


class TerminalTransitionDeliveryTests(unittest.TestCase):
    """One account, one server, the real delivery path, real cooldown rows."""

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
        ServiceNotificationEvent.query.delete()
        ServiceObservedState.query.delete()
        WhatsappBotLog.query.delete()
        db.session.commit()
        self.key = lifecycle.make_service_key(SERVER_ID, 'uuid-ended')
        self.event = self._event('volume_ended', 'ended')
        self.sent = []
        patches = [
            mock.patch.object(messaging, '_cached_snapshot_clients',
                              lambda *_a, **_k: [self._row()]),
            # The delivery path translates the canonical state into the SMS monitor
            # vocabulary before every gate; the fixtures below are monitor states.
            mock.patch.object(messaging, '_classify_cached_client_state',
                              lambda *_a, **_k: self.monitor_state),
            mock.patch.object(messaging, '_sms_depletion_state_still_valid',
                              lambda *_a, **_k: (True, '')),
            mock.patch.object(messaging, '_sms_account_opted_out',
                              lambda *_a, **_k: False),
            mock.patch.object(messaging, '_sms_has_manual_review',
                              lambda *_a, **_k: False),
            mock.patch.object(messaging, '_extract_iran_mobile_from_text',
                              lambda *_a, **_k: '09195758193'),
            mock.patch.object(messaging, '_sms_in_quiet_hours',
                              lambda *_a, **_k: False),
            mock.patch.object(messaging, '_sms_gateway_ready',
                              lambda *_a, **_k: (True, None, 200)),
            mock.patch.object(messaging, '_render_monitor_state_template',
                              lambda *_a, **_k: 'your volume ended'),
            mock.patch.object(messaging, '_send_sms_via_gmweb',
                              side_effect=self._fake_send),
            mock.patch.object(messaging, '_sms_log_row', lambda *_a, **_k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # ── fixtures ──────────────────────────────────────────────────────────────

    monitor_state = 'ended'
    _serial = 0

    def _event(self, canonical_state, monitor_state, previous='active'):
        type(self)._serial += 1
        event_id = 'st:terminal-%s-%d' % (canonical_state, type(self)._serial)
        event = ServiceNotificationEvent(
            event_id=event_id,
            service_key=self.key, server_id=SERVER_ID,
            client_uuid='uuid-ended', client_email=EMAIL,
            state=canonical_state, previous_state=previous,
            notification_kind=canonical_state, state_version=1,
            lifecycle_generation=1, observed_at=datetime.utcnow(),
            source='transition', status='pending', attempt_count=0,
            next_attempt_at=datetime.utcnow(),
            idempotency_key='depletion-%s' % event_id,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.session.add(event)
        db.session.commit()
        self.addCleanup(self._forget, event)
        return event

    def _forget(self, event):
        try:
            db.session.rollback()
            row = ServiceNotificationEvent.query.filter_by(event_id=event.event_id).first()
            if row is not None:
                db.session.delete(row)
                db.session.commit()
        except Exception:
            db.session.rollback()

    def _row(self):
        return {'email': EMAIL, 'id': 'uuid-ended', 'remaining_bytes': 0,
                'totalGB': 20 * GB, 'up': 20 * GB, 'down': 0,
                'expiryTimestamp': 0, 'comment': '09195758193',
                'service_state': 'volume_ended', 'service_state_tag': 'ended'}

    def _log(self, event_name, hours_ago):
        db.session.add(WhatsappBotLog(email=EMAIL.lower(), server_id=SERVER_ID,
                                      event=event_name,
                                      sent_at=datetime.utcnow() - timedelta(hours=hours_ago)))
        db.session.commit()

    def _fake_send(self, to, text, cfg=None, priority=None, idempotency_key=None,
                   meta=None):
        self.sent.append({'to': to, 'idempotency_key': idempotency_key})
        return {'sent': True, 'status_code': 202, 'request_id': 'req-1',
                'provider': 'gmweb'}

    def _deliver(self, *, cfg=None, generation=1):
        cfg = cfg or {'enabled': True, 'trigger_ended': True, 'trigger_low_volume': True,
                      'trigger_expired': True, 'trigger_near_expiry': True,
                      'cooldown_hours': {'ended': 24, 'low_volume': 24,
                                         'expired': 48, 'near_expiry': 24}}
        with mock.patch.object(self.messaging.lifecycle_service, 'generation_state',
                               return_value={'generation': generation,
                                             'last_lifecycle_change_at': None}):
            return self.messaging._deliver_depletion_event(
                self.event, cfg=cfg,
                templates={self.messaging.SMS_STATE_TO_MONITOR_TPL[self.monitor_state]: 'tpl'},
                cooldown_hours=cfg['cooldown_hours'], job_id='test', shadow=False)

    def _status(self):
        db.session.expire(self.event)
        db.session.refresh(self.event)
        return self.event.status

    # ── 1. the reported bug ───────────────────────────────────────────────────

    def test_yesterdays_warning_does_not_consume_the_ended_transition(self):
        # 20 hours: inside the ended cooldown window and inside the low_volume one,
        # which is exactly the state the old code could not tell apart.
        self._log('sms_low_volume', hours_ago=20)
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'sent')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self._status(), 'sent')

    def test_yesterdays_near_expiry_warning_does_not_consume_the_expired_transition(self):
        self.monitor_state = 'expired'
        self.event = self._event('expired', 'expired', previous='near_expiry')
        self._log('sms_near_expiry', hours_ago=20)
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'sent')
        self.assertEqual(len(self.sent), 1)

    def test_the_legacy_combined_depletion_row_still_holds_the_warning_cooldown(self):
        # The pre-granular trigger wrote one row for either warning state. Keeping it
        # in the warning kinds is what stops an upgrade from re-texting everyone who
        # was warned by the old path...
        self.monitor_state = 'low_volume'
        self.event = self._event('volume_low', 'low_volume', previous='active')
        self._log('depletion', hours_ago=2)
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'deferred')
        self.assertEqual(self.sent, [])
        # ...while a terminal state still ignores it, because it is a different fact.
        self.monitor_state = 'ended'
        self.event = self._event('volume_ended', 'ended')
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'sent')
        self.assertEqual(len(self.sent), 1)

    # ── 2. a same-kind cooldown defers, it never drops ────────────────────────

    def test_a_same_kind_cooldown_defers_with_the_remaining_time(self):
        self._log('sms_ended', hours_ago=2)
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'deferred')
        self.assertEqual(self.sent, [])
        db.session.refresh(self.event)
        self.assertEqual(self.event.status, 'retry')
        self.assertEqual(self.event.last_error, 'cooldown_active')
        self.assertIsNotNone(self.event.next_attempt_at)
        remaining = (self.event.next_attempt_at - datetime.utcnow()).total_seconds()
        # 24 h cooldown, 2 h elapsed -> ~22 h left, never a terminal status.
        self.assertGreater(remaining, 21 * 3600)
        self.assertLess(remaining, 23 * 3600)

    def test_the_cooldown_is_shared_across_channels_within_a_kind(self):
        # The Telegram warning is the same notification for the same state: telling
        # the customer on another channel must still hold the SMS back. (The terminal
        # states have no Telegram counterpart -- the bot only warns.)
        self.monitor_state = 'low_volume'
        self.event = self._event('volume_low', 'low_volume')
        self._log('tg_low_volume', hours_ago=1)
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'deferred')

    def test_an_unmapped_state_has_no_cooldown_to_wait_for(self):
        self.assertEqual(self.messaging.cooldown_events_for_state('royalty'), ())
        self.assertEqual(
            self.messaging._cooldown_remaining_seconds(EMAIL.lower(), SERVER_ID,
                                                       'royalty', 24), 0)

    def test_an_expired_cooldown_row_is_ignored(self):
        self._log('sms_ended', hours_ago=25)
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'sent')

    # ── 3. the fences stay: a deferred event still dies with its generation ───

    def test_a_renewal_supersedes_a_deferred_terminal_event(self):
        self._log('sms_ended', hours_ago=2)
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'deferred')
        # The account was renewed while the event waited: the next attempt must
        # retire it rather than deliver a stale terminal notice.
        outcome, _stop = self._deliver(generation=2)
        self.assertEqual(outcome, 'superseded')
        self.assertEqual(self.sent, [])
        db.session.refresh(self.event)
        self.assertEqual(self.event.superseded_reason, 'lifecycle_generation_advanced')

    # ── 4. an operator-disabled trigger is explicit, and recoverable ──────────

    def test_a_disabled_trigger_defers_and_names_the_setting(self):
        cfg = {'enabled': True, 'trigger_ended': False,
               'cooldown_hours': {'ended': 24}}
        outcome, _stop = self._deliver(cfg=cfg)
        self.assertEqual(outcome, 'deferred')
        self.assertEqual(self.sent, [])
        db.session.refresh(self.event)
        self.assertEqual(self.event.status, 'retry')
        self.assertEqual(self.event.last_error, 'trigger_disabled_by_operator:ended')

    def test_enabling_the_trigger_later_delivers_what_it_held(self):
        # The reason this is a deferral: a material transition only creates an event
        # once, so closing it on a disabled trigger would leave a permanent hole in
        # the account's history that switching the trigger on could never fill.
        self._deliver(cfg={'enabled': True, 'trigger_ended': False,
                           'cooldown_hours': {'ended': 24}})
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, 'sent')
        self.assertEqual(len(self.sent), 1)

    def test_the_disabled_trigger_retry_is_hourly_not_a_hot_loop(self):
        before = datetime.utcnow()
        self._deliver(cfg={'enabled': True, 'trigger_ended': False,
                           'cooldown_hours': {'ended': 24}})
        db.session.refresh(self.event)
        gap = (self.event.next_attempt_at - before).total_seconds()
        self.assertGreater(gap, 55 * 60)
        self.assertLessEqual(gap, 61 * 60)


class CooldownKindTests(unittest.TestCase):
    """The kind map itself: what counts as 'already told this' for each state."""

    def setUp(self):
        from panel.jobs import messaging
        self.messaging = messaging

    def test_each_state_maps_to_its_own_sms_event(self):
        for state in ('near_expiry', 'low_volume', 'expired', 'ended'):
            self.assertIn('sms_%s' % state,
                          self.messaging.cooldown_events_for_state(state))

    def test_warning_kinds_include_the_legacy_combined_row_and_endings_do_not(self):
        self.assertIn('depletion', self.messaging.cooldown_events_for_state('low_volume'))
        self.assertIn('depletion', self.messaging.cooldown_events_for_state('near_expiry'))
        self.assertNotIn('depletion', self.messaging.cooldown_events_for_state('ended'))
        self.assertNotIn('depletion', self.messaging.cooldown_events_for_state('expired'))

    def test_no_kind_contains_another_kinds_event(self):
        # The whole defect in one assertion: no state's cooldown may be triggered by
        # another state's message. The single documented exception is the legacy
        # combined row, which the two WARNING kinds may share -- and which the two
        # terminal kinds must not see at all.
        kinds = {state: set(self.messaging.cooldown_events_for_state(state))
                 for state in ('near_expiry', 'low_volume', 'expired', 'ended')}
        for state, events in kinds.items():
            for other, other_events in kinds.items():
                if other == state:
                    continue
                shared = events & other_events
                self.assertEqual(shared, {'depletion'} if shared else set(),
                                 '%s and %s share an undocumented cooldown event'
                                 % (state, other))
        self.assertNotIn('depletion', kinds['ended'] | kinds['expired'])


if __name__ == '__main__':
    unittest.main()
