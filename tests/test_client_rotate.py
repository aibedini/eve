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
from app import (  # noqa: E402
    GLOBAL_SERVER_DATA,
    Admin,
    ClientOwnership,
    CustomerAccount,
    Server,
    ServiceOwnership,
    app,
    db,
)

OLD_UUID = '11111111-2222-3333-4444-555555555555'
OLD_SUB_ID = 'oldsubid12345678'
DAY_MS = 86400000


def _raw_client(email='alice', expiry=0, total=0, **overrides):
    raw = {
        'id': OLD_UUID,
        'email': email,
        'comment': 'vip customer',
        'enable': True,
        'expiryTime': expiry,
        'totalGB': total,
        'subId': OLD_SUB_ID,
        'limitIp': 0,
        'flow': '',
        'tgId': '',
        'reset': 0,
    }
    raw.update(overrides)
    return raw


def _cached_inbound(server_id, clients):
    return {
        'server_id': server_id,
        'id': 1,
        'protocol': 'vless',
        'clients': clients,
    }


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


class ClientRotateTests(unittest.TestCase):
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
        ServiceOwnership.query.delete()
        ClientOwnership.query.delete()
        Server.query.delete()
        Admin.query.delete()
        CustomerAccount.query.delete()
        db.session.commit()

        self.admin = Admin(username='owner', password_hash='x', role='superadmin', is_superadmin=True)
        self.server = Server(
            name='panel-1', host='https://panel.example:8443/base',
            username='u', password='p', sub_path='/sub/', panel_type='auto',
        )
        self.customer = CustomerAccount(display_name='Alice Customer')
        db.session.add_all([self.admin, self.server, self.customer])
        db.session.commit()

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin.id

        self._orig_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = []

        self.v3_update = mock.Mock(return_value=(True, {}, None))
        self.v3_add = mock.Mock(return_value=(True, {}, None))
        self._patches = [
            mock.patch.object(app_module, 'get_xui_session', return_value=(object(), None)),
            mock.patch.object(app_module, 'server_is_v3', return_value=True),
            mock.patch.object(app_module, 'v3_update_client', self.v3_update),
            mock.patch.object(app_module, 'v3_add_client', self.v3_add),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        GLOBAL_SERVER_DATA['inbounds'] = self._orig_inbounds
        db.session.rollback()

    def _post_rotate(self, email='alice'):
        return self.client.post(
            f'/api/client/{self.server.id}/rotate',
            json={'client_email': email},
        )

    def test_rotate_carries_remaining_traffic_and_days(self):
        total = 50 * 1024 ** 3
        up = 10 * 1024 ** 3
        down = 5 * 1024 ** 3
        expiry = int(time.time() * 1000) + 10 * DAY_MS + 60000
        raw = _raw_client(expiry=expiry, total=total)
        GLOBAL_SERVER_DATA['inbounds'] = [
            _cached_inbound(self.server.id, [_cached_client_row(self.server.id, raw, up=up, down=down)]),
        ]

        resp = self._post_rotate()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        payload = resp.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['new_email'], 'alice_v2')
        self.assertEqual(payload['remaining_days'], 10)
        self.assertAlmostEqual(payload['remaining_gb'], 35.0, places=1)

        # Old client disabled with documentation comment (old comment preserved).
        self.assertEqual(self.v3_update.call_count, 1)
        _srv, _sess, old_email, disabled = self.v3_update.call_args[0]
        self.assertEqual(old_email, 'alice')
        self.assertFalse(disabled['enable'])
        self.assertIn('rotated -> alice_v2 (uid/link revoked)', disabled['comment'])
        self.assertIn('vip customer', disabled['comment'])
        self.assertEqual(disabled['id'], OLD_UUID)

        # New client: fresh uuid + subId, remaining quota carried over.
        self.assertEqual(self.v3_add.call_count, 1)
        _srv, _sess, new_client, inbound_ids = self.v3_add.call_args[0]
        self.assertEqual(inbound_ids, [1])
        self.assertEqual(new_client['email'], 'alice_v2')
        self.assertTrue(new_client['enable'])
        self.assertNotEqual(new_client['id'], OLD_UUID)
        self.assertNotEqual(new_client['subId'], OLD_SUB_ID)
        self.assertEqual(len(new_client['subId']), 16)
        self.assertEqual(new_client['totalGB'], total - up - down)
        self.assertIn('rotated from alice', new_client['comment'])
        # expiry ≈ now + remaining
        expected = int(time.time() * 1000) + 10 * DAY_MS
        self.assertLess(abs(new_client['expiryTime'] - expected), 120000)

        # Response links point at the new subId.
        self.assertIn(new_client['subId'], payload['sub_url'])
        self.assertIn('/sub/', payload['sub_url'])
        self.assertIn(f"/s/{self.server.id}/{new_client['subId']}", payload['dash_sub_url'])

    def test_existing_version_suffix_is_incremented(self):
        expiry = int(time.time() * 1000) + 5 * DAY_MS
        raw = _raw_client(expiry=expiry)
        raw_v2 = _raw_client(email='alice_v2', expiry=expiry)
        GLOBAL_SERVER_DATA['inbounds'] = [
            _cached_inbound(self.server.id, [
                _cached_client_row(self.server.id, raw),
                _cached_client_row(self.server.id, raw_v2),
            ]),
        ]

        resp = self._post_rotate()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()['new_email'], 'alice_v3')

    def test_ownership_rows_are_rotated(self):
        expiry = int(time.time() * 1000) + 5 * DAY_MS
        raw = _raw_client(expiry=expiry)
        GLOBAL_SERVER_DATA['inbounds'] = [
            _cached_inbound(self.server.id, [_cached_client_row(self.server.id, raw)]),
        ]
        db.session.add(ServiceOwnership(
            customer_id=self.customer.id, server_id=self.server.id,
            client_uuid=OLD_UUID, client_email_snapshot='alice',
            verification_method='admin',
        ))
        db.session.add(ClientOwnership(
            reseller_id=self.admin.id, server_id=self.server.id,
            inbound_id=1, client_email='alice', client_uuid=OLD_UUID, price=100,
        ))
        db.session.commit()

        resp = self._post_rotate()
        self.assertEqual(resp.status_code, 200, resp.get_json())

        old_so = ServiceOwnership.query.filter_by(
            server_id=self.server.id, client_uuid=OLD_UUID).first()
        self.assertIsNotNone(old_so.revoked_at)
        self.assertFalse(old_so.is_active)

        new_so = ServiceOwnership.query.filter_by(
            server_id=self.server.id, client_email_snapshot='alice_v2').first()
        self.assertIsNotNone(new_so)
        self.assertTrue(new_so.is_active)
        self.assertEqual(new_so.customer_id, self.customer.id)
        self.assertEqual(new_so.verification_method, 'admin')
        self.assertNotEqual(new_so.client_uuid, OLD_UUID)

        co = ClientOwnership.query.filter_by(server_id=self.server.id).first()
        self.assertEqual(co.client_email, 'alice_v2')
        self.assertEqual(co.client_uuid, new_so.client_uuid)

    def test_unlimited_client_stays_unlimited(self):
        raw = _raw_client(expiry=0, total=0)
        GLOBAL_SERVER_DATA['inbounds'] = [
            _cached_inbound(self.server.id, [_cached_client_row(self.server.id, raw)]),
        ]

        resp = self._post_rotate()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        payload = resp.get_json()
        self.assertIsNone(payload['remaining_days'])
        self.assertIsNone(payload['remaining_gb'])
        new_client = self.v3_add.call_args[0][2]
        self.assertEqual(new_client['totalGB'], 0)
        self.assertEqual(new_client['expiryTime'], 0)

    def test_pending_negative_expiry_is_preserved(self):
        pending = -5 * DAY_MS
        raw = _raw_client(expiry=pending, total=10 * 1024 ** 3)
        GLOBAL_SERVER_DATA['inbounds'] = [
            _cached_inbound(self.server.id, [_cached_client_row(self.server.id, raw)]),
        ]

        resp = self._post_rotate()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()['remaining_days'], 5)
        new_client = self.v3_add.call_args[0][2]
        self.assertEqual(new_client['expiryTime'], pending)

    def test_shadowsocks_client_rotates_without_uuid(self):
        expiry = int(time.time() * 1000) + 5 * DAY_MS
        raw = _raw_client(expiry=expiry)
        del raw['id']  # shadowsocks clients have no UUID
        GLOBAL_SERVER_DATA['inbounds'] = [
            _cached_inbound(self.server.id, [_cached_client_row(self.server.id, raw)]),
        ]

        resp = self._post_rotate()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        new_client = self.v3_add.call_args[0][2]
        self.assertNotIn('id', new_client)
        self.assertNotEqual(new_client['subId'], OLD_SUB_ID)

    def test_client_not_found_returns_404(self):
        GLOBAL_SERVER_DATA['inbounds'] = []
        with mock.patch.object(app_module, 'fetch_inbounds', return_value=([], None, 'v3')), \
             mock.patch.object(app_module, 'persist_detected_panel_type', return_value=True):
            resp = self._post_rotate(email='ghost')
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()['ok'])

    def test_missing_email_returns_400(self):
        resp = self.client.post(f'/api/client/{self.server.id}/rotate', json={})
        # after_request downgrades API business errors to 200; real code in X-Eve-Status
        self.assertEqual(resp.headers.get('X-Eve-Status'), '400')
        self.assertFalse(resp.get_json()['ok'])

    def test_old_client_marked_disabled_in_cache(self):
        expiry = int(time.time() * 1000) + 5 * DAY_MS
        raw = _raw_client(expiry=expiry)
        row = _cached_client_row(self.server.id, raw)
        GLOBAL_SERVER_DATA['inbounds'] = [_cached_inbound(self.server.id, [row])]

        resp = self._post_rotate()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(row['raw_client']['enable'])
        self.assertIn('uid/link revoked', row['raw_client']['comment'])
        # New client appended to the cached inbound.
        emails = [c['email'] for c in GLOBAL_SERVER_DATA['inbounds'][0]['clients']]
        self.assertIn('alice_v2', emails)


if __name__ == '__main__':
    unittest.main()
