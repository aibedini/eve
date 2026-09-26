"""Fresh telemetry is the detector; the periodic scan is the repair net.

Every test here pins a concrete failure of the OLD shape, where the SMS scan read
the snapshot and asked "who looks depleted right now":

* the reported bug -- X-UI said Volume Ended while the dashboard still showed 2 GB,
  so no scan candidate ever existed and no reminder was ever queued;
* repeated polls of an already-ended service (one reminder per DETECTION, not per
  poll);
* an out-of-order panel response reverting the ledger and producing a second event
  for one logical depletion;
* a renewal that lands while a reminder is queued (the row must be retired, not
  delivered);
* two workers claiming the same event (exactly one send).
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
from panel.core import fetch_sequence, refresh_policy  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.models import ServiceNotificationEvent, ServiceObservedState  # noqa: E402
from panel.services import depletion_pipeline, lifecycle, telemetry_state  # noqa: E402

GB = 1024 ** 3


def _state(state_key, remaining_gb=40, total_gb=50, expiry_ms=0):
    return {
        'service_state': state_key,
        'service_state_tag': 'ok',
        'remaining_bytes': (None if remaining_gb is None else int(remaining_gb * GB)),
        'total_bytes': int(total_gb * GB),
        'expiry_time': expiry_ms,
        'telemetry_updated_at': None,
    }


class TelemetryTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        ServiceNotificationEvent.query.delete()
        ServiceObservedState.query.delete()
        db.session.commit()
        fetch_sequence.reset()
        self.key = lifecycle.make_service_key(4, "uuid-xyz")
        self.ident = {"client_uuid": "uuid-xyz", "client_email": "user@example.com"}

    def _record(self, state, **kw):
        return telemetry_state.record_observations(
            4, [(self.key, state, self.ident)], **kw)

    def test_first_observation_is_a_baseline_not_a_transition(self):
        # A deploy introduces the ledger to an install full of already-expired
        # accounts; nothing may be sent for a state Eve has never observed before.
        counts = self._record(_state('volume_ended', remaining_gb=0))
        self.assertEqual(counts['baselines'], 1)
        self.assertEqual(counts['events_created'], 0)
        self.assertEqual(ServiceNotificationEvent.query.count(), 0)

    def test_the_reported_bug_two_gigabytes_to_ended_creates_one_event(self):
        self._record(_state('active', remaining_gb=2))
        counts = self._record(_state('volume_ended', remaining_gb=0))
        self.assertEqual(counts['transitions'], 1)
        self.assertEqual(counts['events_created'], 1)
        event = ServiceNotificationEvent.query.one()
        self.assertEqual(event.state, 'volume_ended')
        self.assertEqual(event.notification_kind, 'volume_ended')
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.idempotency_key, 'depletion-%s' % event.event_id)

    def test_repeated_polls_of_an_ended_service_never_duplicate(self):
        self._record(_state('active', remaining_gb=2))
        self._record(_state('volume_ended', remaining_gb=0))
        for _ in range(5):
            self._record(_state('volume_ended', remaining_gb=0))
        self.assertEqual(ServiceNotificationEvent.query.count(), 1)
        row = ServiceObservedState.query.one()
        self.assertEqual(row.state_version, 1)

    def test_a_moved_byte_counter_is_not_a_transition(self):
        # 40 GB -> 39 GB crosses no threshold: no version, no event, and not even a
        # row write (the write floor), because this runs on every poll of every
        # client in the install.
        self._record(_state('active', remaining_gb=40))
        row = ServiceObservedState.query.one()
        first_stamp = row.updated_at
        counts = self._record(_state('active', remaining_gb=39))
        self.assertEqual(counts['transitions'], 0)
        self.assertEqual(ServiceNotificationEvent.query.count(), 0)
        db.session.refresh(row)
        self.assertEqual(row.updated_at, first_stamp)
        self.assertEqual(row.state_version, 0)

    def test_two_notifiable_states_in_a_row_produce_two_events(self):
        self._record(_state('active', remaining_gb=5))
        self._record(_state('volume_low', remaining_gb=1))
        self._record(_state('volume_ended', remaining_gb=0))
        states = sorted(e.state for e in ServiceNotificationEvent.query.all())
        self.assertEqual(states, ['volume_ended', 'volume_low'])

    def test_stronger_terminal_state_supersedes_queued_warning(self):
        self._record(_state('active', remaining_gb=5))
        self._record(_state('volume_low', remaining_gb=1))
        self._record(_state('volume_ended', remaining_gb=0))
        events = {event.state: event for event in ServiceNotificationEvent.query.all()}
        self.assertEqual(events['volume_low'].status, 'superseded')
        self.assertEqual(events['volume_ended'].status, 'pending')

    def test_reconciliation_repairs_missing_transition_obligation_once(self):
        self._record(_state('active', remaining_gb=2))
        self._record(_state('volume_ended', remaining_gb=0))
        ServiceNotificationEvent.query.delete()
        db.session.commit()
        first = self._record(_state('volume_ended', remaining_gb=0),
                             source='reconciliation')
        second = self._record(_state('volume_ended', remaining_gb=0),
                              source='reconciliation')
        self.assertEqual(first['events_created'], 1)
        self.assertEqual(second['events_created'], 0)
        self.assertEqual(ServiceNotificationEvent.query.count(), 1)
        self.assertEqual(ServiceNotificationEvent.query.one().source,
                         'reconciliation_recovery')

    def test_out_of_order_response_is_refused_by_the_ticket(self):
        # A slow read of an EARLIER state must not be applied after a newer one: it
        # would revert the ledger to "active", and the next fresh read would then
        # open a SECOND event for one logical depletion.
        first = fetch_sequence.begin(4)
        second = fetch_sequence.begin(4)
        self.assertTrue(fetch_sequence.accept(4, second))
        self.assertFalse(fetch_sequence.accept(4, first))
        self.assertEqual(fetch_sequence.last_accepted(4), second)

    def test_claiming_is_exclusive_and_a_dead_lease_is_reclaimed(self):
        self._record(_state('active', remaining_gb=2))
        self._record(_state('volume_ended', remaining_gb=0))
        claimed = telemetry_state.claim_events(limit=5, owner='worker-a')
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].status, 'sending')
        self.assertEqual(telemetry_state.claim_events(limit=5, owner='worker-b'), [])
        # A crashed worker leaves the lease behind; it must come back, not vanish.
        claimed[0].claimed_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        self.assertEqual(telemetry_state.reclaim_expired_leases(), 1)
        self.assertEqual(telemetry_state.claim_events(limit=5, owner='worker-b')[0].status,
                         'sending')

    def test_terminal_notice_keeps_retrying_after_backoff_ladder(self):
        self._record(_state('active', remaining_gb=2))
        self._record(_state('volume_ended', remaining_gb=0))
        event_id = ServiceNotificationEvent.query.one().event_id
        delays = []
        # Terminal notices continue at a low frequency after the initial ladder.
        clock = datetime.utcnow()
        for _ in range(telemetry_state.MAX_ATTEMPTS + 2):
            # Step past every rung of the ladder so this exercises the LADDER, not
            # the clock: each rung's backoff is what the next iteration skips.
            clock = clock + timedelta(hours=4)
            claimed = telemetry_state.claim_events(limit=1, owner='w', now=clock)
            if not claimed:
                break
            delays.append(telemetry_state.mark_retry(claimed[0], 'http_429', now=clock))
        final = ServiceNotificationEvent.query.filter_by(event_id=event_id).one()
        self.assertEqual(final.status, 'retry')
        self.assertEqual(final.attempt_count, telemetry_state.MAX_ATTEMPTS + 2)
        self.assertEqual(delays[0], 30)
        self.assertEqual(delays[-1], 3600)
        self.assertEqual(len(delays), telemetry_state.MAX_ATTEMPTS + 2)

    def test_renewal_retires_queued_reminders(self):
        self._record(_state('active', remaining_gb=2))
        self._record(_state('volume_ended', remaining_gb=0))
        retired = telemetry_state.supersede_pending(
            self.key, 'lifecycle_generation_advanced', max_generation=0)
        self.assertEqual(retired, 1)
        event = ServiceNotificationEvent.query.one()
        self.assertEqual(event.status, 'superseded')
        self.assertEqual(event.superseded_reason, 'lifecycle_generation_advanced')

    def test_metrics_never_carry_customer_identifiers(self):
        self._record(_state('active', remaining_gb=2))
        self._record(_state('volume_ended', remaining_gb=0))
        snapshot = telemetry_state.metrics()
        self.assertTrue(snapshot['available'])
        self.assertEqual(snapshot['pending'], 1)
        self.assertEqual(snapshot['observed_services'], 1)
        blob = repr(snapshot)
        self.assertNotIn('user@example.com', blob)
        self.assertNotIn('uuid-xyz', blob)

    def test_observations_use_the_canonical_service_key(self):
        row = {'email': 'User@Example.com', 'id': 'panel-client-id',
               'remaining_bytes': 0, 'totalGB': 50 * GB, 'up': 50 * GB, 'down': 0,
               'expiryTimestamp': 0, 'service_state': 'volume_ended',
               'service_state_tag': 'ended', 'raw_client': {'id': 'panel-client-id'}}
        observations = depletion_pipeline.observations_from_inbounds(
            [{'server_id': 4, 'clients': [row]}])
        self.assertEqual(len(observations), 1)
        service_key, state, identity = observations[0]
        self.assertEqual(service_key, lifecycle.make_service_key(4, 'panel-client-id'))
        self.assertEqual(identity['client_email'], 'user@example.com')
        self.assertEqual(state['service_state'], 'volume_ended')

    def test_reconciliation_detects_a_transition_nobody_observed(self):
        # The blind spot itself: the ledger holds "active" from before, the snapshot
        # now says ended, and the transition happened while the pipeline was not
        # watching (deploy, DB outage). The repair pass must still produce the event.
        self._record(_state('active', remaining_gb=2))
        from panel.core.redis_client import GLOBAL_SERVER_DATA
        block = {'server_id': 4, 'server_name': 'srv', 'clients': [
            {'email': 'user@example.com', 'id': 'uuid-xyz', 'remaining_bytes': 0,
             'totalGB': 50 * GB, 'up': 50 * GB, 'down': 0, 'expiryTimestamp': 0,
             'service_state': 'volume_ended', 'service_state_tag': 'ended',
             'raw_client': {'id': 'uuid-xyz'}}]}
        original = list(GLOBAL_SERVER_DATA.get('inbounds') or [])
        GLOBAL_SERVER_DATA['inbounds'] = [block]
        try:
            totals = depletion_pipeline.reconcile_snapshot(source='reconciliation')
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = original
        self.assertEqual(totals['events_created'], 1)
        self.assertEqual(totals['transitions'], 1)
        self.assertEqual(ServiceNotificationEvent.query.one().source, 'reconciliation')

    def test_mode_flag_decides_who_may_send(self):
        cases = {'off': (False, False, True),
                 'shadow': (True, False, True),
                 'on': (True, True, False),
                 'nonsense': (False, False, True)}
        for value, expected in cases.items():
            with self.subTest(mode=value):
                os.environ['EVE_DEPLETION_EVENT_PIPELINE'] = value
                try:
                    actual = (depletion_pipeline.detection_enabled(),
                              depletion_pipeline.delivery_enabled(),
                              depletion_pipeline.legacy_sender_active())
                    self.assertEqual(actual, expected)
                finally:
                    os.environ.pop('EVE_DEPLETION_EVENT_PIPELINE', None)


class FetchPipelineWiringTests(unittest.TestCase):
    """The wiring itself: the processed inbound block must reach the ledger.

    Every other test in this file calls the pipeline directly, and that is exactly how
    a broken call shape survived them: `_record_fetch_transitions` used to wrap the
    processed INBOUND list as if it were the client list, so it found no emails and
    recorded nothing -- green unit tests, an empty ledger in production. These tests
    drive the real helper with the real shape `process_inbounds()` returns.
    """

    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        from panel.jobs import schedulers
        self.schedulers = schedulers
        ServiceNotificationEvent.query.delete()
        ServiceObservedState.query.delete()
        db.session.commit()
        os.environ.pop("EVE_DEPLETION_EVENT_PIPELINE", None)

    def _client(self, email, state, remaining_gb, client_uuid):
        return {
            "email": email, "id": client_uuid, "raw_client": {"id": client_uuid,
                                                            "email": email,
                                                            "totalGB": 10 * GB,
                                                            "expiryTime": 0},
            "totalGB": 10 * GB, "up": int((10 - remaining_gb) * GB), "down": 0,
            "remaining_bytes": int(remaining_gb * GB), "expiryTimestamp": 0,
            "service_state": state, "service_state_tag": "ok",
        }

    def test_a_processed_inbound_block_reaches_the_ledger(self):
        block = {"server_id": 5, "server_name": "srv", "clients": [
            self._client("a@example.com", "volume_ended", 0, "uuid-a"),
            self._client("b@example.com", "active", 4, "uuid-b"),
        ]}
        self.schedulers._record_fetch_transitions(5, [block])
        rows = {row.client_email: row for row in ServiceObservedState.query.all()}
        self.assertEqual(sorted(rows), ["a@example.com", "b@example.com"])
        self.assertEqual(rows["a@example.com"].last_state, "volume_ended")
        # First sighting of each service is a baseline: it records, and stays silent.
        self.assertEqual(ServiceNotificationEvent.query.count(), 0)

    def test_the_end_to_end_hook_creates_one_event_when_the_state_flips(self):
        block = {"server_id": 5, "server_name": "srv", "clients": [
            self._client("a@example.com", "active", 2, "uuid-a")]}
        self.schedulers._record_fetch_transitions(5, [block])
        block["clients"][0]["remaining_bytes"] = 0
        block["clients"][0]["service_state"] = "volume_ended"
        self.schedulers._record_fetch_transitions(5, [block])
        events = ServiceNotificationEvent.query.all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].state, "volume_ended")
        self.assertEqual(events[0].service_key, "eve:5:uuid-a")
        self.assertEqual(events[0].source, "transition")

    def test_a_raised_ledger_failure_does_not_escape_the_fetch_path(self):
        with mock.patch.object(telemetry_state, "record_observations",
                               side_effect=RuntimeError("boom")):
            self.schedulers._record_fetch_transitions(5, [{"server_id": 5, "clients": [
                self._client("a@example.com", "volume_ended", 0, "uuid-a")]}])

class WatchPropagationTests(unittest.TestCase):
    """The dashboard is served by a WEB process; the loop is another process."""

    def setUp(self):
        refresh_policy.reset_state()

    def tearDown(self):
        refresh_policy.reset_state()

    def test_a_watched_server_is_due_at_the_active_interval(self):
        # Local case (single process): the mark must shorten the schedule, not just
        # record an intention somewhere nobody reads.
        refresh_policy.note_server_result(11, True, now=0)
        idle_interval = refresh_policy.server_interval(11, now=0)
        self.assertEqual(idle_interval, refresh_policy.server_idle_seconds())
        refresh_policy.note_watched_servers([11])
        self.assertTrue(refresh_policy.is_server_watched(11))
        self.assertEqual(refresh_policy.server_interval(11),
                         refresh_policy.server_active_seconds())
        self.assertTrue(refresh_policy.server_due(11))

    def test_a_panel_in_backoff_is_not_retried_because_someone_looked(self):
        refresh_policy.note_server_result(12, False)
        refresh_policy.defer_server_until(12, 10 ** 9)
        refresh_policy.note_watched_servers([12])
        self.assertFalse(refresh_policy.server_due(12))
        self.assertEqual(refresh_policy.server_interval(12),
                         refresh_policy.server_backoff_base())

    def test_watch_marks_are_reported_by_the_doctor_snapshot(self):
        refresh_policy.note_watched_servers([13])
        marks = refresh_policy.server_watch_marks()
        self.assertIn('13', marks['local'])
        self.assertIn('13', marks['shared'])


class DepletionEventDeliveryTests(unittest.TestCase):
    """The outbox worker: what may reach the gateway, and what must not."""

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
        db.session.commit()
        self.key = lifecycle.make_service_key(6, "uuid-ended")
        self.event = ServiceNotificationEvent(
            event_id="st:delivery-test", service_key=self.key, server_id=6,
            client_uuid="uuid-ended", client_email="user@example.com",
            state="volume_ended", previous_state="active",
            notification_kind="volume_ended", state_version=1,
            lifecycle_generation=1, observed_at=datetime.utcnow(),
            source="transition", status="pending", attempt_count=0,
            next_attempt_at=datetime.utcnow(),
            idempotency_key="depletion-st:delivery-test",
            created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.session.add(self.event)
        db.session.commit()
        self.sent = []
        patches = [
            mock.patch.object(messaging, "_cached_snapshot_clients",
                              lambda *_a, **_k: [self._row()]),
            # The SMS-monitor vocabulary: the delivery path translates the canonical
            # state to it before every gate (trigger, cooldown, template).
            mock.patch.object(messaging, "_classify_cached_client_state",
                              lambda *_a, **_k: "ended"),
            mock.patch.object(messaging, "_sms_depletion_state_still_valid",
                              lambda *_a, **_k: (True, "")),
            mock.patch.object(messaging, "_sms_account_opted_out",
                              lambda *_a, **_k: False),
            mock.patch.object(messaging, "_sms_has_manual_review",
                              lambda *_a, **_k: False),
            mock.patch.object(messaging, "_recent_bot_message_within",
                              lambda *_a, **_k: False),
            mock.patch.object(messaging, "_extract_iran_mobile_from_text",
                              lambda *_a, **_k: "09121234567"),
            mock.patch.object(messaging, "_sms_in_quiet_hours",
                              lambda *_a, **_k: False),
            mock.patch.object(messaging, "_sms_gateway_ready",
                              lambda *_a, **_k: (True, None, 200)),
            mock.patch.object(messaging, "_render_monitor_state_template",
                              lambda *_a, **_k: "your volume ended"),
            mock.patch.object(messaging, "_send_sms_via_gmweb",
                              side_effect=self._fake_send),
            mock.patch.object(messaging, "_sms_log_row",
                              lambda *_a, **_k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _row(self):
        return {"email": "user@example.com", "id": "uuid-ended",
                "remaining_bytes": 0, "totalGB": 10 * GB, "up": 10 * GB,
                "down": 0, "expiryTimestamp": 0, "comment": "09121234567",
                "service_state": "volume_ended", "service_state_tag": "ended"}

    def _fake_send(self, to, text, cfg=None, priority=None, idempotency_key=None,
                   meta=None):
        self.sent.append({"to": to, "idempotency_key": idempotency_key, "meta": meta})
        return {"sent": True, "status_code": 202, "request_id": "req-1",
                "provider": "gmweb"}

    def _deliver(self, *, shadow=False, generation=1):
        with mock.patch.object(self.messaging.lifecycle_service, "generation_state",
                               return_value={"generation": generation,
                                             "last_lifecycle_change_at": None}):
            return self.messaging._deliver_depletion_event(
                self.event, cfg={"enabled": True, "trigger_ended": True,
                                 "cooldown_hours": {"ended": 24}},
                templates={self.messaging.SMS_STATE_TO_MONITOR_TPL["ended"]: "tpl"},
                cooldown_hours={"ended": 24}, job_id="test", shadow=shadow)

    def test_a_renewed_service_supersedes_its_queued_reminder(self):
        # RACE A on the new path: the event was created before the renewal and is
        # being delivered after it. The generation fence must retire it, and the
        # gateway must never be called.
        outcome, stop = self._deliver(generation=2)
        self.assertEqual(outcome, "superseded")
        self.assertFalse(stop)
        self.assertEqual(self.sent, [])
        db.session.refresh(self.event)
        self.assertEqual(self.event.status, "superseded")
        self.assertEqual(self.event.superseded_reason, "lifecycle_generation_advanced")

    def test_shadow_mode_records_instead_of_sending(self):
        # The rollout mode that makes the migration safe: the pipeline proves what it
        # WOULD send while the legacy scan keeps sending, so exactly one path reaches
        # the gateway and the new detector can be trusted before it is enabled.
        outcome, _stop = self._deliver(shadow=True)
        self.assertEqual(outcome, "shadowed")
        self.assertEqual(self.sent, [])
        db.session.refresh(self.event)
        self.assertEqual(self.event.status, "shadowed")

    def test_the_delivered_message_carries_the_generation_and_a_stable_key(self):
        outcome, _stop = self._deliver()
        self.assertEqual(outcome, "sent")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["idempotency_key"], self.event.idempotency_key)
        meta = self.sent[0]["meta"]
        self.assertEqual(meta["serviceKey"], self.key)
        self.assertEqual(meta["generation"], 1)
        self.assertEqual(meta["notificationKind"], "volume_ended")
        self.assertTrue(meta["requiresValidation"])

    def test_a_row_deleted_mid_delivery_is_reported_not_raised(self):
        # What an out-of-band delete (retention pruning, an operator cleanup, a test
        # harness) leaves behind: the worker session still holds the instance while the
        # row is gone, so the next attribute access on the expired instance raises
        # ObjectDeletedError. Reading event.event_id inside the error handler turned a
        # HANDLED delivery failure into a traceback in the worker log.
        from sqlalchemy import text as _sql_text
        event_id = self.event.event_id
        db.session.commit()   # expire the instance, as any commit in the worker does
        db.session.execute(
            _sql_text("DELETE FROM service_notification_events WHERE event_id = :e"),
            {"e": event_id})
        db.session.commit()
        with mock.patch.object(telemetry_state, "claim_events", lambda **_k: [self.event]), \
             mock.patch.object(self.messaging, "_get_sms_runtime_settings",
                               lambda: {"enabled": True}), \
             mock.patch.object(self.messaging, "_get_monitor_settings_cached",
                               lambda _cfg: {"templates": {}}), \
             mock.patch.object(self.messaging, "_deliver_depletion_event",
                               side_effect=RuntimeError("boom")):
            result = self.messaging.run_depletion_event_outbox(limit=1)
        self.assertEqual(result["claimed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["sent"], 0)

if __name__ == '__main__':
    unittest.main()
