import json
import os
import tempfile
import time
import unittest
from unittest import mock


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
import panel.adapters.xui as xui_adapter  # noqa: E402
import panel.core.redis_client as redis_cache  # noqa: E402
import panel.jobs.refresh as refresh_jobs  # noqa: E402
import panel.routes.clients as clients_module  # noqa: E402
import panel.routes.packages as packages_module  # noqa: E402
from app import (  # noqa: E402
    GLOBAL_SERVER_DATA,
    Admin,
    ClientOperation,
    Server,
    SystemConfig,
    Transaction,
    app,
    db,
)

DAY_MS = 86400000
GB = 1024 ** 3


def _raw_client(email='bob', expiry=0, total=0, enable=True, **overrides):
    raw = {
        'id': 'uuid-bob-1',
        'email': email,
        'comment': '',
        'enable': enable,
        'expiryTime': expiry,
        'totalGB': total,
        'subId': 'subidbob1234567',
        'limitIp': 0,
        'flow': '',
        'tgId': '',
        'reset': 0,
    }
    raw.update(overrides)
    return raw


def _cached_inbound(server_id, clients):
    return {'server_id': server_id, 'id': 1, 'protocol': 'vless', 'clients': clients}


def _cached_client_row(server_id, raw, up=0, down=0):
    return {
        'server_id': server_id,
        'inbound_id': 1,
        'email': raw.get('email'),
        'id': raw.get('id'),
        'up': up,
        'down': down,
        'totalGB': raw.get('totalGB'),
        'expiryTimestamp': raw.get('expiryTime'),
        'raw_client': raw,
    }


def _panel_inbounds(raw, server_id):
    """Shape find_client() expects: settings JSON with the clients list."""
    return [{'id': 1, 'settings': json.dumps({'clients': [raw]})}]


class V3EnableAdapterTests(unittest.TestCase):
    def test_uses_bulk_enable_when_supported(self):
        server = mock.Mock()
        session_obj = mock.Mock()
        client = _raw_client(enable=False)
        with (
            mock.patch.object(xui_adapter, '_v3_fix_spaced_email', return_value='bob'),
            mock.patch.object(
                xui_adapter, '_v3_post', return_value=(True, {'success': True}, None),
            ) as post,
        ):
            ok, _result, error = xui_adapter.v3_enable_client(
                server, session_obj, 'bob', client,
            )

        self.assertTrue(ok)
        self.assertIsNone(error)
        post.assert_called_once_with(
            server, session_obj, '/panel/api/clients/bulkEnable',
            {'emails': ['bob']},
        )

    def test_falls_back_to_update_when_bulk_enable_is_unavailable(self):
        server = mock.Mock()
        session_obj = mock.Mock()
        client = _raw_client(enable=False)
        with (
            mock.patch.object(xui_adapter, '_v3_fix_spaced_email', return_value='bob'),
            mock.patch.object(
                xui_adapter, '_v3_post',
                side_effect=[
                    (False, None, 'Non-JSON response (status 404, content-type text/html)'),
                    (True, {'success': True}, None),
                ],
            ) as post,
        ):
            ok, _result, error = xui_adapter.v3_enable_client(
                server, session_obj, 'bob', client,
            )

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual(post.call_count, 2)
        fallback_path = post.call_args_list[1].args[2]
        fallback_payload = post.call_args_list[1].args[3]
        self.assertEqual(fallback_path, '/panel/api/clients/update/bob')
        self.assertTrue(fallback_payload['enable'])

    def test_bulk_enable_success_with_skipped_client_is_failure(self):
        server = mock.Mock()
        session_obj = mock.Mock()
        client = _raw_client(enable=False)
        result = {
            'success': True,
            'obj': {'changed': 0, 'skipped': [
                {'email': 'bob', 'reason': 'client not found'},
            ]},
        }
        with (
            mock.patch.object(xui_adapter, '_v3_fix_spaced_email', return_value='bob'),
            mock.patch.object(xui_adapter, '_v3_post', return_value=(True, result, None)),
        ):
            ok, _result, error = xui_adapter.v3_enable_client(
                server, session_obj, 'bob', client,
            )

        self.assertFalse(ok)
        self.assertEqual(error, 'client not found')


