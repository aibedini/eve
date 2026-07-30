import base64
import json
import os
import tempfile
import unittest
from urllib.parse import quote
from unittest import mock


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'

import app as app_module  # noqa: E402
from app import Admin, GLOBAL_SERVER_DATA, Server, app, db  # noqa: E402


SUB_ID = 'live-subscription-token'
STALE_PASSWORD = 'stale-shadow-password'
LIVE_PASSWORD = 'current-shadow-password'
METHOD = 'chacha20-ietf-poly1305'


def _authoritative_link():
    userinfo = base64.b64encode(f'{METHOD}:{LIVE_PASSWORD}'.encode()).decode()
    remark = quote('navid-🇩🇪 Germany')
    return (
        f'ss://{userinfo}@edge-germany.example:15001'
        f'?security=none&type=tcp#{remark}'
    )


def _shadowsocks_inbound(password):
    client = {
        'email': 'shadow-user',
        'password': password,
        'subId': SUB_ID,
        'enable': True,
        'expiryTime': 0,
        'totalGB': 0,
    }
    return {
        'id': 42,
        'protocol': 'shadowsocks',
        'port': 8388,
        'settings': json.dumps({
            'method': METHOD,
            'clients': [client],
        }),
        'streamSettings': json.dumps({
            'network': 'tcp',
            'security': 'none',
        }),
        'clientStats': [{
            'email': client['email'],
            'up': 10,
            'down': 20,
        }],
    }


def _decode_subscription_body(response):
    return base64.b64decode(response.get_data(as_text=True)).decode('utf-8')


def _decode_ss_credentials(payload):
    ss_link = next(line for line in payload.splitlines() if line.startswith('ss://'))
    encoded_userinfo = ss_link[len('ss://'):].split('@', 1)[0]
    return base64.b64decode(encoded_userinfo).decode('utf-8')


class LiveSubscriptionContentTests(unittest.TestCase):
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
        try:
            os.unlink(_DB_FILE.name)
        except OSError:
            pass

    def setUp(self):
        Admin.query.delete()
        Server.query.delete()
        db.session.commit()

        self.admin = Admin(
            username='subscription-owner',
            password_hash='x',
            role='superadmin',
            is_superadmin=True,
        )
        self.server = Server(
            name='Live Shadowsocks',
            host='https://panel.example:8443',
            username='u',
            password='p',
            sub_path='/sub/',
            panel_type='v3',
        )
        db.session.add_all([self.admin, self.server])
        db.session.commit()

        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session['admin_id'] = self.admin.id

        self.previous_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        stale = _shadowsocks_inbound(STALE_PASSWORD)
        GLOBAL_SERVER_DATA['inbounds'] = [{
            **stale,
            'server_id': self.server.id,
            'clients': [{
                'email': 'shadow-user',
                'password': STALE_PASSWORD,
                'subId': SUB_ID,
            }],
        }]

    def tearDown(self):
        GLOBAL_SERVER_DATA['inbounds'] = self.previous_inbounds
        db.session.rollback()

    def _live_patches(self, fetch_result):
        return (
            mock.patch(
                'panel.routes.subscription_pages.get_xui_session',
                return_value=(object(), None),
            ),
            mock.patch(
                'panel.routes.subscription_pages.fetch_inbounds',
                return_value=fetch_result,
            ),
            mock.patch(
                'panel.routes.subscription_pages.persist_detected_panel_type',
            ),
            mock.patch(
                'panel.services.subscription.server_is_v3',
                return_value=True,
            ),
            mock.patch(
                'panel.services.subscription._v3_get',
                return_value=(True, {
                    'obj': [_authoritative_link()],
                }, None),
            ),
        )

    def test_public_subscription_uses_live_shadowsocks_password(self):
        patches = self._live_patches(([_shadowsocks_inbound(LIVE_PASSWORD)], None, 'legacy'))
        with patches[0], patches[1] as fetch, patches[2], patches[3], patches[4] as v3_get:
            response = self.client.get(
                f'/s/{self.server.id}/{SUB_ID}',
                headers={'User-Agent': 'v2rayng', 'Accept': '*/*'},
            )

        self.assertEqual(response.status_code, 200)
        payload = _decode_subscription_body(response)
        self.assertEqual(
            _decode_ss_credentials(payload),
            f'{METHOD}:{LIVE_PASSWORD}',
        )
        self.assertNotIn(STALE_PASSWORD, payload)
        self.assertIn('edge-germany.example:15001', payload)
        self.assertIn(quote('navid-🇩🇪 Germany'), payload)
        self.assertEqual(fetch.call_count, 1)
        v3_get.assert_called_once()
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))

    def test_live_fetch_failure_never_falls_back_to_stale_content(self):
        patches = self._live_patches(([], 'panel unavailable', 'legacy'))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = self.client.get(
                f'/s/{self.server.id}/{SUB_ID}',
                headers={'User-Agent': 'v2rayng', 'Accept': '*/*'},
            )

        self.assertEqual(response.status_code, 502)
        self.assertNotIn(STALE_PASSWORD.encode(), response.data)

    def test_direct_link_api_is_built_from_the_same_live_read(self):
        patches = self._live_patches(([_shadowsocks_inbound(LIVE_PASSWORD)], None, 'legacy'))
        with patches[0], patches[1] as fetch, patches[2], patches[3], patches[4] as v3_get:
            response = self.client.get(
                f'/api/client/direct-link/{self.server.id}/{SUB_ID}',
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(
            _decode_ss_credentials(payload['configs'][0]),
            f'{METHOD}:{LIVE_PASSWORD}',
        )
        self.assertEqual(payload['configs'][0], _authoritative_link())
        self.assertEqual(fetch.call_count, 1)
        v3_get.assert_called_once()
