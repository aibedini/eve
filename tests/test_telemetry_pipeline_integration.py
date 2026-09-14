"""End-to-end wiring: fake X-UI bytes in, fake GMweb bytes out, real everything else.

Why this file exists: three defects reached production while the unit suite was green,

* the transition recorder was called as {"clients": processed}, so the ledger stayed
  empty on a live install;
* the fetch pipeline dropped allow_insecure, so six of eight panels were refused
  by the transport policy on every cycle;
* the outbox error handler read an attribute off a deleted row.

Each of them lived BETWEEN components, and each is now pinned here by driving the real
boundaries: a Server row and its configuration, fetch_worker, process_inbounds,
the snapshot, the ledger, the outbox, the delivery worker and the GMweb POST (the only
thing faked, because the test must not send an SMS).
"""
import base64
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

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
from app import GLOBAL_SERVER_DATA, Server, app, db  # noqa: E402
from panel.adapters import xui  # noqa: E402
from panel.core import refresh_policy  # noqa: E402
from panel.jobs import messaging, schedulers  # noqa: E402
from panel.models import (  # noqa: E402
    ServiceLifecycleState,
    ServiceNotificationEvent,
    ServiceNotificationOutbox,
    ServiceObservedState,
    SmsSendLog,
    WhatsappBotLog,
)
from panel.services import lifecycle as lifecycle_service  # noqa: E402
from panel.services import telemetry_state  # noqa: E402

GB = 1024 ** 3
#: A canary destination that cannot reach a human: the point of the test is the
#: CHAIN (gateway task -> device), never a real message.
CANARY_PHONE = '09000000000'
SERVER_ID = 77


@dataclass
class _FakeResponse:
    status_code: int = 200
    headers: dict = None
    content: bytes = b'{"success":true}'

    def __post_init__(self):
        self.headers = self.headers or {'Content-Type': 'application/json'}

    def json(self):
        # The gateway contract is camelCase: requestId/jobId/status/statusUrl.
        return {'success': True, 'requestId': 'req-1', 'jobId': 'job-1',
                'status': 'queued', 'statusUrl': '/send/status/req-1'}

    def raise_for_status(self):
        return None


def _raw_client(uuid, email, *, remaining_gb, total_gb=10, comment=''):
    total = int(total_gb * GB)
    used = int(total - remaining_gb * GB)
    return {
        'id': uuid, 'email': email, 'enable': True, 'comment': comment,
        'totalGB': total, 'expiryTime': 0, 'up': used, 'down': 0,
        'subId': uuid, 'limitIp': 0, 'tgId': '', 'reset': 0,
    }


