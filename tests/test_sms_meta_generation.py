"""The gateway contract for outbound meta: generation is never null.

Production failure this file pins: the transactional create/renew path resolved
its serviceKey from the EMAIL while the renewal had written the durable row
under the UUID, so the generation lookup missed and the payload went out with
`"generation": null` -> HTTP 400 invalid_meta / meta_generation_required.
"""
import base64
import os
import tempfile
import unittest
from unittest import mock

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL',
                    'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
import panel.jobs.messaging as messaging  # noqa: E402
from app import GLOBAL_SERVER_DATA, app, db  # noqa: E402
from panel.models import ServiceLifecycleState, SmsSendLog  # noqa: E402
from panel.services import lifecycle as lifecycle_service  # noqa: E402


def _push_context(case):
    ctx = app.app_context()
    ctx.push()
    case.addCleanup(ctx.pop)


def _seed_snapshot(server_id, email, client_uuid):
    GLOBAL_SERVER_DATA['inbounds'] = [{
        'server_id': server_id, 'id': 1, 'protocol': 'vless',
        'clients': [{'email': email, 'id': client_uuid,
                     'totalGB': 0, 'expiryTimestamp': 0, 'enable': True,
                     'comment': '09121234567'}],
    }]


class SendMetaContractTests(unittest.TestCase):
    """The local assertion that replaces a wasted 400 from the gateway."""

    def test_a_complete_block_passes(self):
        self.assertIsNone(lifecycle_service.validate_send_meta({
            'source': 'eve', 'serviceKey': 'eve:1:uuid-a',
            'notificationKind': 'renew', 'generation': 18,
        }))

    def test_a_null_generation_is_refused_locally(self):
        reason = lifecycle_service.validate_send_meta({
            'source': 'eve', 'serviceKey': 'eve:1:uuid-a',
            'notificationKind': 'renew', 'generation': None,
        })
        self.assertEqual(reason, 'meta_generation_missing')

    def test_every_required_field_is_enforced(self):
        base = {'source': 'eve', 'serviceKey': 'eve:1:uuid-a',
                'notificationKind': 'renew', 'generation': 0}
        for field in ('source', 'serviceKey', 'notificationKind'):
            broken = dict(base)
            broken[field] = ''
            self.assertEqual(lifecycle_service.validate_send_meta(broken),
                             'meta_%s_missing' % field)
        self.assertEqual(lifecycle_service.validate_send_meta(None),
                         'meta_missing')
        for bad in ('18', 18.0, True, -1):
            broken = dict(base, generation=bad)
            self.assertIsNotNone(
                lifecycle_service.validate_send_meta(broken), bad)

    def test_send_sms_refuses_to_post_invalid_meta(self):
        cfg = {'provider': 'gmweb', 'base_url': 'https://gw.test',
               'api_key': 'k'}
        with mock.patch.object(messaging.requests, 'post') as post:
            result = messaging._send_sms_via_gmweb(
                '09121234567', 'hi', cfg, priority='critical',
                meta={'source': 'eve', 'serviceKey': 'eve:1:uuid-a',
                      'notificationKind': 'renew', 'generation': None})
        post.assert_not_called()  # zero HTTP sends
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'meta_generation_missing')

    def test_no_meta_aware_post_may_carry_a_null_generation(self):
        """Belt and braces: every builder in the module is checked at once."""
        builders = [
            lambda: messaging._transactional_notification_meta(
                'eve:1:uuid-a', 'renew', 18),
            lambda: messaging._transactional_notification_meta(
                'eve:1:uuid-a', 'created', 0),
            lambda: messaging._depletion_notification_meta(
                'eve:1:uuid-a', 7, 'volume_ended'),
        ]
        for build in builders:
            meta = messaging._notification_meta(build())
            self.assertIsNone(lifecycle_service.validate_send_meta(meta), meta)
            self.assertIsInstance(meta['generation'], int)
        with self.assertRaises(ValueError):
            messaging._transactional_notification_meta(
                'eve:1:uuid-a', 'renew', None)
        with self.assertRaises(ValueError):
            messaging._depletion_notification_meta(
                'eve:1:uuid-a', None, 'volume_ended')

SMS_CFG = {
    'enabled': True, 'provider': 'gmweb', 'base_url': 'https://gw.test',
    'api_key': 'k', 'trigger_near_expiry': True, 'trigger_low_volume': True,
    'trigger_expired': True, 'trigger_ended': True,
    'depletion_expiry_days': 3, 'depletion_volume_gb': 2.0,
    'cooldown_hours': {'near_expiry': 24, 'low_volume': 24, 'expired': 48,
                       'ended': 24},
    'expired_max_age_days': 30, 'ended_max_age_days': 0,
    'min_interval_seconds': 0, 'daily_limit': 200, 'hourly_limit': 0,
    'send_pace_seconds': 0, 'quiet_enabled': False, 'skip_unlimited': False,
}