class RenewEnableTests(unittest.TestCase):
    """Renewal must always re-enable the client (manual or panel auto-disable)
    and must not extend an already-expired timestamp in the past."""

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
        ClientOperation.query.delete()
        Transaction.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()

        self.admin = Admin(username='renew-owner', password_hash='x',
                           role='superadmin', is_superadmin=True)
        self.server = Server(
            name='panel-renew', host='https://panel.example:8443/base',
            username='u', password='p', sub_path='/sub/', panel_type='auto',
        )
        db.session.add_all([self.admin, self.server])
        db.session.commit()

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
            sess['admin_username'] = self.admin.username
            sess['role'] = self.admin.role
            sess['is_superadmin'] = True

        self._orig_snapshot = {
            key: GLOBAL_SERVER_DATA.get(key)
            for key in ('inbounds', 'stats', 'servers_status', 'last_update')
        }
        GLOBAL_SERVER_DATA['inbounds'] = []

        self.session_obj = mock.Mock()
        self.v3_update = mock.Mock(return_value=(True, {}, None))
        self.v3_enable = mock.Mock(return_value=(True, {}, None))
        self.postcheck = mock.Mock()

        def fetch_confirmed(*_args, **_kwargs):
            if not self.v3_update.call_args:
                return [], 'not updated', '3x-ui'
            sent = dict(self.v3_update.call_args[0][3])
            sent['enable'] = True
            return _panel_inbounds(sent, self.server.id), None, '3x-ui'

        self._patches = [
            mock.patch.object(app_module, 'get_xui_session',
                              return_value=(self.session_obj, None)),
            mock.patch.object(app_module, 'server_is_v3', return_value=True),
            mock.patch.object(app_module, 'v3_update_client', self.v3_update),
            mock.patch.object(app_module, 'v3_enable_client', self.v3_enable),
            mock.patch.object(app_module, 'v3_reset_client',
                              return_value=(True, {}, None)),
            mock.patch.object(app_module, 'fetch_inbounds', side_effect=fetch_confirmed),
            mock.patch.object(app_module, '_fire_automation_sms'),
            mock.patch.object(app_module, '_fire_cancel_stale_account_sms'),
            mock.patch.object(app_module, '_notify_customer_telegram'),
            mock.patch.object(clients_module, '_fire_renew_whatsapp'),
            mock.patch.object(clients_module, '_fire_renew_postcheck',
                              self.postcheck),
            mock.patch('time.sleep'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        GLOBAL_SERVER_DATA.update(self._orig_snapshot)
        db.session.rollback()
        db.session.remove()

    def _seed_cache(self, raw):
        GLOBAL_SERVER_DATA['inbounds'] = [
            _cached_inbound(self.server.id, [_cached_client_row(self.server.id, raw)]),
        ]

    def _renew(self, email='bob', **payload):
        return self.client.post(
            f'/api/client/{self.server.id}/1/{email}/renew', json=payload)

    def test_expired_disabled_client_renewed_from_now_and_enabled(self):
        past = int(time.time() * 1000) - 10 * DAY_MS
        raw = _raw_client(expiry=past, total=5 * GB, enable=False)
        self._seed_cache(raw)

        resp = self._renew(mode='custom', days=30, volume=10, free=True)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        payload = resp.get_json()
        self.assertTrue(payload['success'], payload)

        # Panel update carries enable=True and an expiry based on NOW
        # (not on the 10-days-ago timestamp, which would stay expired).
        self.assertEqual(self.v3_update.call_count, 1)
        _srv, _sess, _email, sent = self.v3_update.call_args[0]
        self.assertTrue(sent['enable'])
        self.v3_enable.assert_called_once()
        self.assertEqual(self.v3_enable.call_args[0][2], 'bob')
        expected = int(time.time() * 1000) + 30 * DAY_MS
        self.assertLess(abs(sent['expiryTime'] - expected), 120000)
        self.assertEqual(sent['totalGB'], 15 * GB)

        # Success is returned only after a real read-back from 3x-ui.
        self.assertTrue(payload['verify']['attempted'])
        self.assertTrue(payload['verify']['ok'])
        self.assertTrue(payload['verify']['observed']['enable'])
        self.postcheck.assert_not_called()

        # Cookie-authenticated v3 panels have no API token, so capability
        # detection must receive the live authenticated session.
        app_module.server_is_v3.assert_called_with(self.server, self.session_obj)

    def test_completed_renew_operation_replays_without_second_panel_write(self):
        future = int(time.time() * 1000) + DAY_MS
        self._seed_cache(_raw_client(expiry=future, total=5 * GB, enable=True))
        request_payload = {
            'mode': 'custom', 'days': 30, 'volume': 10, 'free': True,
            'operation_id': 'renew-replay-once',
        }
        first = self._renew(**request_payload)
        second = self._renew(**request_payload)

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertTrue(second.get_json()['idempotent_replay'])
        self.assertEqual(self.v3_update.call_count, 1)

    def test_not_started_client_stays_pending(self):
        raw = _raw_client(expiry=-5 * DAY_MS, total=0, enable=True)
        self._seed_cache(raw)

        resp = self._renew(mode='custom', days=30, volume=0, free=True)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()['success'])
        sent = self.v3_update.call_args[0][3]
        self.assertEqual(sent['expiryTime'], -35 * DAY_MS)

    def test_fractional_days_and_volume_are_preserved(self):
        future = int(time.time() * 1000) + 2 * DAY_MS
        raw = _raw_client(email='fractional', expiry=future, total=5 * GB, enable=True)
        self._seed_cache(raw)

        resp = self._renew(email='fractional', mode='custom', days=0.5,
                           volume=0.5, free=True)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()['success'])

        sent = self.v3_update.call_args[0][3]
        self.assertEqual(sent['expiryTime'], future + (DAY_MS // 2))
        self.assertEqual(sent['totalGB'], 5 * GB + (GB // 2))

        transaction = Transaction.query.filter_by(
            client_email='fractional', type='renew'
        ).order_by(Transaction.id.desc()).first()
        self.assertIsNotNone(transaction)
        self.assertEqual(transaction.days, 0.5)
        self.assertEqual(transaction.volume_gb, 0.5)

    def test_fractional_units_are_included_in_minimum_price(self):
        db.session.merge(SystemConfig(key='cost_per_gb', value='1000'))
        db.session.merge(SystemConfig(key='cost_per_day', value='2000'))
        db.session.commit()

        with mock.patch.object(packages_module, '_get_applicable_price_tier',
                               return_value=None):
            price, _cpg, _cpd, _tier = packages_module._calculate_minimum_price(
                0.5, 0.5
            )

        self.assertEqual(price, 1500)

    def test_inline_verify_reasserts_enable_until_panel_confirms(self):
        future = int(time.time() * 1000) + 5 * DAY_MS
        raw = _raw_client(expiry=future, total=5 * GB, enable=False)
        self._seed_cache(raw)

        still_disabled = dict(raw)
        still_disabled['expiryTime'] = future + 30 * DAY_MS
        still_disabled['totalGB'] = 15 * GB
        reenabled = dict(still_disabled)
        reenabled['enable'] = True

        fetch = mock.Mock(side_effect=[
            (_panel_inbounds(still_disabled, self.server.id), None, '3x-ui'),
            (_panel_inbounds(reenabled, self.server.id), None, '3x-ui'),
        ])
        with mock.patch.object(app_module, 'fetch_inbounds', fetch):
            resp = self._renew(mode='custom', days=30, volume=10, free=True,
                               verify_inline=True)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        payload = resp.get_json()
        self.assertTrue(payload['success'], payload)

        # Explicit enable once immediately and once more after disabled read-back.
        self.assertEqual(self.v3_update.call_count, 1)
        self.assertEqual(self.v3_enable.call_count, 2)
        sent = self.v3_enable.call_args[0][3]
        self.assertTrue(sent['enable'])
        verify = payload.get('verify') or {}
        self.assertTrue(verify.get('re_enabled'))
        self.assertTrue(verify.get('ok'))
        self.assertTrue((verify.get('observed') or {}).get('enable'))

    def test_unconfirmed_disabled_client_is_not_reported_or_billed_as_success(self):
        future = int(time.time() * 1000) + 5 * DAY_MS
        raw = _raw_client(expiry=future, total=5 * GB, enable=False)
        self._seed_cache(raw)

        still_disabled = dict(raw)
        still_disabled['expiryTime'] = future + 30 * DAY_MS
        still_disabled['totalGB'] = 15 * GB
        with mock.patch.object(
            app_module, 'fetch_inbounds',
            return_value=(_panel_inbounds(still_disabled, self.server.id), None, '3x-ui'),
        ):
            resp = self._renew(mode='custom', days=30, volume=10, free=True)

        # The app preserves JSON business errors through proxies as HTTP 200
        # and carries the real status in X-Eve-Status.
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.headers.get('X-Eve-Status'), '409')
        payload = resp.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['code'], 'renew_not_verified')
        self.assertFalse(payload['verify']['observed']['enable'])
        self.assertEqual(Transaction.query.filter_by(client_email='bob').count(), 0)

    def test_recheck_reports_each_partially_applied_field(self):
        expected_expiry = int(time.time() * 1000) + 30 * DAY_MS
        expected_total = 15 * GB
        observed = _raw_client(
            expiry=expected_expiry, total=5 * GB, enable=False,
        )
        completed = {
            'state': 'pending',
            'verify': {'expected': {
                'expiryTime': expected_expiry,
                'totalGB': expected_total,
                'enable': True,
            }},
        }
        with (
            mock.patch.object(
                app_module, 'fetch_inbounds',
                return_value=(_panel_inbounds(observed, self.server.id), None, '3x-ui'),
            ),
            mock.patch.object(clients_module, '_load_renew_result', return_value=completed),
        ):
            resp = self.client.post(
                f'/api/client/{self.server.id}/1/bob/renew/verify',
                json={'awaiting_result': True},
            )

        self.assertEqual(resp.status_code, 200, resp.get_json())
        verify = resp.get_json()['verify']
        self.assertFalse(verify['ok'])
        self.assertEqual(verify['state'], 'partially_applied')
        self.assertTrue(verify['checks']['expiryTime']['matches'])
        self.assertFalse(verify['checks']['totalGB']['matches'])
        self.assertFalse(verify['checks']['enable']['matches'])
        self.assertEqual(verify['checks']['totalGB']['observed'], 5 * GB)

    def test_recheck_prefers_fresh_v3_client_over_stale_inbound_list(self):
        expected_expiry = int(time.time() * 1000) + 30 * DAY_MS
        expected_total = 15 * GB
        stale = _raw_client(expiry=DAY_MS, total=5 * GB, enable=True)
        fresh = _raw_client(
            expiry=expected_expiry, total=expected_total, enable=True,
        )
        completed = {'verify': {'expected': {
            'expiryTime': expected_expiry,
            'totalGB': expected_total,
            'enable': True,
        }}}
        with (
            mock.patch.object(
                app_module, 'fetch_inbounds',
                return_value=(_panel_inbounds(stale, self.server.id), None, '3x-ui'),
            ) as fetch,
            mock.patch.object(app_module, '_v3_get_client', return_value=fresh),
            mock.patch.object(clients_module, '_load_renew_result', return_value=completed),
        ):
            resp = self.client.post(
                f'/api/client/{self.server.id}/1/bob/renew/verify',
                json={'awaiting_result': True},
            )

        payload = resp.get_json()
        self.assertTrue(payload['verify']['ok'], payload)
        self.assertEqual(payload['verify']['observed']['expiryTime'], expected_expiry)
        self.assertTrue(fetch.call_args.kwargs['force_fresh'])

    def test_recheck_repairs_disabled_v3_client(self):
        expected_expiry = int(time.time() * 1000) + 30 * DAY_MS
        expected_total = 15 * GB
        disabled = _raw_client(
            expiry=expected_expiry, total=expected_total, enable=False,
        )
        enabled = dict(disabled, enable=True)
        completed = {'verify': {'expected': {
            'expiryTime': expected_expiry,
            'totalGB': expected_total,
            'enable': True,
        }}}
        with (
            mock.patch.object(
                app_module, 'fetch_inbounds',
                return_value=(_panel_inbounds(disabled, self.server.id), None, '3x-ui'),
            ),
            mock.patch.object(
                app_module, '_v3_get_client', side_effect=[disabled, enabled],
            ),
            mock.patch.object(clients_module, '_load_renew_result', return_value=completed),
        ):
            resp = self.client.post(
                f'/api/client/{self.server.id}/1/bob/renew/verify',
                json={'awaiting_result': True},
            )

        payload = resp.get_json()
        self.assertTrue(payload['verify']['ok'], payload)
        self.assertTrue(payload['verify']['re_enabled'])
        self.assertTrue(self.v3_enable.call_args[0][3]['enable'])

    def test_recheck_without_saved_expectation_returns_observed_state(self):
        observed = _raw_client(expiry=7 * DAY_MS, total=3 * GB, enable=True)
        with (
            mock.patch.object(
                app_module, 'fetch_inbounds',
                return_value=(_panel_inbounds(observed, self.server.id), None, '3x-ui'),
            ),
            mock.patch.object(clients_module, '_load_renew_result', return_value=None),
        ):
            resp = self.client.post(
                f'/api/client/{self.server.id}/1/bob/renew/verify',
                json={'awaiting_result': True},
            )

        verify = resp.get_json()['verify']
        self.assertEqual(verify['error'], 'renew_result_unavailable')
        self.assertEqual(verify['state'], 'observed_without_expected')
        self.assertEqual(verify['observed']['totalGB'], 3 * GB)
        self.assertTrue(verify['observed']['enable'])

    def test_successful_recheck_repairs_shared_cache_and_stats(self):
        old = _raw_client(expiry=DAY_MS, total=5 * GB, enable=False)
        self._seed_cache(old)
        expected_expiry = int(time.time() * 1000) + 30 * DAY_MS
        expected_total = 15 * GB
        observed = _raw_client(
            expiry=expected_expiry, total=expected_total, enable=True,
        )
        GLOBAL_SERVER_DATA['servers_status'] = [{
            'server_id': self.server.id,
            'success': True,
            'stats': {},
        }]
        completed = {
            'state': 'complete',
            'verify': {'expected': {
                'expiryTime': expected_expiry,
                'totalGB': expected_total,
                'enable': True,
            }},
        }
        with (
            mock.patch.object(
                app_module, 'fetch_inbounds',
                return_value=(_panel_inbounds(observed, self.server.id), None, '3x-ui'),
            ),
            mock.patch.object(clients_module, '_load_renew_result', return_value=completed),
        ):
            resp = self.client.post(
                f'/api/client/{self.server.id}/1/bob/renew/verify',
                json={'awaiting_result': True},
            )

        payload = resp.get_json()
        self.assertTrue(payload['verify']['ok'], payload)
        self.assertTrue(payload['cache_sync'], payload)
        cached = GLOBAL_SERVER_DATA['inbounds'][0]['clients'][0]
        self.assertEqual(cached['expiryTimestamp'], expected_expiry)
        self.assertEqual(cached['totalGB'], expected_total)
        self.assertTrue(cached['enable'])
        server_stats = GLOBAL_SERVER_DATA['servers_status'][0]['stats']
        self.assertEqual(server_stats['active_clients'], 1)
        self.assertEqual(server_stats['inactive_clients'], 0)

    def test_dashboard_refresh_hydrates_shared_snapshot_before_read(self):
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server.id, 'id': 1, 'clients': [], 'enable': True,
        }]
        GLOBAL_SERVER_DATA['servers_status'] = [{
            'server_id': self.server.id, 'success': True, 'stats': {},
        }]
        GLOBAL_SERVER_DATA['last_update'] = '2026-08-30T00:00:00'
        with mock.patch.object(app_module, 'load_snapshot_from_redis', return_value=False) as load:
            resp = self.client.get('/api/refresh?mode=cache&enqueue=0')

        self.assertEqual(resp.status_code, 200, resp.get_json())
        load.assert_called_once_with()

    def test_legacy_panel_update_carries_enable(self):
        self._patches[1].stop()  # server_is_v3 -> use a fresh False mock
        v3_flag = mock.patch.object(app_module, 'server_is_v3', return_value=False)
        v3_flag.start()
        self._patches[1] = v3_flag

        future = int(time.time() * 1000) + 5 * DAY_MS
        raw = _raw_client(expiry=future, total=5 * GB, enable=False)
        self._seed_cache(raw)

        resp_ok = mock.Mock(status_code=200)
        resp_ok.json.return_value = {'success': True}
        self.session_obj.post.return_value = resp_ok

        observed = dict(raw)
        observed['expiryTime'] = future + 30 * DAY_MS
        observed['totalGB'] = 15 * GB
        observed['enable'] = True
        fetch = mock.Mock(return_value=(
            _panel_inbounds(observed, self.server.id), None, '3x-ui'))
        with mock.patch.object(app_module, 'fetch_inbounds', fetch):
            resp = self._renew(mode='custom', days=30, volume=10, free=True)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()['success'])

        # The legacy updateClient POST carries settings with enable=True.
        posted = None
        for call in self.session_obj.post.call_args_list:
            body = call[1].get('json') or {}
            settings = body.get('settings')
            if settings:
                posted = json.loads(settings)['clients'][0]
                break
        self.assertIsNotNone(posted, 'no updateClient POST observed')
        self.assertTrue(posted['enable'])
        self.assertEqual(posted['expiryTime'], future + 30 * DAY_MS)