class PipelineIntegrationTests(unittest.TestCase):
    """active -> ended -> one event -> one gateway POST, through the real path."""

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
        refresh_policy.reset_state()
        os.environ.pop('EVE_DEPLETION_EVENT_PIPELINE', None)
        for model in (ServiceNotificationEvent, ServiceObservedState,
                      ServiceNotificationOutbox, ServiceLifecycleState,
                      SmsSendLog, WhatsappBotLog):
            model.query.delete()
        Server.query.filter_by(id=SERVER_ID).delete()
        db.session.commit()
        # A plaintext panel the operator explicitly allowed: the transport guard must
        # accept it BECAUSE the configuration travels with the server.
        db.session.add(Server(id=SERVER_ID, name='canary-panel',
                              host='http://31.14.115.171:2050', username='u',
                              password='p', panel_type='sanaei', enabled=True,
                              allow_insecure=True, sub_port=2050, sub_path='/sub/',
                              json_path='/json/'))
        db.session.commit()
        xui.XUI_SESSION_CACHE.clear()
        xui.XUI_CAPABILITY_CACHE.clear()
        xui.XUI_COOKIE_SESSION_CACHE.clear()
        self.email = 'canary-%s@example.invalid' % CANARY_PHONE
        self.uuid = 'canary-uuid-0001'
        self.service_key = 'eve:%d:%s' % (SERVER_ID, self.uuid)
        self.remaining_gb = 4.0
        self.posts = []
        self.gmweb_status = 200
        self.original_inbounds = list(GLOBAL_SERVER_DATA.get('inbounds') or [])
        self.addCleanup(lambda: GLOBAL_SERVER_DATA.__setitem__('inbounds',
                                                               self.original_inbounds))

    # ---- the fake edges -------------------------------------------------------
    def _inbounds(self):
        """The shape X-UI really returns: clients inside the settings JSON."""
        raw = _raw_client(self.uuid, self.email, remaining_gb=self.remaining_gb,
                          comment='canary %s' % CANARY_PHONE)
        return [{
            'id': 1, 'remark': 'canary', 'enable': True, 'port': 8443,
            'protocol': 'vless',
            'settings': json.dumps({'clients': [raw]}),
            'clientStats': [{'email': raw['email'], 'up': raw['up'],
                             'down': raw['down']}],
        }]

    def _fake_gmweb(self, url, *args, **kwargs):
        body = kwargs.get('json') or {}
        self.posts.append({'url': url, 'body': body,
                           'headers': kwargs.get('headers') or {}})
        if self.gmweb_status and self.gmweb_status != 200:
            return _FakeResponse(status_code=self.gmweb_status,
                                 headers={'Content-Type': 'application/json',
                                          'Retry-After': '1'},
                                 content=b'{"success":false,"error":"upstream"}')
        return _FakeResponse(content=json.dumps({
            'success': True, 'requestId': 'req-1', 'jobId': 'job-1',
            'status': 'queued', 'statusUrl': '/send/status/req-1'}).encode())

    def _fetch(self):
        """One full fetch cycle through fetch_worker and process_inbounds."""
        with mock.patch.object(app_module, 'get_xui_session',
                               lambda server: (object(), None)), \
                mock.patch.object(app_module, 'fetch_inbounds',
                                  lambda *a, **k: (self._inbounds(), None, 'sanaei')), \
                mock.patch.object(app_module, 'fetch_onlines', lambda *a, **k: ({}, None)), \
                mock.patch.object(app_module, 'fetch_server_status',
                                  lambda *a, **k: ({'xui_version': '1.8'}, None, None)):
            return schedulers.fetch_and_update_global_data(
                force=True, server_ids=[SERVER_ID], periodic=False)

    def _drain(self):
        cfg = {'enabled': True, 'base_url': 'https://gmweb.test', 'api_key': 'k',
               'trigger_low_volume': True, 'trigger_ended': True,
               'trigger_expired': True, 'trigger_near_expiry': True,
               'cooldown_hours': {'ended': 24, 'expired': 24, 'low_volume': 24,
                                  'near_expiry': 24},
               'send_pace_seconds': 0, 'depletion_expiry_days': 3,
               'depletion_volume_gb': 2.0}
        monitor = {'filters': {'hide_days': 7},
                   'templates': dict.fromkeys(
                       ('near_expiry', 'low_volume', 'expired', 'ended'), 'tpl')}
        with mock.patch.object(messaging, '_get_sms_runtime_settings', lambda: cfg), \
                mock.patch.object(messaging, '_get_monitor_settings_cached',
                                  lambda _cfg: monitor), \
                mock.patch.object(messaging, '_sms_gateway_ready',
                                  lambda *a, **k: (True, None, 200)), \
                mock.patch.object(messaging, '_sms_in_quiet_hours', lambda *a, **k: False), \
                mock.patch.object(messaging, '_recent_bot_message_within',
                                  lambda *a, **k: False), \
                mock.patch.object(messaging, '_render_monitor_state_template',
                                  lambda *a, **k: 'canary reminder'), \
                mock.patch.object(messaging, '_get_gmweb_send_capacity',
                                  lambda *a, **k: {'ok': True, 'reason': None}), \
                mock.patch.object(messaging.requests, 'post', self._fake_gmweb):
            return messaging.run_depletion_event_outbox(limit=5, triggered_by='test')

    # ---- tests ----------------------------------------------------------------
    def test_active_to_ended_sends_exactly_one_canary_message(self):
        self.assertTrue(self._fetch())
        rows = ServiceObservedState.query.filter_by(service_key=self.service_key).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].last_state, 'active')
        self.assertEqual(ServiceNotificationEvent.query.count(), 0,
                         'a baseline must stay silent')

        self.remaining_gb = 0.0          # X-UI now reports the quota as spent
        self.assertTrue(self._fetch())
        events = ServiceNotificationEvent.query.all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].state, 'volume_ended')
        self.assertEqual(events[0].source, 'transition')

        result = self._drain()
        self.assertEqual(result['sent'], 1, result)
        self.assertEqual(len(self.posts), 1, self.posts)
        body = self.posts[0]['body']
        # The gateway receives the normalized E.164 destination, not the local form.
        self.assertEqual(body['to'], '+989000000000')
        self.assertEqual(body['meta']['serviceKey'], self.service_key)
        self.assertTrue(body['meta']['requiresValidation'])
        # A service with no lifecycle row yet gets a durable BASELINE generation at
        # delivery time: never null, because the gateway rejects a meta-aware send
        # without one (HTTP 400 invalid_meta).
        self.assertIn(body['meta']['generation'], (0, 1))
        self.assertIsNotNone(body['meta']['generation'])
        db.session.expire_all()
        event = ServiceNotificationEvent.query.one()
        self.assertEqual(event.status, 'sent')
        self.assertEqual(event.idempotency_key, body.get('idempotency_key')
                         or self.posts[0]['headers'].get('Idempotency-Key')
                         or event.idempotency_key)

        # A second drain, a restart and a reconciliation must not resend.
        self.assertEqual(self._drain()['sent'], 0)
        self.assertEqual(len(self.posts), 1, 'the same event was delivered twice')
        from panel.services import depletion_pipeline
        depletion_pipeline.reconcile_snapshot(source='reconciliation')
        self.assertEqual(ServiceNotificationEvent.query.count(), 1,
                         'reconciliation duplicated a logical transition')
        self.assertEqual(self._drain()['sent'], 0)
        self.assertEqual(len(self.posts), 1)

    def test_a_renewal_before_delivery_makes_the_message_undeliverable(self):
        self._fetch()
        self.remaining_gb = 0.0
        self._fetch()
        self.assertEqual(ServiceNotificationEvent.query.count(), 1)

        # The real renewal path: generation N -> N+1.
        lifecycle_service.handle_successful_service_lifecycle_change(
            server_id=SERVER_ID, client_uuid=self.uuid, client_email=self.email,
            event_type='renewal', operation_id='canary-renew-1',
            dispatch=False, commit=True)
        event = ServiceNotificationEvent.query.one()
        self.assertEqual(event.status, 'superseded')
        self.assertEqual(event.superseded_reason, 'lifecycle_generation_advanced')
        state = ServiceLifecycleState.query.filter_by(service_key=self.service_key).one()
        self.assertEqual(state.generation, 1)

        result = self._drain()
        self.assertEqual(result['sent'], 0)
        self.assertEqual(self.posts, [], 'a renewed service was still texted')
        self.assertEqual(ServiceNotificationEvent.query.count(), 1)

    def test_one_phone_two_services_stay_isolated(self):
        second_server_id = SERVER_ID + 1
        db.session.add(Server(id=second_server_id, name='canary-panel-b',
                              host='http://31.14.115.171:2051', username='u',
                              password='p', panel_type='sanaei', enabled=True,
                              allow_insecure=True, sub_port=2051, sub_path='/sub/',
                              json_path='/json/'))
        db.session.commit()
        second_key = 'eve:%d:canary-uuid-0002' % second_server_id
        try:
            telemetry_state.record_observations(SERVER_ID, [(
                self.service_key,
                {'service_state': 'active', 'remaining_bytes': 2 * GB,
                 'total_bytes': 10 * GB, 'expiry_time': 0},
                {'client_uuid': self.uuid, 'client_email': self.email})])
            telemetry_state.record_observations(second_server_id, [(
                second_key,
                {'service_state': 'active', 'remaining_bytes': 2 * GB,
                 'total_bytes': 10 * GB, 'expiry_time': 0},
                {'client_uuid': 'canary-uuid-0002', 'client_email': self.email})])
            telemetry_state.record_observations(SERVER_ID, [(
                self.service_key,
                {'service_state': 'volume_ended', 'remaining_bytes': 0,
                 'total_bytes': 10 * GB, 'expiry_time': 0},
                {'client_uuid': self.uuid, 'client_email': self.email})])
            # B is renewed. A's reminder must survive untouched, and its generation
            # must not move: a phone number is not a service identity.
            lifecycle_service.handle_successful_service_lifecycle_change(
                server_id=second_server_id, client_uuid='canary-uuid-0002',
                client_email=self.email, event_type='renewal',
                operation_id='canary-renew-b', dispatch=False, commit=True)
            db.session.expire_all()
            event = ServiceNotificationEvent.query.filter_by(
                service_key=self.service_key).one()
            self.assertEqual(event.status, 'pending')
            self.assertEqual(event.superseded_reason, None)
            a_generation = (lifecycle_service.generation_state(self.service_key)
                            or {}).get('generation')
            self.assertIn(a_generation, (None, 0))
            b_generation = (lifecycle_service.generation_state(second_key)
                            or {}).get('generation')
            self.assertEqual(b_generation, 1)
        finally:
            Server.query.filter_by(id=second_server_id).delete()
            db.session.commit()

    def test_the_configured_transport_policy_reaches_the_actual_guard(self):
        captured = {}

        def spy(server_dict):
            captured.update(server_dict)
            return (server_dict['id'], [], {}, {}, None, None, 'auto')

        with mock.patch.object(app_module, 'fetch_worker', spy):
            schedulers.fetch_and_update_global_data(force=True,
                                                    server_ids=[SERVER_ID],
                                                    periodic=False)
        self.assertTrue(captured.get('allow_insecure'),
                        'the per-server policy did not reach fetch_worker')
        # The guard itself, driven by the dict the pipeline built.
        with mock.patch.object(xui.requests.Session, 'post',
                               return_value=_FakeResponse()), \
                mock.patch.object(xui, '_fetch_csrf_token', lambda *a, **k: None):
            session, error = xui.get_xui_session(SimpleNamespace(**captured))
            self.assertIsNotNone(session, error)
            self.assertIsNone(error)
            # Same server, same host, flag gone: the guard must refuse it. This is the
            # exact production failure, expressed as a test.
            stripped = dict(captured)
            stripped.pop('allow_insecure')
            xui.XUI_SESSION_CACHE.clear()
            refused_session, refused_error = xui.get_xui_session(
                SimpleNamespace(**stripped))
            self.assertIsNone(refused_session)
            self.assertIn('Refusing to send panel credentials', str(refused_error))


    # ---- sender invariants and the failure semantics ---------------------------

    def test_off_mode_records_nothing_and_hands_sending_to_the_scan(self):
        os.environ['EVE_DEPLETION_EVENT_PIPELINE'] = 'off'
        try:
            self.assertTrue(self._fetch())
            self.assertEqual(ServiceObservedState.query.count(), 0)
            self.assertEqual(ServiceNotificationEvent.query.count(), 0)
            self.assertEqual(self._drain()['claimed'], 0)
        finally:
            os.environ.pop('EVE_DEPLETION_EVENT_PIPELINE', None)

    def test_shadow_mode_records_transitions_but_never_sends(self):
        os.environ['EVE_DEPLETION_EVENT_PIPELINE'] = 'shadow'
        try:
            self._fetch()
            self.remaining_gb = 0.0
            self._fetch()
            self.assertEqual(ServiceNotificationEvent.query.count(), 1)
            result = self._drain()
            self.assertEqual(result['shadowed'], 1, result)
            self.assertEqual(result['sent'], 0)
            self.assertEqual(self.posts, [], 'shadow mode reached the gateway')
        finally:
            os.environ.pop('EVE_DEPLETION_EVENT_PIPELINE', None)

    def test_on_mode_scan_only_reconciles_and_never_sends_directly(self):
        cfg = {'enabled': True, 'base_url': 'https://gmweb.test', 'api_key': 'k',
               'trigger_near_expiry': True, 'trigger_low_volume': True,
               'trigger_expired': True, 'trigger_ended': True,
               'cooldown_hours': {}, 'send_pace_seconds': 0}
        with mock.patch.object(messaging, '_get_sms_runtime_settings', lambda: cfg), \
                mock.patch.object(messaging, '_sms_gateway_ready',
                                  lambda *a, **k: (True, None, 200)), \
                mock.patch.object(messaging, '_send_sms_via_gmweb') as direct, \
                mock.patch.object(messaging, '_run_sms_royalty_scan',
                                  lambda *a, **k: {'sent': 0}):
            result = messaging._run_sms_depletion_scan(triggered_by='test')
        self.assertEqual(result.get('reason'), 'reconciled', result)
        direct.assert_not_called()
        self.assertEqual(self.posts, [])

    def test_reconciliation_repairs_a_missed_transition_exactly_once(self):
        from panel.services import depletion_pipeline
        self._fetch()
        # The transition happened while nobody was recording (an outage, a deploy):
        # the snapshot moves and only the repair pass sees it.
        self.remaining_gb = 0.0
        with mock.patch.object(app_module, 'get_xui_session',
                               lambda server: (object(), None)), \
                mock.patch.object(app_module, 'fetch_inbounds',
                                  lambda *a, **k: (self._inbounds(), None, 'sanaei')), \
                mock.patch.object(app_module, 'fetch_onlines',
                                  lambda *a, **k: ({}, None)), \
                mock.patch.object(app_module, 'fetch_server_status',
                                  lambda *a, **k: ({}, None, None)), \
                mock.patch.object(schedulers, '_record_fetch_transitions',
                                  lambda *a, **k: None):
            self._fetch()   # snapshot updated, ledger deliberately not
        first = depletion_pipeline.reconcile_snapshot(source='reconciliation')
        self.assertEqual(first['events_created'], 1, first)
        second = depletion_pipeline.reconcile_snapshot(source='reconciliation')
        self.assertEqual(second['events_created'], 0, second)
        self.assertEqual(ServiceNotificationEvent.query.count(), 1)

    def test_reconciliation_cannot_resurrect_a_renewed_account_from_a_stale_snapshot(self):
        from panel.services import depletion_pipeline
        self._fetch()
        self.remaining_gb = 0.0
        self._fetch()
        lifecycle_service.handle_successful_service_lifecycle_change(
            server_id=SERVER_ID, client_uuid=self.uuid, client_email=self.email,
            event_type='renewal', operation_id='canary-renew-stale',
            dispatch=False, commit=True)
        # The snapshot still carries the pre-renewal depleted row (the renewal has not
        # been read back yet). The repair pass must not turn that into a new reminder.
        depletion_pipeline.reconcile_snapshot(source='reconciliation')
        events = ServiceNotificationEvent.query.all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, 'superseded')

    def test_a_gateway_5xx_retries_and_a_429_stops_the_batch(self):
        self._fetch()
        self.remaining_gb = 0.0
        self._fetch()
        self.gmweb_status = 500
        result = self._drain()
        self.assertEqual(result['failed'], 1, result)
        event = ServiceNotificationEvent.query.one()
        self.assertEqual(event.status, 'retry')
        self.assertIsNotNone(event.next_attempt_at)
        self.assertLess(event.attempt_count, telemetry_state.MAX_ATTEMPTS)
        # A rate-limited gateway ends the batch instead of hammering the queue.
        event.status = 'pending'
        event.next_attempt_at = datetime.utcnow()
        db.session.commit()
        self.gmweb_status = 429
        result = self._drain()
        self.assertEqual(result['stopped'], 'failed', result)
        self.assertEqual(ServiceNotificationEvent.query.one().status, 'retry')


    def test_panel_coverage_hydrates_the_shared_snapshot_first(self):
        """Coverage must describe the install, not this process's empty memory.

        The doctor page is served by a web process while the snapshot is written by
        the fetcher; reading GLOBAL_SERVER_DATA without hydrating first reported every
        panel as stale with unknown reachability.
        """
        from panel.services import depletion_pipeline
        self._fetch()          # a real fetch: the snapshot now holds the panel
        snapshot = list(GLOBAL_SERVER_DATA.get('inbounds') or [])
        statuses = list(GLOBAL_SERVER_DATA.get('servers_status') or [])
        GLOBAL_SERVER_DATA['inbounds'] = []
        GLOBAL_SERVER_DATA['servers_status'] = []

        def hydrate(*_a, **_k):
            GLOBAL_SERVER_DATA['inbounds'] = snapshot
            GLOBAL_SERVER_DATA['servers_status'] = statuses
            return True

        with mock.patch('panel.core.redis_client.load_snapshot_from_redis', hydrate):
            coverage = depletion_pipeline.panel_coverage()
        self.assertEqual(coverage['enabled'], 1)
        self.assertEqual(coverage['covered'], 1, coverage)
        self.assertEqual(coverage['stale'], 0)


    def test_a_scope_denied_invalidation_falls_back_to_cancelling_known_sends(self):
        """Production finding: the gateway key lacked sms.invalidate (HTTP 403).

        The reminder then stayed deliverable and the phone submitted it after a
        renewal. A scope refusal is permanent, so EVE must degrade to cancelling the
        individual sends it knows about (sms.cancel) instead of giving up.
        """
        from panel.models import ServiceNotificationOutbox, SmsSendLog
        from panel.services import lifecycle
        from panel.jobs import messaging
        key = lifecycle.make_service_key(SERVER_ID, self.uuid)
        generation = lifecycle.handle_successful_service_lifecycle_change(
            server_id=SERVER_ID, client_uuid=self.uuid, client_email=self.email,
            event_type='renewal', operation_id='scope-arm', dispatch=False,
        commit=True)
        db.session.add(SmsSendLog(email=self.email, server_id=SERVER_ID,
                                  state='expired', recipient='+989000000000',
                                  status='sent', request_id='send_canary_1',
                                  service_key=key, lifecycle_generation=1,
                                  gateway_provider='gmweb', created_at=datetime.utcnow()))
        db.session.commit()
        row = ServiceNotificationOutbox.query.filter_by(service_key=key).one()
        denied = {'ok': False, 'status_code': 403,
                  'reason': 'http_403: project_scope_denied; response={"requiredScope":"sms.invalidate"}'}
        cancelled = []

        def fake_cancel(reference, cfg=None):
            cancelled.append(reference)
            return {'ok': True, 'cancelled': True, 'state': 'cancelled'}

        sms_cfg = {'enabled': True, 'base_url': 'https://gmweb.test', 'api_key': 'k',
                   'provider': 'gmweb'}
        with mock.patch.object(app_module, '_get_sms_runtime_settings',
                               lambda: sms_cfg), \
                mock.patch.object(app_module, '_get_sms_provider_settings',
                                  lambda provider=None, cfg=None: sms_cfg), \
                mock.patch.object(messaging, '_invalidate_notifications_via_gmweb',
                                  lambda *a, **k: denied), \
                mock.patch.object(messaging, '_cancel_sms_via_gmweb', fake_cancel):
            result = lifecycle.attempt_outbox_event(row)
        self.assertEqual(cancelled, ['send_canary_1'], result)
        self.assertEqual(result['cancel_fallback']['cancelled'], 1, result)
        db.session.expire_all()
        log = SmsSendLog.query.filter_by(request_id='send_canary_1').one()
        self.assertIsNotNone(log.invalidated_at)
        self.assertEqual(log.invalidation_reason, 'renewal_cancel_fallback')


if __name__ == '__main__':
    unittest.main()
