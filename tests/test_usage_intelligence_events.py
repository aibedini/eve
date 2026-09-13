"""Business events (RenewalEvent v2): the writer, and the renew route that feeds it.

RFP sections 4-9, 45.6-45.8, 47, 48, 49: an explicit verified renewal is the only thing
that may open a recommendation cycle; a counter decrease is telemetry; a retry or a
failed read-back must not produce a second or a false boundary.
"""
import copy
import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

import unittest  # noqa: E402

import app as app_module  # noqa: E402
from app import Admin, ClientOperation, GLOBAL_SERVER_DATA, Server, app, db  # noqa: E402
from panel.models import RenewalEvent  # noqa: E402
from panel.routes import clients as clients_module  # noqa: E402
from panel.services.usage_intelligence import events as event_service  # noqa: E402

GB = 1024 ** 3
DAY_MS = 24 * 60 * 60 * 1000


class UsageIntelligenceTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        RenewalEvent.query.delete()
        db.session.commit()

    def tearDown(self):
        db.session.rollback()


class RenewalEventWriterTests(UsageIntelligenceTestCase):
    def test_a_verified_renewal_is_a_cycle_boundary_with_separated_rollover(self):
        event = event_service.record_verified_renewal(
            server_id=1, sub_id='acct-1', operation_id='op-1', email='a@b.test',
            days=31,
            previous_volume_limit_bytes=50 * GB,
            new_volume_limit_bytes=100 * GB,
            previous_remaining_bytes=10 * GB,
            granted_volume_bytes=50 * GB,
            previous_expiry_ms=1_700_000_000_000,
            new_expiry_ms=1_702_678_400_000,
        )
        self.assertIsNotNone(event)
        self.assertTrue(event.verified)
        self.assertIsNotNone(event.verified_at)
        self.assertEqual(event.event_type, 'renewal')
        self.assertEqual(event.source, 'explicit_renew')
        self.assertTrue(event.is_cycle_boundary)
        # A 50GB purchase on top of a 10GB leftover is not a 60GB purchase.
        self.assertEqual(event.granted_volume_bytes, 50 * GB)
        self.assertEqual(event.carried_over_bytes, 10 * GB)
        self.assertEqual(event.previous_remaining_bytes, 10 * GB)
        self.assertEqual(event.new_volume_limit_bytes, 100 * GB)
        self.assertFalse(event.is_unlimited_volume)
        self.assertFalse(event.is_unlimited_time)
        self.assertEqual(event.days, 31)

    def test_a_traffic_reset_carries_nothing_over(self):
        event = event_service.record_verified_renewal(
            server_id=1, sub_id='acct-reset', operation_id='op-reset',
            previous_volume_limit_bytes=50 * GB, new_volume_limit_bytes=50 * GB,
            previous_remaining_bytes=10 * GB, granted_volume_bytes=50 * GB,
            previous_expiry_ms=1_700_000_000_000, new_expiry_ms=1_702_678_400_000,
            traffic_reset=True,
        )
        self.assertTrue(event.traffic_reset)
        self.assertEqual(event.carried_over_bytes, 0)
        self.assertEqual(event.granted_volume_bytes, 50 * GB)

    def test_unlimited_volume_and_time_are_flagged_not_guessed(self):
        event = event_service.record_verified_renewal(
            server_id=1, sub_id='acct-unlimited', operation_id='op-unlimited',
            previous_volume_limit_bytes=50 * GB, new_volume_limit_bytes=0,
            previous_remaining_bytes=0, granted_volume_bytes=0,
            previous_expiry_ms=1_700_000_000_000, new_expiry_ms=0,
        )
        self.assertTrue(event.is_unlimited_volume)
        self.assertTrue(event.is_unlimited_time)
        self.assertIsNone(event.new_expiry_at)

    def test_a_counter_decrease_is_telemetry_and_never_a_boundary(self):
        event = event_service.record_inferred_reset(
            server_id=1, sub_id='acct-2', volume_bytes=50 * GB)
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, 'inferred_reset')
        self.assertEqual(event.source, 'counter_reset')
        self.assertFalse(event.verified)
        self.assertIsNone(event.verified_at)
        self.assertFalse(event.is_cycle_boundary)

    def test_an_inferred_reset_is_skipped_next_to_an_explicit_renewal(self):
        renewal = event_service.record_verified_renewal(
            server_id=1, sub_id='acct-3', operation_id='op-3',
            previous_volume_limit_bytes=10 * GB, new_volume_limit_bytes=20 * GB,
            previous_remaining_bytes=0, granted_volume_bytes=10 * GB,
            previous_expiry_ms=1, new_expiry_ms=0)
        self.assertIsNotNone(renewal)
        # The collector sees the counter decrease seconds later: not a second cycle.
        self.assertIsNone(event_service.record_inferred_reset(
            server_id=1, sub_id='acct-3', volume_bytes=20 * GB))
        self.assertEqual(RenewalEvent.query.filter_by(sub_id='acct-3').count(), 1)

        # Long after the renewal the signal is recorded as its own (unverified) event.
        later = datetime.utcnow() + timedelta(minutes=30)
        self.assertIsNotNone(event_service.record_inferred_reset(
            server_id=1, sub_id='acct-3', volume_bytes=20 * GB, renewed_at=later))
        self.assertEqual(RenewalEvent.query.filter_by(sub_id='acct-3').count(), 2)

    def test_the_dedup_window_is_configurable(self):
        event_service.record_verified_renewal(
            server_id=1, sub_id='acct-4', operation_id='op-4',
            previous_volume_limit_bytes=10 * GB, new_volume_limit_bytes=10 * GB,
            previous_remaining_bytes=0, granted_volume_bytes=0,
            previous_expiry_ms=1, new_expiry_ms=0)
        inside = datetime.utcnow() + timedelta(minutes=4)
        outside = datetime.utcnow() + timedelta(minutes=6)
        self.assertIsNone(event_service.record_inferred_reset(
            server_id=1, sub_id='acct-4', volume_bytes=1, dedup_window_minutes=5,
            renewed_at=inside))
        self.assertIsNotNone(event_service.record_inferred_reset(
            server_id=1, sub_id='acct-4', volume_bytes=1, dedup_window_minutes=5,
            renewed_at=outside))

    def test_the_same_operation_and_type_is_written_once(self):
        first = event_service.record_verified_renewal(
            server_id=1, sub_id='acct-5', operation_id='op-5',
            previous_volume_limit_bytes=1, new_volume_limit_bytes=2,
            previous_remaining_bytes=0, granted_volume_bytes=1,
            previous_expiry_ms=1, new_expiry_ms=0)
        second = event_service.record_verified_renewal(
            server_id=1, sub_id='acct-5', operation_id='op-5',
            previous_volume_limit_bytes=1, new_volume_limit_bytes=2,
            previous_remaining_bytes=0, granted_volume_bytes=1,
            previous_expiry_ms=1, new_expiry_ms=0)
        self.assertEqual(first.id, second.id)
        self.assertEqual(RenewalEvent.query.filter_by(operation_id='op-5').count(), 1)
        # A different event type on the same operation is a different fact.
        other = event_service.record_renewal_event(
            server_id=1, sub_id='acct-5', operation_id='op-5',
            event_type='traffic_reset', source='admin_reset', verified=True)
        self.assertIsNotNone(other)
        self.assertEqual(RenewalEvent.query.filter_by(operation_id='op-5').count(), 2)

    def test_the_low_level_writer_defaults_to_a_non_boundary(self):
        event = event_service.record_renewal_event(server_id=1, sub_id='acct-6')
        self.assertEqual(event.event_type, 'inferred_reset')
        self.assertEqual(event.source, 'inferred')
        self.assertFalse(event.verified)
        self.assertFalse(event.is_cycle_boundary)

    def test_the_latest_boundary_ignores_unverified_events(self):
        event_service.record_inferred_reset(server_id=7, sub_id='acct-7',
                                           renewed_at=datetime.utcnow())
        self.assertIsNone(event_service.latest_cycle_boundary(7, 'acct-7'))

        old = event_service.record_verified_renewal(
            server_id=7, sub_id='acct-7', operation_id='op-old',
            previous_volume_limit_bytes=1, new_volume_limit_bytes=9,
            previous_remaining_bytes=0, granted_volume_bytes=8,
            previous_expiry_ms=1, new_expiry_ms=0,
            renewed_at=datetime.utcnow() - timedelta(days=20))
        recent = event_service.record_verified_renewal(
            server_id=7, sub_id='acct-7', operation_id='op-new',
            previous_volume_limit_bytes=9, new_volume_limit_bytes=20,
            previous_remaining_bytes=0, granted_volume_bytes=11,
            previous_expiry_ms=1, new_expiry_ms=0,
            renewed_at=datetime.utcnow() - timedelta(days=2))
        boundary = event_service.latest_cycle_boundary(7, 'acct-7')
        self.assertEqual(boundary.id, recent.id)
        self.assertNotEqual(boundary.id, old.id)

    def test_a_package_change_is_a_boundary_and_a_topup_is_not(self):
        change = event_service.record_renewal_event(
            server_id=8, sub_id='acct-8', operation_id='op-change',
            event_type='package_change', source='explicit_renew', verified=True)
        topup = event_service.record_renewal_event(
            server_id=8, sub_id='acct-8', operation_id='op-topup',
            event_type='quota_topup', source='explicit_renew', verified=True)
        self.assertTrue(change.is_cycle_boundary)
        self.assertFalse(topup.is_cycle_boundary)
        self.assertEqual(event_service.latest_cycle_boundary(8, 'acct-8').id, change.id)

    def test_the_writer_leaves_the_transaction_to_its_caller(self):
        event_service.record_verified_renewal(
            server_id=9, sub_id='acct-9', operation_id='op-9',
            previous_volume_limit_bytes=1, new_volume_limit_bytes=1,
            previous_remaining_bytes=0, granted_volume_bytes=0,
            previous_expiry_ms=1, new_expiry_ms=0)
        db.session.rollback()
        self.assertEqual(RenewalEvent.query.filter_by(sub_id='acct-9').count(), 0)


