"""
Renewal vs depletion-SMS consistency: lifecycle generation + invalidation.

The two races this file pins down (see panel/services/lifecycle.py):

  RACE A  a reminder classified before the renewal is already sitting in the SMS
          gateway queue when the customer renews;
  RACE B  a worker reads a cached panel snapshot that predates the renewal and
          creates a brand-new reminder afterwards.

Every test here is a regression test for a concrete failure mode, not a
coverage exercise: dropping one of these guards re-opens a real customer-visible
bug (an 'expired' SMS arriving minutes after the customer paid).
"""
import base64
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL',
                    'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
import panel.jobs.messaging as messaging  # noqa: E402
from app import GLOBAL_SERVER_DATA, app, db  # noqa: E402
from panel.models import (  # noqa: E402
    ServiceLifecycleState,
    ServiceNotificationEvent,
    ServiceNotificationOutbox,
    ServiceObservedState,
    SmsSendLog,
    WhatsappBotLog,
)
from panel.services import lifecycle as lifecycle_service  # noqa: E402


GB = 1024 ** 3
DAY_MS = 86400000




def _iso(moment):
    return moment.replace(microsecond=0).isoformat()


class _AppContextTestCase(unittest.TestCase):
    """Base for every test in this file: gives it the workers' app context."""

    @classmethod
    def setUpClass(cls):
        # Self-sufficient schema. Several suites in this repository drop every
        # table in their tearDownClass (test_renew_enable, test_audit_chain, ...),
        # and the app-level migration run happens once at import time, so a file
        # that runs after one of those would otherwise query a schema that no
        # longer exists. create_all() is idempotent and only fills in what is
        # missing.
        with app.app_context():
            db.create_all()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

    def setUp(self):
        if type(self) is _AppContextTestCase:
            self.skipTest("base class")
        _push_app_context(self)


def _drop_session():
    try:
        db.session.remove()
    except Exception:
        pass


def _push_app_context(case):
    """Give the test the same Flask app context the background workers run in.

    The lifecycle service and the scan both touch `db.session`; without an app
    context every one of them raises 'Working outside of application context'.
    Registering the pop as a cleanup keeps it ordered after the test body even
    when setUp later raises elsewhere."""
    ctx = app.app_context()
    ctx.push()
    case.addCleanup(ctx.pop)
    case.addCleanup(lambda: _drop_session())


def _raw_client(email='bob', expiry=0, total=0, enable=True, **overrides):
    raw = {
        'id': 'uuid-' + email,
        'email': email,
        'comment': '0912' + '1234567',
        'enable': enable,
        'expiryTime': expiry,
        'totalGB': total,
        'subId': 'sub' + email,
    }
    raw.update(overrides)
    return raw


def _cache_row(server_id, raw, up=0, down=0, observed_at=None):
    row = {
        'server_id': server_id,
        'inbound_id': 1,
        'email': raw.get('email'),
        'id': raw.get('id'),
        'up': up,
        'down': down,
        'totalGB': raw.get('totalGB'),
        'expiryTimestamp': raw.get('expiryTime'),
        'enable': raw.get('enable', True),
        'comment': raw.get('comment') or '',
        'raw_client': raw,
    }
    if observed_at is not None:
        row['config_updated_at'] = _iso(observed_at)
        row['telemetry_updated_at'] = _iso(observed_at)
    return row


def _seed_snapshot(server_id, rows):
    GLOBAL_SERVER_DATA['inbounds'] = [
        {'server_id': server_id, 'id': 1, 'protocol': 'vless', 'clients': rows}
    ]
    GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()


class LifecycleIdentityTests(_AppContextTestCase):
    q1 = 'Durable, canonical service identity (one helper, no inline strings).'

    def test_service_key_is_eve_server_uuid(self):
        self.assertEqual(lifecycle_service.make_service_key(7, 'abc'), 'eve:7:abc')
        self.assertEqual(lifecycle_service.make_service_key('7', 'abc'), 'eve:7:abc')

    def test_service_key_never_uses_the_phone_or_the_email_as_identity(self):
        raw = _raw_client('bob')
        key = lifecycle_service.service_key_for_client(3, raw)
        self.assertEqual(key, 'eve:3:uuid-bob')
        self.assertNotIn('09121234567', key)
        self.assertNotIn('@', key)

    def test_missing_uuid_falls_back_to_the_email_only_as_last_resort(self):
        self.assertEqual(
            lifecycle_service.service_key_for_client(3, {}, email='BOB@x'),
            'eve:3:bob@x')

    def test_uuid_spellings_are_accepted_in_v3_then_legacy_order(self):
        self.assertEqual(lifecycle_service.resolve_client_uuid({'uuid': 'u1', 'id': 'u2'}), 'u1')
        self.assertEqual(lifecycle_service.resolve_client_uuid({'id': 'u2'}), 'u2')
        self.assertEqual(lifecycle_service.resolve_client_uuid({'subId': 's1'}), 's1')
        self.assertIsNone(lifecycle_service.resolve_client_uuid({}))

    def test_internal_state_ended_maps_to_the_external_volume_ended_kind(self):
        self.assertEqual(lifecycle_service.sms_notification_kind('ended'), 'volume_ended')
        self.assertEqual(lifecycle_service.sms_notification_kind('near_expiry'),
                         'near_expiry')
        self.assertEqual(lifecycle_service.sms_notification_kind('renew'), 'renew')

    def test_transactional_kinds_are_never_invalidation_eligible(self):
        for kind in ('created', 'renew'):
            self.assertFalse(lifecycle_service.is_depletion_kind(kind))
        for kind in ('near_expiry', 'low_volume', 'expired', 'volume_ended'):
            self.assertTrue(lifecycle_service.is_depletion_kind(kind))
        payload = lifecycle_service.build_invalidation_payload(
            service_key='eve:1:a', generation=18, reason='renewed',
            correlation_id='c1', event_id='e1')
        self.assertNotIn('renew', payload['invalidateKinds'])
        self.assertNotIn('created', payload['invalidateKinds'])