class RedisSnapshotRevisionTests(unittest.TestCase):
    def test_cache_patch_bumps_revision_even_when_local_row_is_missing(self):
        with (
            mock.patch.object(refresh_jobs, 'bump_server_revision') as bump,
            mock.patch.object(refresh_jobs, 'serialized_server_snapshot_write') as serialized,
            mock.patch.object(app_module, '_get_dashboard_status_thresholds', return_value={}),
            mock.patch.object(app_module, '_get_panel_ui_lang', return_value='en'),
        ):
            serialized.return_value = redis_cache.contextmanager(lambda: (yield))()
            with app_module.app.app_context():
                changed = refresh_jobs.patch_cached_client(77, 'missing@example', enable=True)

        self.assertFalse(changed)
        bump.assert_called_once_with(77)

    def test_cache_patch_enters_serialized_server_write_cycle(self):
        original = dict(redis_cache.GLOBAL_SERVER_DATA)
        latest = {
            'server_id': 7,
            'id': 1,
            'clients': [{
                'email': 'alice', 'id': 'u1', 'up': 0, 'down': 0,
                'raw_client': {'email': 'alice', 'id': 'u1', 'enable': False},
            }],
        }

        class LatestSnapshotContext:
            def __enter__(self):
                redis_cache.GLOBAL_SERVER_DATA['inbounds'] = [latest]

            def __exit__(self, *_args):
                return False

        try:
            redis_cache.GLOBAL_SERVER_DATA.update({
                'inbounds': [], 'servers_status': [], 'stats': {}, 'last_update': None,
            })
            with (
                mock.patch.object(refresh_jobs, 'bump_server_revision'),
                mock.patch.object(refresh_jobs, 'serialized_server_snapshot_write',
                                  return_value=LatestSnapshotContext()) as serialized,
                mock.patch.object(refresh_jobs, 'publish_snapshot_to_redis', return_value=True),
                mock.patch.object(app_module, '_get_dashboard_status_thresholds', return_value={}),
                mock.patch.object(app_module, '_get_panel_ui_lang', return_value='en'),
            ):
                with app_module.app.app_context():
                    changed = refresh_jobs.patch_cached_client(7, 'alice', enable=True)
        finally:
            redis_cache.GLOBAL_SERVER_DATA.clear()
            redis_cache.GLOBAL_SERVER_DATA.update(original)

        self.assertTrue(changed)
        serialized.assert_called_once_with(7)
        self.assertTrue(latest['clients'][0]['raw_client']['enable'])

    def test_publish_discards_refresh_result_when_server_revision_changed(self):
        client = mock.Mock()
        pipe = mock.Mock()
        client.pipeline.return_value = pipe
        # The refresh started at revision 4, but a renew bumped it to 5.
        pipe.get.side_effect = lambda key: (
            b'5' if key.endswith(':7') else None
        )
        original = dict(redis_cache.GLOBAL_SERVER_DATA)
        redis_cache.GLOBAL_SERVER_DATA.update({
            'inbounds': [{'server_id': 7, 'id': 1, 'clients': []}],
            'servers_status': [{'server_id': 7, 'success': True, 'stats': {}}],
            'stats': {},
            'last_update': 'now',
        })
        try:
            with mock.patch.object(redis_cache, 'get_redis', return_value=client):
                published = redis_cache.publish_snapshot_to_redis(
                    [7], expected_server_revisions={7: 4},
                )
        finally:
            redis_cache.GLOBAL_SERVER_DATA.clear()
            redis_cache.GLOBAL_SERVER_DATA.update(original)

        self.assertFalse(published)
        pipe.unwatch.assert_called_once_with()
        pipe.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