class RenewRouteRenewalEventTests(UsageIntelligenceTestCase):
    """RFP section 48: the renew endpoint must create exactly one verified event."""

    def setUp(self):
        super().setUp()
        ClientOperation.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()

        self.admin = Admin(username='renewal-events', password_hash='x',
                           role='superadmin', is_superadmin=True)
        self.server = Server(name='panel-renewal-events', host='https://panel.example:8443/base',
                             username='u', password='p', sub_path='/sub/', panel_type='auto')
        db.session.add_all([self.admin, self.server])
        db.session.commit()

        self.http = app.test_client()
        with self.http.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
            sess['admin_username'] = self.admin.username
            sess['role'] = self.admin.role
            sess['is_superadmin'] = True

        self._saved_snapshot = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore_snapshot)
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({'inbounds': [], 'stats': {}, 'servers_status': [],
                                   'last_update': None})

        self.panel_expiry_ms = int(datetime.utcnow().timestamp() * 1000) + 5 * DAY_MS
        self.panel_raw = {
            'email': 'bob', 'id': 'uuid-bob', 'enable': True,
            'totalGB': 50 * GB, 'expiryTime': self.panel_expiry_ms,
        }
        self.session_obj = mock.Mock()
        self.v3_update = mock.Mock(return_value=(True, {}, None))
        self.v3_enable = mock.Mock(return_value=(True, {}, None))
        self.rewrite_readback = False

        def fetch_inbounds(*_args, **_kwargs):
            sent = dict(self.v3_update.call_args[0][3]) if self.v3_update.call_args \
                else copy.deepcopy(self.panel_raw)
            if self.rewrite_readback:
                # The panel ignored the write: read-back does not match the intent.
                sent = dict(self.panel_raw)
            return ([{'id': 1, 'server_id': self.server.id,
                      'settings': json.dumps({'clients': [sent]})}], None, '3x-ui')

        self._patches = [
            mock.patch.object(app_module, 'get_xui_session',
                              return_value=(self.session_obj, None)),
            mock.patch.object(app_module, 'server_is_v3', return_value=True),
            mock.patch.object(app_module, 'v3_update_client', self.v3_update),
            mock.patch.object(app_module, 'v3_enable_client', self.v3_enable),
            mock.patch.object(app_module, 'fetch_inbounds', side_effect=fetch_inbounds),
            mock.patch.object(app_module, '_fire_automation_sms'),
            mock.patch.object(app_module, '_fire_cancel_stale_account_sms'),
            mock.patch.object(app_module, '_notify_customer_telegram'),
            mock.patch.object(clients_module, '_fire_renew_whatsapp'),
            mock.patch.object(clients_module, '_fire_renew_postcheck'),
            mock.patch('time.sleep'),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._stop_patches)

        self._seed_cache()

    def _stop_patches(self):
        for patch in self._patches:
            patch.stop()

    def _restore_snapshot(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved_snapshot)

    def _seed_cache(self):
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server.id, 'id': 1, 'remark': 'in',
            'clients': [{
                'server_id': self.server.id, 'inbound_id': 1, 'email': 'bob',
                'id': 'uuid-bob', 'up': 40 * GB, 'down': 0,
                'up_formatted': '40 GB', 'down_formatted': '0 B',
                'raw_client': copy.deepcopy(self.panel_raw),
            }],
            'client_count': 1, 'active_count': 1,
        }]
        GLOBAL_SERVER_DATA['servers_status'] = [
            {'server_id': self.server.id, 'success': True, 'reachable': True, 'stats': {}}]
        GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()

    def _renew(self, *, operation_id='renew-op-1', **payload):
        body = {'mode': 'custom', 'days': 30, 'volume': 10, 'free': True}
        body.update(payload)
        return self.http.post(
            '/api/client/%d/1/bob/renew' % self.server.id, json=body,
            headers={'Idempotency-Key': operation_id})

    def test_a_successful_renew_records_exactly_one_verified_event(self):
        response = self._renew()
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'], payload)
        self.assertTrue(payload['verify']['ok'], payload)

        events = RenewalEvent.query.filter_by(server_id=self.server.id).all()
        self.assertEqual(len(events), 1, [e.to_dict() for e in events])
        event = events[0]
        self.assertTrue(event.verified)
        self.assertTrue(event.is_cycle_boundary)
        self.assertEqual(event.event_type, 'renewal')
        self.assertEqual(event.source, 'explicit_renew')
        self.assertEqual(event.operation_id, 'renew-op-1')
        self.assertEqual(event.sub_id, 'uuid-bob')
        self.assertEqual(event.client_uuid, 'uuid-bob')
        self.assertEqual(event.previous_volume_limit_bytes, 50 * GB)
        self.assertEqual(event.new_volume_limit_bytes, 60 * GB)
        # 40GB of the 50GB cap was used: 10GB survived as rollover.
        self.assertEqual(event.previous_remaining_bytes, 10 * GB)
        self.assertEqual(event.carried_over_bytes, 10 * GB)
        self.assertEqual(event.granted_volume_bytes, 10 * GB)
        self.assertEqual(event.days, 30)
        self.assertFalse(event.traffic_reset)
        self.assertIsNotNone(event.previous_expiry_at)
        # The response carries the fact (redacted) for the UI and for audits.
        self.assertIn('renewal_event', payload)
        self.assertEqual(payload['renewal_event']['operation_id'], 'renew-op-1')
        self.assertIsNone(payload['renewal_event']['client_email'])

    def test_an_explicit_renewal_does_not_need_a_counter_reset(self):
        # 40GB used everywhere: no counter movement, yet the cycle boundary exists.
        response = self._renew(operation_id='renew-op-noreset')
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(RenewalEvent.query.filter_by(
            operation_id='renew-op-noreset', verified=True).count(), 1)

    def test_a_retried_renew_replays_without_a_second_event(self):
        first = self._renew(operation_id='renew-op-retry')
        self.assertTrue(first.get_json()['success'])
        second = self._renew(operation_id='renew-op-retry')
        body = second.get_json()
        self.assertTrue(body['success'], body)
        self.assertTrue(body.get('idempotent_replay'), body)
        self.assertEqual(RenewalEvent.query.filter_by(
            operation_id='renew-op-retry').count(), 1)
        # The replay never wrote to the panel again.
        self.assertEqual(self.v3_update.call_count, 1)

    def test_a_failed_panel_write_creates_no_verified_event(self):
        self.v3_update.return_value = (False, {}, 'panel rejected the update')
        response = self._renew(operation_id='renew-op-fail')
        self.assertFalse(response.get_json()['success'], response.get_json())
        self.assertEqual(RenewalEvent.query.filter_by(
            operation_id='renew-op-fail', verified=True).count(), 0)

    def test_a_read_back_mismatch_creates_no_verified_event(self):
        self.rewrite_readback = True
        response = self._renew(operation_id='renew-op-stale')
        payload = response.get_json()
        self.assertFalse(payload.get('verify', {}).get('ok'), payload)
        self.assertEqual(RenewalEvent.query.filter_by(
            operation_id='renew-op-stale', verified=True).count(), 0)

    def test_a_volume_top_up_is_a_renewal_of_the_cycle(self):
        response = self._renew(operation_id='renew-op-topup', volume=25)
        self.assertTrue(response.get_json()['success'])
        event = RenewalEvent.query.filter_by(operation_id='renew-op-topup').one()
        self.assertEqual(event.granted_volume_bytes, 25 * GB)
        self.assertEqual(event.new_volume_limit_bytes, 75 * GB)
        self.assertEqual(event.carried_over_bytes, 10 * GB)


if __name__ == '__main__':
    unittest.main()