class LifecycleGenerationTests(_AppContextTestCase):
    "Durable generation: monotonic, idempotent, and committed with its outbox row."

    def setUp(self):
        _push_app_context(self)
        self.dispatch = mock.Mock()
        patcher = mock.patch.object(
            lifecycle_service, "dispatch_outbox_row_async", self.dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._cleanup_tables()

    def _cleanup_tables(self):
        # Drop the scoped session BEFORE deleting: another test file may have left
        # an ORM instance in the identity map for a primary key that sqlite is
        # about to reuse (the classic cross-file SAWarning this suite's conftest
        # documents). A fresh session makes that impossible.
        _drop_session()
        for model in (ServiceNotificationOutbox, ServiceLifecycleState,
                      ServiceObservedState, ServiceNotificationEvent,
                      SmsSendLog, WhatsappBotLog):
            try:
                model.query.delete()
            except Exception:
                db.session.rollback()
        db.session.commit()
        _drop_session()

    def _change(self, **overrides):
        kwargs = dict(server_id=1, client_uuid="uuid-bob",
                      client_email="bob", event_type="renewal")
        kwargs.update(overrides)
        return lifecycle_service.handle_successful_service_lifecycle_change(**kwargs)

    # 14 -- two successive renewals monotonically advance the generation.
    def test_two_successive_renewals_advance_the_generation_monotonically(self):
        first = self._change(operation_id="op-1")
        second = self._change(operation_id="op-2")
        self.assertEqual(first["generation"], 1)
        self.assertEqual(second["generation"], 2)
        state = ServiceLifecycleState.query.filter_by(
            service_key="eve:1:uuid-bob").one()
        self.assertEqual(state.generation, 2)
        self.assertIsNotNone(state.last_renewed_at)
        self.assertIsNotNone(state.last_lifecycle_change_at)

    # 13 -- repeated same renewal / invalidation is idempotent.
    def test_replayed_operation_does_not_advance_the_generation_twice(self):
        first = self._change(operation_id="op-1")
        replay = self._change(operation_id="op-1")
        self.assertFalse(replay["advanced"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["generation"], first["generation"])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(ServiceNotificationOutbox.query.count(), 1)

    def test_a_different_operation_still_advances(self):
        self._change(operation_id="op-1")
        second = self._change(operation_id="op-2")
        self.assertEqual(second["generation"], 2)
        self.assertEqual(ServiceNotificationOutbox.query.count(), 2)

    # 2 -- depletion already submitted, renewal succeeds, invalidate is asked for.
    def test_a_successful_change_queues_exactly_one_invalidation(self):
        result = self._change(operation_id="op-1")
        rows = ServiceNotificationOutbox.query.all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.service_key, "eve:1:uuid-bob")
        self.assertEqual(row.generation, result["generation"])
        self.assertEqual(row.status, "pending")
        self.assertEqual(sorted(row.kinds()),
                         ["expired", "low_volume", "near_expiry", "volume_ended"])
        self.dispatch.assert_called_once_with(result["outbox_id"])

    def test_the_immediate_dispatch_is_attempted_after_commit(self):
        with mock.patch.object(lifecycle_service, "attempt_outbox_event") as attempt:
            result = self._change(operation_id="op-9", dispatch=True)
        self.assertIsNotNone(result["outbox_id"])

    # 17 -- #nosms/#nopm and reseller behaviour are untouched by lifecycle work.
    def test_lifecycle_bookkeeping_does_not_touch_optout_tags(self):
        raw = _raw_client("bob", comment="09121234567 #nosms #nopm")
        self._change(client_uuid=raw["id"])
        self.assertIn("#nosms", raw["comment"])
        self.assertIn("#nopm", raw["comment"])

    def test_generation_is_durable_not_process_local(self):
        self._change(operation_id="op-1")
        # A second reader (another worker) sees it through the DB alone.
        db.session.expire_all()
        state = lifecycle_service.generation_state("eve:1:uuid-bob")
        self.assertEqual(state["generation"], 1)
        self.assertIsNotNone(state["last_lifecycle_change_at"])

    def test_generations_for_keys_is_a_single_batched_read(self):
        self._change(operation_id="op-1")
        self._change(client_uuid="uuid-carol", operation_id="op-2")
        with mock.patch.object(ServiceLifecycleState, "query") as query:
            query.filter.return_value.all.return_value = []
            lifecycle_service.generations_for_keys([
                "eve:1:uuid-bob", "eve:1:uuid-carol"])
            self.assertEqual(query.filter.call_count, 1)
        found = lifecycle_service.generations_for_keys([
            "eve:1:uuid-bob", "eve:1:uuid-carol", "eve:1:none"])
        self.assertEqual(sorted(found), [
            "eve:1:uuid-bob", "eve:1:uuid-carol"])

    # 8 -- same phone, two services: renewing A must not touch B.
    def test_invalidating_one_service_key_leaves_the_other_service_alone(self):
        self._change(client_uuid="uuid-A", operation_id="op-a")
        self._change(client_uuid="uuid-B", operation_id="op-b")
        state_a = ServiceLifecycleState.query.filter_by(
            service_key="eve:1:uuid-A").one()
        state_b = ServiceLifecycleState.query.filter_by(
            service_key="eve:1:uuid-B").one()
        self.assertEqual(state_a.generation, 1)
        self.assertEqual(state_b.generation, 1)
        outbox_a = ServiceNotificationOutbox.query.filter_by(
            service_key="eve:1:uuid-A").all()
        outbox_b = ServiceNotificationOutbox.query.filter_by(
            service_key="eve:1:uuid-B").all()
        # Both renewals produced their own invalidation, each scoped to its own key.
        self.assertEqual(len(outbox_a), 1)
        self.assertEqual(len(outbox_b), 1)
        self.assertNotEqual(outbox_a[0].event_id, outbox_b[0].event_id)
        self.assertNotEqual(outbox_a[0].service_key, outbox_b[0].service_key)


SMS_BASE_CFG = {
    "enabled": True,
    "provider": "gmweb",
    "base_url": "http://gateway.local",
    "api_key": "k",
    "trigger_near_expiry": True,
    "trigger_low_volume": True,
    "trigger_expired": True,
    "trigger_ended": True,
    "depletion_expiry_days": 3,
    "depletion_volume_gb": 2.0,
    "cooldown_hours": {"near_expiry": 24, "low_volume": 24,
                       "expired": 48, "ended": 24},
    "expired_max_age_days": 30,
    "ended_max_age_days": 0,
    "min_interval_seconds": 0,
    "daily_limit": 200,
    "hourly_limit": 0,
    "send_pace_seconds": 0,
    "quiet_enabled": False,
    "skip_unlimited": False,
}


class ScannerStaleSnapshotGuardTests(_AppContextTestCase):
    "RACE B: a snapshot older than the renewal can never prove depletion."

    def setUp(self):
        _push_app_context(self)
        self.server_id = 11
        self._orig = {
            key: GLOBAL_SERVER_DATA.get(key)
            for key in ("inbounds", "stats", "servers_status", "last_update")
        }
        self.addCleanup(lambda: GLOBAL_SERVER_DATA.update(self._orig))
        # Each test owns its generation rows: a leftover row from another test
        # would make the durable barrier fire for the wrong reason. The transition
        # pipeline's ledger/outbox rows are equally durable, and a leftover row turns
        # a first observation into a no-op duplicate.
        for model in (ServiceNotificationOutbox, ServiceLifecycleState,
                      ServiceObservedState, ServiceNotificationEvent):
            try:
                model.query.delete()
            except Exception:
                db.session.rollback()
        db.session.commit()
        self._patches = [
            mock.patch.object(messaging, "load_snapshot_from_redis",
                              lambda *a, **k: False),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _service_key(self, email="bob"):
        return lifecycle_service.make_service_key(self.server_id, "uuid-" + email)

    def _observe(self, state, now, *, remaining_gb=0.0, expiry_in_days=1,
                 email="bob", observed_at=None, omits_time=False):
        total = int(10 * GB)
        used = int(total - remaining_gb * GB)
        expiry = int((now + timedelta(days=expiry_in_days)).timestamp() * 1000)
        raw = _raw_client(email, expiry=expiry, total=total)
        row = _cache_row(self.server_id, raw, up=used, down=0,
                         observed_at=None if omits_time else (observed_at or now))
        _seed_snapshot(self.server_id, [row])
        return state

    # 5 -- scanner uses a snapshot older than lastRenewedAt: nothing is sent.
    def test_snapshot_observed_before_the_renewal_is_refused(self):
        now = datetime.utcnow()
        self._observe("ended", now)
        ok, reason = messaging._sms_depletion_state_still_valid(
            self.server_id, "bob", "ended", SMS_BASE_CFG,
            service_key=self._service_key(),
            expected_generation=0,
            last_lifecycle_change_at=now + timedelta(seconds=30),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "snapshot_predates_lifecycle_change")

    def test_an_unstamped_snapshot_row_fails_closed(self):
        now = datetime.utcnow()
        self._observe("ended", now, omits_time=True)
        ok, reason = messaging._sms_depletion_state_still_valid(
            self.server_id, "bob", "ended", SMS_BASE_CFG,
            service_key=self._service_key(),
            expected_generation=0,
            last_lifecycle_change_at=now - timedelta(seconds=30),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "snapshot_time_unknown_recheck")

    # 7 -- a genuinely expired service still notifies.
    def test_fresh_snapshot_confirming_a_real_expiry_is_allowed(self):
        now = datetime.utcnow()
        self._observe("expired", now, remaining_gb=5.0,
                      expiry_in_days=-1)
        ok, reason = messaging._sms_depletion_state_still_valid(
            self.server_id, "bob", "expired", SMS_BASE_CFG,
            service_key=self._service_key(),
            expected_generation=0,
            last_lifecycle_change_at=now - timedelta(seconds=30),
        )
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    # 6 -- a fresh snapshot that shows a healthy service suppresses the reminder.
    def test_fresh_snapshot_showing_a_healthy_service_suppresses(self):
        now = datetime.utcnow()
        self._observe("healthy", now, remaining_gb=40.0,
                      expiry_in_days=90)
        ok, reason = messaging._sms_depletion_state_still_valid(
            self.server_id, "bob", "ended", SMS_BASE_CFG,
            service_key=self._service_key(),
            expected_generation=0,
            last_lifecycle_change_at=now - timedelta(seconds=30),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "state_changed_recheck")

    # 1 / 15 -- a candidate classified before the renewal is dropped, not sent.
    def test_generation_change_drops_the_candidate(self):
        now = datetime.utcnow()
        self._observe("ended", now, observed_at=now + timedelta(seconds=60))
        result = lifecycle_service.handle_successful_service_lifecycle_change(
            server_id=self.server_id, client_uuid="uuid-bob",
            client_email="bob", operation_id="op-1",
            dispatch=False, commit=True)
        self.assertEqual(result["generation"], 1)
        ok, reason = messaging._sms_depletion_state_still_valid(
            self.server_id, "bob", "ended", SMS_BASE_CFG,
            service_key=self._service_key(),
            expected_generation=0,  # classified at generation 0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "generation_changed_recheck")

    # 12 -- the scanner races exactly with a renewal transaction.
    def test_candidate_with_no_captured_generation_is_dropped_after_a_renewal(self):
        now = datetime.utcnow()
        self._observe("ended", now, observed_at=now + timedelta(seconds=60))
        lifecycle_service.handle_successful_service_lifecycle_change(
            server_id=self.server_id, client_uuid="uuid-bob",
            client_email="bob", operation_id="op-1",
            dispatch=False, commit=True)
        ok, reason = messaging._sms_depletion_state_still_valid(
            self.server_id, "bob", "ended", SMS_BASE_CFG,
            service_key=self._service_key(),
            expected_generation=None,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "generation_unknown_recheck")

    def test_multi_worker_barrier_is_the_database_not_a_cache(self):
        """Worker B expired its own session; the durable row still wins."""
        now = datetime.utcnow()
        self._observe("ended", now, observed_at=now + timedelta(seconds=60))
        lifecycle_service.handle_successful_service_lifecycle_change(
            server_id=self.server_id, client_uuid="uuid-bob",
            client_email="bob", operation_id="op-1",
            dispatch=False, commit=True)
        db.session.expire_all()
        ok, reason = messaging._sms_depletion_state_still_valid(
            self.server_id, "bob", "ended", SMS_BASE_CFG,
            service_key=self._service_key(), expected_generation=0)
        self.assertFalse(ok)
        self.assertEqual(reason, "generation_changed_recheck")


MONITOR_TEMPLATES = {
    "soon": "soon {user} {time} {date} {server}",
    "low": "low {user} {rem} {time} {date} {server}",
    "expired": "expired {user} {rem} {time} {date} {server}",
    "ended": "ended {user} {rem} {time} {date} {server}",
}


class DepletionScanEndToEndTests(_AppContextTestCase):
    "The whole scan: generation captured, guard run, meta + stable key sent."

    server_id = 21

    def setUp(self):
        _push_app_context(self)
        self._orig = {
            key: GLOBAL_SERVER_DATA.get(key)
            for key in ("inbounds", "stats", "servers_status", "last_update")
        }
        self.addCleanup(lambda: GLOBAL_SERVER_DATA.update(self._orig))
        for model in (ServiceNotificationOutbox, ServiceLifecycleState,
                      ServiceObservedState, ServiceNotificationEvent,
                      SmsSendLog, WhatsappBotLog):
            try:
                model.query.delete()
            except Exception:
                db.session.rollback()
        db.session.commit()
        self.sent = []
        self._patches = [
            mock.patch.object(messaging, "load_snapshot_from_redis",
                              lambda *a, **k: False),
            mock.patch.object(messaging, "_get_sms_runtime_settings",
                              return_value=dict(SMS_BASE_CFG)),
            mock.patch.object(messaging, "_sms_gateway_ready",
                              return_value=(True, None, 200)),
            # The scan resolves this through `from app import ...`, so the patch
            # must target the app namespace or the fake would never be used.
            mock.patch.object(app_module, "_send_sms_via_gmweb",
                              side_effect=self._fake_send),
            mock.patch.object(app_module, "_get_monitor_settings",
                              return_value={"filters": {},
                                            "templates": MONITOR_TEMPLATES}),
            mock.patch.object(app_module, "fetch_and_update_global_data",
                              side_effect=self._targeted_refresh),
            # These tests pin the LEGACY sender path (the RACE A / RACE B guards on
            # the periodic scan), which remains a supported mode. The same races on the
            # transition pipeline are covered in test_telemetry_state_transitions.py.
            mock.patch.dict(os.environ,
                            {"EVE_DEPLETION_EVENT_PIPELINE": "off"}),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        messaging.SMS_LAST_SEND_TS[0] = 0
        messaging.SMS_SCAN_JOB.update({"state": "idle"})

    def _fake_send(self, to, text, cfg=None, priority=None, idempotency_key=None,
                   meta=None):
        self.sent.append({'to': to, 'priority': priority,
                          'idempotency_key': idempotency_key, 'meta': meta,
                          'text': text})
        return {'sent': True, 'status_code': 202, 'request_id': 'req-%d' % len(self.sent),
                'job_id': 'job-1', 'status': 'queued', 'priority': priority,
                'priority_level': 6, 'provider': 'gmweb', 'terminal': False,
                'successful': None, 'sms_segments': 1}

    def _targeted_refresh(self, *_args, **_kwargs):
        return False  # a targeted read is unavailable in the unit test

    def _depleted_row(self, now, email="bob", observed_at=None):
        total = int(10 * GB)
        expiry = int((now + timedelta(days=2)).timestamp() * 1000)
        raw = _raw_client(email, expiry=expiry, total=total)
        row = _cache_row(self.server_id, raw, up=total, down=0,
                         observed_at=observed_at or now)
        _seed_snapshot(self.server_id, [row])
        return row

    # 1 -- candidate classified, renewal happens before enqueue, candidate dropped.
    def test_scan_drops_a_candidate_whose_snapshot_predates_the_renewal(self):
        now = datetime.utcnow()
        self._depleted_row(now, observed_at=now)
        lifecycle_service.handle_successful_service_lifecycle_change(
            server_id=self.server_id, client_uuid="uuid-bob",
            client_email="bob", operation_id="op-before-scan",
            dispatch=False, commit=True)
        result = messaging._run_sms_depletion_scan(triggered_by="manual")
        self.assertEqual(self.sent, [])
        self.assertEqual(result.get("sent"), 0)
        rows = SmsSendLog.query.filter_by(email="bob").all()
        reasons = {row.reason for row in rows}
        self.assertIn("snapshot_predates_lifecycle_change", reasons)

    def test_scan_sends_when_the_snapshot_postdates_the_lifecycle_change(self):
        now = datetime.utcnow()
        lifecycle_service.handle_successful_service_lifecycle_change(
            server_id=self.server_id, client_uuid="uuid-bob",
            client_email="bob", operation_id="op-old",
            dispatch=False, commit=True)
        db.session.expire_all()
        state = ServiceLifecycleState.query.filter_by(
            service_key="eve:%d:uuid-bob" % self.server_id).one()
        fresh_observation = state.last_lifecycle_change_at + timedelta(seconds=60)
        self._depleted_row(now, observed_at=fresh_observation)
        result = messaging._run_sms_depletion_scan(triggered_by="manual")
        self.assertEqual(result.get("sent"), 1, result)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["meta"]["notificationKind"],
                         "volume_ended")
        self.assertTrue(self.sent[0]["meta"]["requiresValidation"])
        self.assertEqual(self.sent[0]["meta"]["serviceKey"],
                         "eve:%d:uuid-bob" % self.server_id)
        self.assertEqual(self.sent[0]["meta"]["generation"], 1)
        logged = SmsSendLog.query.filter_by(email="bob").all()
        self.assertTrue(any(row.service_key for row in logged))
        self.assertTrue(any(row.lifecycle_generation == 1 for row in logged))

    # 11 -- two scanner workers cannot both create a duplicate send.
    def test_two_workers_produce_the_same_deterministic_idempotency_key(self):
        key_one = lifecycle_service.sms_notification_kind("ended")
        first = messaging._sms_idempotency_key("eve:1:uuid-bob", 1, key_one, 24)
        second = messaging._sms_idempotency_key("eve:1:uuid-bob", 1, key_one, 24)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("eve:eve:1:uuid-bob:1:volume_ended:"), first)
        later = messaging._stable_sms_idempotency_key(
            "eve:1:uuid-bob", 1, key_one, 3600)
        self.assertNotEqual(first, later)

    def test_idempotency_key_differs_per_generation_and_per_kind(self):
        base = messaging._stable_sms_idempotency_key(
            "eve:1:a", 1, "volume_ended", 86400)
        next_gen = messaging._stable_sms_idempotency_key(
            "eve:1:a", 2, "volume_ended", 86400)
        other_kind = messaging._stable_sms_idempotency_key(
            "eve:1:a", 1, "expired", 86400)
        self.assertNotEqual(base, next_gen)
        self.assertNotEqual(base, other_kind)
        self.assertLessEqual(len(base), 200)

    # 18 -- existing rate limits and cooldowns remain correct.
    def test_transactional_renew_confirmations_stay_hourly_exempt(self):
        cfg = dict(SMS_BASE_CFG)
        cfg["hourly_limit"] = 1
        cfg["min_interval_seconds"] = 0
        messaging.SMS_SEND_TRACKER["per_recipient"] = {}
        ok, reason = messaging._sms_take_send_slot(
            "09120000001", cfg, 1, priority="renew")
        self.assertTrue(ok, reason)

    def test_per_recipient_cooldown_still_blocks_the_scan(self):
        now = datetime.utcnow()
        self._depleted_row(now, observed_at=now)
        db.session.add(WhatsappBotLog(email="bob",
                                     server_id=self.server_id,
                                     event="sms_ended"))
        db.session.commit()
        result = messaging._run_sms_depletion_scan(triggered_by="manual")
        self.assertEqual(self.sent, [])
        self.assertEqual(result.get("sent"), 0)

    # 17 -- #nosms/#nopm still suppress the scan entirely.
    def test_optout_tag_suppresses_the_scan(self):
        now = datetime.utcnow()
        total = int(10 * GB)
        expiry = int((now + timedelta(days=2)).timestamp() * 1000)
        raw = _raw_client("bob", expiry=expiry, total=total,
                          comment="09121234567 #nosms")
        row = _cache_row(self.server_id, raw, up=total, down=0, observed_at=now)
        _seed_snapshot(self.server_id, [row])
        messaging._run_sms_depletion_scan(triggered_by="manual")
        self.assertEqual(self.sent, [])

    # 16 -- reseller-owned accounts are still skipped, unchanged.
    def test_reseller_owned_accounts_are_still_skipped(self):
        now = datetime.utcnow()
        self._depleted_row(now, observed_at=now)
        with mock.patch.object(messaging, "_account_has_reseller_owner",
                               return_value=True):
            messaging._run_sms_depletion_scan(triggered_by="manual")
        self.assertEqual(self.sent, [])

class OutboxRetryTests(_AppContextTestCase):
    "A gateway outage delays the invalidation; it never loses it."

    def setUp(self):
        _push_app_context(self)
        self._cleanup()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        _drop_session()
        for model in (ServiceNotificationOutbox, ServiceLifecycleState,
                      ServiceObservedState, ServiceNotificationEvent,
                      SmsSendLog):
            try:
                model.query.delete()
            except Exception:
                db.session.rollback()
        db.session.commit()
        _drop_session()

    def _enqueue(self, **overrides):
        kwargs = dict(server_id=1, client_uuid="uuid-bob",
                      client_email="bob", operation_id="op-1",
                      dispatch=False, commit=True)
        kwargs.update(overrides)
        return lifecycle_service.handle_successful_service_lifecycle_change(**kwargs)

    # 3 -- GMweb invalidation fails, the renewal still succeeded, outbox retries.
    def test_a_gateway_failure_keeps_the_event_pending_with_a_backoff(self):
        result = self._enqueue(operation_id="op-1")
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        self.assertEqual(row.status, "pending")
        with mock.patch.object(messaging, "_invalidate_notifications_via_gmweb",
                               return_value={'ok': False, 'status_code': 503,
                                             'reason': 'gateway_unavailable'}), \
             mock.patch.object(app_module, "_get_sms_runtime_settings",
                               return_value=dict(SMS_BASE_CFG)):
            outcome = lifecycle_service.attempt_outbox_event(row.id)
        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome["terminal"])
        db.session.expire_all()
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.attempt_count, 1)
        self.assertEqual(row.last_status_code, 503)
        self.assertIn("gateway_unavailable", row.last_error)
        self.assertIsNotNone(row.next_attempt_at)
        self.assertGreater(row.next_attempt_at, datetime.utcnow())

    def test_backoff_is_bounded_and_monotonic(self):
        delays = [lifecycle_service._backoff_delay(attempt)
                  for attempt in range(0, 12)]
        # First failure (attempt number 0 after the 1-based count is applied)
        # starts at the short lane, then the ladder grows and finally plateaus.
        self.assertEqual(delays[0], 5)
        self.assertEqual(delays[2], 30)
        self.assertEqual(delays[:5], [5, 5, 30, 120, 600])
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[-1], delays[-2])
        self.assertLessEqual(max(delays), 10800)

    def test_429_and_timeouts_are_retriable_not_swallowed(self):
        result = self._enqueue(operation_id="op-1")
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        for response in (
            {'ok': False, 'status_code': 429, 'reason': 'http_429'},
            {'ok': False, 'status_code': None, 'reason': 'gateway_error: timeout'},
            {'ok': False, 'status_code': 500, 'reason': 'http_500'},
        ):
            with mock.patch.object(messaging, "_invalidate_notifications_via_gmweb",
                                   return_value=response), \
                 mock.patch.object(app_module, "_get_sms_runtime_settings",
                                   return_value=dict(SMS_BASE_CFG)):
                outcome = lifecycle_service.attempt_outbox_event(row.id)
            self.assertFalse(outcome["ok"])
            db.session.expire_all()
            row = db.session.get(ServiceNotificationOutbox,
                                 result["outbox_id"])
            self.assertEqual(row.status, "pending")
            self.assertIsNotNone(row.last_error)
        self.assertEqual(row.attempt_count, 3)

    def test_a_malformed_gateway_answer_is_treated_as_a_failure(self):
        result = self._enqueue(operation_id="op-1")
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        with mock.patch.object(messaging, "_invalidate_notifications_via_gmweb",
                               return_value={'ok': True, 'status_code': 200,
                                             'body': {'ok': False}}), \
             mock.patch.object(app_module, "_get_sms_runtime_settings",
                               return_value=dict(SMS_BASE_CFG)):
            outcome = lifecycle_service.attempt_outbox_event(row.id)
        self.assertFalse(outcome["ok"])
        db.session.expire_all()
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        self.assertEqual(row.status, "pending")

    def test_a_successful_answer_settles_the_event_with_its_counts(self):
        result = self._enqueue(operation_id="op-1")
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        body = {'ok': True, 'currentGeneration': 1, 'cancelledPending': 2,
                'revokedActive': 1, 'revokedInflight': 1, 'alreadyTerminal': 0}
        with mock.patch.object(messaging, "_invalidate_notifications_via_gmweb",
                               return_value={'ok': True, 'status_code': 200,
                                             'body': body}), \
             mock.patch.object(app_module, "_get_sms_runtime_settings",
                               return_value=dict(SMS_BASE_CFG)):
            outcome = lifecycle_service.attempt_outbox_event(row.id)
        self.assertTrue(outcome["ok"])
        db.session.expire_all()
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        self.assertEqual(row.status, "sent")
        self.assertEqual(row.cancelled_pending, 2)
        self.assertEqual(row.revoked_active, 1)
        self.assertEqual(row.revoked_inflight, 1)
        self.assertEqual(row.already_terminal, 0)

    def test_a_newer_lifecycle_gets_its_own_event(self):
        first = self._enqueue(operation_id="op-1")
        self._enqueue(operation_id="op-2")
        rows = ServiceNotificationOutbox.query.order_by(
            ServiceNotificationOutbox.id).all()
        self.assertEqual([row.generation for row in rows], [1, 2])
        self.assertNotEqual(rows[0].event_id, rows[1].event_id)
        self.assertEqual(first["generation"], 1)

    # 4 -- a process restart with a pending invalidation resumes the retry.
    def test_a_restart_resumes_the_pending_invalidation(self):
        result = self._enqueue(operation_id="op-1")
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        with mock.patch.object(messaging, "_invalidate_notifications_via_gmweb",
                               return_value={'ok': False, 'status_code': 503,
                                             'reason': 'gateway_unavailable'}), \
             mock.patch.object(app_module, "_get_sms_runtime_settings",
                               return_value=dict(SMS_BASE_CFG)):
            lifecycle_service.attempt_outbox_event(row.id)
        # Simulate the restart: no in-memory state survives, only the DB row.
        db.session.expire_all()
        db.session.remove()
        body = {'ok': True, 'currentGeneration': 1, 'cancelledPending': 1,
                'revokedActive': 0, 'revokedInflight': 0, 'alreadyTerminal': 0}
        with mock.patch.object(messaging, "_invalidate_notifications_via_gmweb",
                               return_value={'ok': True, 'status_code': 200,
                                             'body': body}) as invalidate, \
             mock.patch.object(app_module, "_get_sms_runtime_settings",
                               return_value=dict(SMS_BASE_CFG)):
            due = lifecycle_service.flush_invalidation_outbox(
                now=datetime.utcnow() + timedelta(hours=1))
        self.assertEqual(due["sent"], 1, due)
        payload = invalidate.call_args[0][0]
        self.assertEqual(payload["serviceKey"], "eve:1:uuid-bob")
        self.assertEqual(payload["currentGeneration"], 1)
        self.assertEqual(payload["eventId"], result["event_id"])
        self.assertEqual(lifecycle_service.pending_invalidation_count(), 0)

    def test_flush_skips_events_whose_backoff_has_not_elapsed(self):
        result = self._enqueue(operation_id="op-1")
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        with mock.patch.object(messaging, "_invalidate_notifications_via_gmweb",
                               return_value={'ok': False, 'status_code': 503,
                                             'reason': 'gateway_unavailable'}), \
             mock.patch.object(app_module, "_get_sms_runtime_settings",
                               return_value=dict(SMS_BASE_CFG)):
            lifecycle_service.attempt_outbox_event(row.id)
        with mock.patch.object(messaging, "_invalidate_notifications_via_gmweb") as again:
            out = lifecycle_service.flush_invalidation_outbox(now=datetime.utcnow())
        self.assertEqual(out["due"], 0)
        again.assert_not_called()

    def test_missing_gateway_configuration_is_not_silently_dropped(self):
        result = self._enqueue(operation_id="op-1")
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        empty = dict(SMS_BASE_CFG, base_url="", api_key="")
        with mock.patch.object(app_module, "_get_sms_runtime_settings",
                               return_value=empty):
            outcome = lifecycle_service.attempt_outbox_event(row.id)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "gateway_not_configured")
        db.session.expire_all()
        row = db.session.get(ServiceNotificationOutbox, result["outbox_id"])
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.last_error, "gateway_not_configured")

    def test_invalidation_status_reports_the_pending_work(self):
        self._enqueue(operation_id="op-1")
        status = lifecycle_service.invalidation_status(service_key="eve:1:uuid-bob")
        self.assertEqual(status["pending"], 1)
        self.assertEqual(len(status["events"]), 1)
        self.assertEqual(status["events"][0]["generation"], 1)

    # 10 -- create/renew confirmations are never invalidated.

    def test_invalidation_is_routed_to_the_gmweb_connection(self):
        "A custom_http relay has no notification ledger to revoke from."
        cfg = {
            "provider": "custom_http",
            "providers": {
                "gmweb": {"base_url": "https://gw.test",
                          "api_key": "k"},
                "custom_http": {"base_url": "https://relay.test",
                                "api_key": "k2"},
            },
        }
        self.assertEqual(
            messaging._invalidation_provider("custom_http", cfg), "gmweb")
        self.assertEqual(
            messaging._invalidation_provider("gmweb", cfg), "gmweb")
        # GMweb unconfigured: keep the caller provider so the outbox records a
        # configuration error instead of posting into the void.
        bare = {
            "provider": "custom_http",
            "providers": {"gmweb": {"base_url": "",
                          "api_key": ""}},
        }
        self.assertEqual(
            messaging._invalidation_provider("custom_http", bare), "custom_http")

    def test_transactional_confirmation_metadata_cannot_be_invalidated(self):
        meta = messaging._transactional_notification_meta(
            "eve:1:uuid-bob", "renew", 18)
        self.assertEqual(meta["notificationKind"], "renew")
        self.assertEqual(meta["generation"], 18)
        self.assertFalse(meta["requiresValidation"])
        self.assertNotIn(meta["notificationKind"],
                         lifecycle_service.DEPLETION_NOTIFICATION_KINDS)
        created = messaging._transactional_notification_meta(
            "eve:1:uuid-bob", "created", 0)
        self.assertEqual(created["notificationKind"], "created")
        self.assertFalse(created["requiresValidation"])

    # 9 -- the renewal confirmation itself is still sent.
    def test_renewal_confirmation_is_still_dispatched_with_its_meta(self):
        sent = []

        def _capture(to, text, cfg=None, priority=None, idempotency_key=None,
                     meta=None):
            sent.append({'meta': meta, 'priority': priority,
                         'idempotency_key': idempotency_key})
            return {'sent': True, 'status_code': 202, 'request_id': 'r1',
                    'provider': 'gmweb', 'priority': priority,
                    'priority_level': 1, 'sms_segments': 1}

        self._enqueue(operation_id="op-1")
        with mock.patch.object(messaging, "_get_sms_runtime_settings",
                               return_value=dict(SMS_BASE_CFG)), \
             mock.patch.object(app_module, "_send_sms_via_gmweb",
                               side_effect=_capture), \
             mock.patch.object(messaging, "_get_sms_template_content",
                               return_value="renewed {user}"), \
             mock.patch("threading.Thread") as thread:
            thread.side_effect = lambda target=None, **kwargs: mock.Mock(
                start=lambda: target())
            messaging._fire_automation_sms(
                "renew", 1, "bob", "renew",
                "renewed {user}", {'user': 'bob'},
                recipient_comment="09121234567", server_name="srv")
        self.assertEqual(len(sent), 1, sent)
        self.assertEqual(sent[0]["meta"]["notificationKind"],
                         "renew")
        self.assertFalse(sent[0]["meta"]["requiresValidation"])
        self.assertEqual(sent[0]["priority"], "critical")
        logged = SmsSendLog.query.filter_by(email="bob",
                                          state="renew").all()
        self.assertTrue(logged)
        self.assertTrue(logged[-1].service_key)