class DurableGenerationTests(unittest.TestCase):
    """Canonical identity + durable baseline, end to end through the helpers."""

    server_id = 41

    def setUp(self):
        _push_context(self)
        self._orig = {key: GLOBAL_SERVER_DATA.get(key)
                      for key in ('inbounds', 'stats', 'servers_status',
                                  'last_update')}
        self.addCleanup(lambda: GLOBAL_SERVER_DATA.update(self._orig))
        for model in (ServiceLifecycleState, SmsSendLog):
            try:
                model.query.delete()
            except Exception:
                db.session.rollback()
        db.session.commit()

    # 1 -- renew at generation 18: the POST carries generation 18.
    def test_a_renewal_confirmation_carries_the_durable_generation(self):
        _seed_snapshot(self.server_id, 'bob', 'uuid-bob')
        for _ in range(18):
            lifecycle_service.handle_successful_service_lifecycle_change(
                server_id=self.server_id, client_uuid='uuid-bob',
                client_email='bob', dispatch=False, commit=True)
        key = lifecycle_service.resolve_canonical_service_key(
            self.server_id, email='bob')
        self.assertEqual(key, 'eve:41:uuid-bob')
        state = lifecycle_service.read_or_create_generation(
            key, server_id=self.server_id, client_email='bob')
        self.assertTrue(state['established'])
        self.assertEqual(state['generation'], 18)
        meta = messaging._transactional_notification_meta(
            key, 'renew', state['generation'])
        self.assertEqual(meta['generation'], 18)
        self.assertIsNone(lifecycle_service.validate_send_meta(meta))

    # 2 -- a service with no lifecycle row gets a durable baseline 0.
    def test_a_legacy_service_gets_a_durable_baseline_of_zero(self):
        _seed_snapshot(self.server_id, 'carol', 'uuid-carol')
        key = lifecycle_service.resolve_canonical_service_key(
            self.server_id, email='carol')
        self.assertIsNone(lifecycle_service.generation_state(key)['generation'])
        state = lifecycle_service.read_or_create_generation(
            key, server_id=self.server_id, client_email='carol',
            reason='created_confirmation')
        self.assertTrue(state['established'])
        self.assertEqual(state['generation'], 0)
        self.assertTrue(state['created'])
        # DURABLE: a second reader sees the stored row, not a fresh baseline.
        db.session.expire_all()
        again = lifecycle_service.read_or_create_generation(
            key, server_id=self.server_id, client_email='carol')
        self.assertEqual(again['generation'], 0)
        self.assertFalse(again['created'])
        self.assertEqual(
            ServiceLifecycleState.query.filter_by(service_key=key).count(), 1)

    # 3 -- UUID identity must never silently read the email-based key.
    def test_uuid_identity_never_reads_the_email_based_service_key(self):
        _seed_snapshot(self.server_id, 'dave', 'uuid-dave')
        canonical = lifecycle_service.resolve_canonical_service_key(
            self.server_id, email='dave')
        email_key = lifecycle_service.make_service_key(self.server_id, 'dave')
        self.assertEqual(canonical, 'eve:41:uuid-dave')
        self.assertNotEqual(canonical, email_key)
        lifecycle_service.handle_successful_service_lifecycle_change(
            server_id=self.server_id, client_uuid='uuid-dave',
            client_email='dave', dispatch=False, commit=True)
        self.assertEqual(
            lifecycle_service.generation_state(canonical)['generation'], 1)
        self.assertIsNone(
            lifecycle_service.generation_state(email_key)['generation'])
        resolved = lifecycle_service.read_or_create_generation(canonical)
        self.assertEqual(resolved['generation'], 1)

    # 4 -- a DB lookup failure is never generation 0, and sends nothing.
    def test_a_generation_lookup_failure_fails_closed_with_zero_sends(self):
        with mock.patch.object(lifecycle_service, '_state_for_key',
                               side_effect=RuntimeError('database is down')):
            result = lifecycle_service.read_or_create_generation(
                'eve:41:uuid-erin', server_id=self.server_id)
            self.assertFalse(result['established'])
            self.assertIsNone(result['generation'])
            self.assertIn('generation_lookup_failed', result['reason'])
            baseline = lifecycle_service.ensure_baseline_generation(
                'eve:41:uuid-erin', server_id=self.server_id)
            self.assertFalse(baseline['established'])
            self.assertIsNone(baseline['generation'])
        cfg = {'provider': 'gmweb', 'base_url': 'https://gw.test',
               'api_key': 'k'}
        with mock.patch.object(messaging.requests, 'post') as post:
            out = messaging._send_sms_via_gmweb(
                '09121234567', 'hi', cfg, priority='critical',
                meta={'source': 'eve', 'serviceKey': 'eve:41:uuid-erin',
                      'notificationKind': 'renew', 'generation': None})
        post.assert_not_called()
        self.assertFalse(out['sent'])

    # 5 -- a depletion candidate with no resolvable generation sends nothing.
    def test_a_depletion_candidate_without_a_generation_is_skipped(self):
        sent = []
        total = 10 * 1024 ** 3
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server_id, 'id': 1, 'protocol': 'vless',
            'clients': [{'email': 'frank', 'id': 'uuid-frank',
                         'totalGB': total, 'up': total, 'down': 0,
                         'expiryTimestamp': 0, 'enable': True,
                         'comment': '09121234567'}],
        }]
        with mock.patch.object(app_module, '_send_sms_via_gmweb',
                               side_effect=lambda *a, **k: sent.append(k) or {
                                   'sent': True}), \
             mock.patch.object(app_module, '_get_monitor_settings',
                               return_value={'filters': {},
                                             'templates': {'ended': 'ended'}}), \
             mock.patch.object(app_module, 'fetch_and_update_global_data',
                               return_value=False), \
             mock.patch.object(messaging, '_get_sms_runtime_settings',
                               return_value=dict(SMS_CFG)), \
             mock.patch.object(messaging, '_sms_gateway_ready',
                               return_value=(True, None, 200)), \
             mock.patch.object(lifecycle_service, '_state_for_key',
                               side_effect=RuntimeError('database is down')):
            result = messaging._run_sms_depletion_scan(triggered_by='manual')
        self.assertEqual(sent, [])
        self.assertEqual(result.get('sent'), 0)


if __name__ == '__main__':
    unittest.main()

