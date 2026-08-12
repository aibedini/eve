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
import panel.routes.clients as clients_module  # noqa: E402
import panel.routes.packages as packages_module  # noqa: E402
from app import (  # noqa: E402
    GLOBAL_SERVER_DATA,
    Admin,
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

        self._orig_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = []

        self.session_obj = mock.Mock()
        self.v3_update = mock.Mock(return_value=(True, {}, None))
        self.v3_enable = mock.Mock(return_value=(True, {}, None))
        self.postcheck = mock.Mock()
        self._patches = [
            mock.patch.object(app_module, 'get_xui_session',
                              return_value=(self.session_obj, None)),
            mock.patch.object(app_module, 'server_is_v3', return_value=True),
            mock.patch.object(app_module, 'v3_update_client', self.v3_update),
            mock.patch.object(app_module, 'v3_enable_client', self.v3_enable),
            mock.patch.object(app_module, 'v3_reset_client',
                              return_value=(True, {}, None)),
            mock.patch.object(app_module, '_fire_automation_sms'),
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
        GLOBAL_SERVER_DATA['inbounds'] = self._orig_inbounds
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

        # v3 fast path defers verification to the background post-check, which
        # receives an enable=True snapshot to re-assert if the panel lags.
        self.assertEqual(self.postcheck.call_count, 1)
        snapshot = self.postcheck.call_args[0][3]
        self.assertTrue(snapshot['enable'])

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


if __name__ == '__main__':
    unittest.main()