class GatewaySupersededReconciliationTests(_AppContextTestCase):
    "The gateway revokes a queued reminder as 'superseded', not by deleting it."

    def setUp(self):
        _push_app_context(self)
        _drop_session()
        SmsSendLog.query.delete()
        db.session.commit()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            SmsSendLog.query.delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
        _drop_session()

    def _row(self, status='queued', request_id='send_1', service_key='eve:1:uuid-bob'):
        row = SmsSendLog(
            email='bob', server_id=1, state='ended', recipient='0912***567',
            status=status, request_id=request_id, gateway_provider='gmweb',
            service_key=service_key, lifecycle_generation=17,
            terminal=False,
        )
        db.session.add(row)
        db.session.commit()
        return row

    # The exact body GMweb 0.19 returns for a reminder a renewal superseded.
    def test_a_superseded_reminder_becomes_terminal_and_non_billable(self):
        row = self._row()
        body = {
            'ok': True, 'requestId': 'send_1', 'status': 'superseded',
            'state': 'superseded', 'superseded': True, 'terminal': True,
            'successful': False, 'outcome': 'superseded',
            'revocationReason': 'renewed', 'revokedAt': '2026-09-13T21:00:00.000Z',
            'serviceKey': 'eve:1:uuid-bob', 'notificationKind': 'volume_ended',
            'generation': 17, 'requiresValidation': True,
        }
        resp = mock.Mock(status_code=200, content=b'{}')
        resp.json.return_value = body
        with mock.patch.object(messaging.requests, 'get', return_value=resp), \
             mock.patch.object(messaging, '_get_sms_runtime_settings',
                               return_value=dict(SMS_BASE_CFG)), \
             mock.patch.object(messaging, '_sms_status_endpoint',
                               return_value='http://gw.local/send/status/send_1'):
            changed = messaging._refresh_pending_sms_statuses()
        self.assertGreaterEqual(changed, 1)
        db.session.expire_all()
        row = SmsSendLog.query.filter_by(request_id='send_1').one()
        self.assertEqual(row.status, 'superseded')
        self.assertTrue(row.terminal)
        self.assertFalse(row.successful)
        self.assertEqual(row.gateway_outcome, 'superseded')
        self.assertEqual(row.revocation_reason, 'renewed')
        self.assertEqual(row.invalidation_reason, 'renewed')
        self.assertIsNotNone(row.invalidated_at)
        self.assertEqual(row.revoked_at, '2026-09-13T21:00:00.000Z')

    def test_a_superseded_row_is_never_offered_for_cancellation_again(self):
        self._row(status='superseded', request_id='send_2')
        with mock.patch.object(messaging, '_cancel_sms_via_gmweb') as cancel:
            result = messaging._cancel_pending_sms_for_account(
                1, 'bob', reason='renew_success')
        cancel.assert_not_called()
        self.assertEqual(result['gateway_cancelled'], 0)

    def test_a_delivered_send_is_still_counted_as_success(self):
        self._row()
        body = {'ok': True, 'requestId': 'send_1', 'status': 'sent',
                'state': 'completed', 'terminal': True, 'successful': True,
                'outcome': 'sent', 'sentAt': '2026-09-13T20:00:00.000Z'}
        resp = mock.Mock(status_code=200, content=b'{}')
        resp.json.return_value = body
        with mock.patch.object(messaging.requests, 'get', return_value=resp), \
             mock.patch.object(messaging, '_get_sms_runtime_settings',
                               return_value=dict(SMS_BASE_CFG)), \
             mock.patch.object(messaging, '_sms_status_endpoint',
                               return_value='http://gw.local/send/status/send_1'):
            messaging._refresh_pending_sms_statuses()
        db.session.expire_all()
        row = SmsSendLog.query.filter_by(request_id='send_1').one()
        self.assertEqual(row.status, 'sent')
        self.assertTrue(row.successful)
        self.assertIsNone(row.invalidated_at)


if __name__ == "__main__":
    unittest.main()

