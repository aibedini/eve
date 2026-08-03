import base64
import json
import os
import tempfile
import unittest
from urllib.parse import quote, unquote
from unittest import mock


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'

import app as app_module  # noqa: E402
from app import Admin, GLOBAL_SERVER_DATA, Server, SystemConfig, app, db  # noqa: E402
from panel.routes.subscription_pages import _build_subscription_statistics_values  # noqa: E402
from panel.services import subscription as subscription_service  # noqa: E402


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
        db.session.remove()
        subscription_service.SUBSCRIPTION_PROFILE_CACHE.clear()
        Admin.query.delete()
        Server.query.delete()
        SystemConfig.query.delete()
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
        self.assertNotIn('vmess://', payload)
        lines = payload.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith('ss://') for line in lines))
        self.assertTrue(all(
            base64.b64decode(line[len('ss://'):].split('@', 1)[0]).decode('utf-8')
            == f'{METHOD}:{LIVE_PASSWORD}'
            for line in lines
        ))
        self.assertIn('No%20renewal%20needed', lines[-1])
        self.assertEqual(fetch.call_count, 1)
        v3_get.assert_called_once()
        self.assertEqual(v3_get.call_args.kwargs['timeout'], (3, 8))
        self.assertIn('Subscription-Userinfo', response.headers)
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))

    def test_live_fetch_failure_never_falls_back_to_stale_content(self):
        patches = self._live_patches(([], 'panel unavailable', 'legacy'))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = self.client.get(
                f'/s/{self.server.id}/{SUB_ID}',
                headers={'User-Agent': 'clash', 'Accept': '*/*'},
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
        self.assertTrue(payload['configs'][0].endswith('-shadow-user'))
        self.assertEqual(fetch.call_count, 1)
        v3_get.assert_called_once()

    def test_profile_title_combines_panel_title_and_eve_server_name(self):
        patches = self._live_patches(([_shadowsocks_inbound(LIVE_PASSWORD)], None, 'legacy'))
        settings_payload = {
            'success': True,
            'obj': {'subTitle': 'VPN Mahna', 'subUpdates': 6},
        }
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            mock.patch(
                'panel.services.subscription._v3_post',
                return_value=(True, settings_payload, None),
            ),
        ):
            response = self.client.get(
                f'/s/{self.server.id}/{SUB_ID}',
                headers={'User-Agent': 'v2rayNG/1.10.27', 'Accept': '*/*'},
            )

        encoded_title = response.headers['Profile-Title'].removeprefix('base64:')
        title = base64.b64decode(encoded_title).decode('utf-8')
        self.assertEqual(title, 'VPN Mahna\nLive Shadowsocks')
        self.assertEqual(response.headers['Profile-Update-Interval'], '6')
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_vmess_identity_is_added_inside_ps_without_changing_credentials(self):
        vmess = {
            'v': '2',
            'ps': 'Germany',
            'add': 'edge.example',
            'port': '443',
            'id': '00000000-1111-2222-3333-444444444444',
            'net': 'tcp',
        }
        link = 'vmess://' + base64.b64encode(
            json.dumps(vmess).encode('utf-8')
        ).decode('ascii')

        result = subscription_service.ensure_subscription_identity(
            [link],
            'shadow-user',
        )[0]
        decoded = json.loads(base64.b64decode(result.removeprefix('vmess://')))
        self.assertEqual(decoded['ps'], 'Germany-shadow-user')
        self.assertEqual(decoded['id'], vmess['id'])

    def test_statistics_config_clones_real_vmess_credentials(self):
        vmess = {
            'v': '2',
            'ps': 'Germany',
            'add': 'edge.example',
            'port': '443',
            'id': '00000000-1111-2222-3333-444444444444',
            'net': 'ws',
            'path': '/socket',
            'tls': 'tls',
        }
        link = 'vmess://' + base64.b64encode(
            json.dumps(vmess).encode('utf-8')
        ).decode('ascii')

        cloned = subscription_service.clone_subscription_config_with_name(
            [link], 'Account is active',
        )
        decoded = json.loads(base64.b64decode(cloned.removeprefix('vmess://')))
        self.assertEqual(decoded['ps'], 'Account is active')
        self.assertEqual(decoded['id'], vmess['id'])
        self.assertEqual(decoded['add'], vmess['add'])
        self.assertEqual(decoded['path'], vmess['path'])

    def test_statistics_values_describe_start_after_first_connection(self):
        values = _build_subscription_statistics_values(
            {'key': 'active', 'label': 'Active', 'emoji': '✅'},
            {'type': 'start_after_use', 'days': 31, 'text': 'Not started (31 days)'},
            20 * 1024 ** 3,
            30 * 1024 ** 3,
            10 * 1024 ** 3,
            'shadow-user',
            'en',
        )
        self.assertEqual(values['days'], '31 days')
        self.assertEqual(values['expiry_type'], 'After first connection')
        self.assertEqual(values['volume'], '20 GB remaining')
        self.assertEqual(values['renewal'], 'No renewal needed')

    def test_custom_statistics_template_is_app_only_and_can_be_disabled(self):
        saved = self.client.put('/api/subscription-statistics', json={
            'enabled': True,
            'template_fa': 'وضعیت {status}',
            'template_en': 'ACCOUNT {email} | {expiry_type}',
        })
        self.assertEqual(saved.status_code, 200, saved.get_json())

        patches = self._live_patches(([_shadowsocks_inbound(LIVE_PASSWORD)], None, 'legacy'))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = self.client.get(
                f'/s/{self.server.id}/{SUB_ID}',
                headers={'User-Agent': 'v2rayng', 'Accept': '*/*'},
            )
        lines = _decode_subscription_body(response).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('ACCOUNT shadow-user | Unlimited', unquote(lines[-1]))
        self.assertEqual(
            base64.b64decode(lines[-1][len('ss://'):].split('@', 1)[0]).decode(),
            f'{METHOD}:{LIVE_PASSWORD}',
        )

        patches = self._live_patches(([_shadowsocks_inbound(LIVE_PASSWORD)], None, 'legacy'))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            html_response = self.client.get(
                f'/s/{self.server.id}/{SUB_ID}?view=1',
                headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html'},
            )
        self.assertEqual(html_response.status_code, 200)
        self.assertNotIn('ACCOUNT shadow-user', html_response.get_data(as_text=True))

        disabled = self.client.put('/api/subscription-statistics', json={
            'enabled': False,
            'template_fa': 'وضعیت {status}',
            'template_en': 'ACCOUNT {email}',
        })
        self.assertEqual(disabled.status_code, 200, disabled.get_json())
        patches = self._live_patches(([_shadowsocks_inbound(LIVE_PASSWORD)], None, 'legacy'))
        with patches[0], patches[1] as fetch, patches[2], patches[3], patches[4]:
            response = self.client.get(
                f'/s/{self.server.id}/{SUB_ID}',
                headers={'User-Agent': 'v2rayng', 'Accept': '*/*'},
            )
        self.assertEqual(len(_decode_subscription_body(response).splitlines()), 1)
        self.assertEqual(fetch.call_count, 0)

    def test_statistics_template_rejects_unknown_variables(self):
        response = self.client.put('/api/subscription-statistics', json={
            'enabled': True,
            'template_fa': '{unknown}',
            'template_en': '{status}',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')
        self.assertFalse(response.get_json()['success'])
        self.assertIn('Unknown statistics variable', response.get_json()['error'])

    def test_sub_manager_renders_statistics_editor(self):
        response = self.client.get('/sub-manager')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="tab-statistics"', html)
        self.assertIn('id="statistics-template-fa"', html)
        self.assertIn('saveSubscriptionStatistics()', html)
        self.assertIn('remaining_volume', html)

    def test_subscription_sort_skips_unassigned_priority_and_keeps_user_links_ordered(self):
        self.server.subscription_inbound_order = json.dumps([99, 7, 42])
        db.session.commit()
        inbounds = [
            {'id': 42, 'server_id': self.server.id, 'protocol': 'shadowsocks', 'port': 15001, 'remark': 'Germany'},
            {'id': 7, 'server_id': self.server.id, 'protocol': 'vless', 'port': 443, 'remark': 'Turkey'},
            {'id': 99, 'server_id': self.server.id, 'protocol': 'trojan', 'port': 8443, 'remark': 'USA'},
        ]
        # This client is only present on inbounds 7 and 42. Inbound 99 has the
        # highest configured priority but must not create a link for the user.
        links = [
            _authoritative_link(),
            'vless://00000000-1111-2222-3333-444444444444@edge.example:443?type=tcp#Turkey',
        ]

        result = subscription_service.sort_subscription_configs(
            links,
            self.server,
            inbounds=inbounds,
        )

        self.assertTrue(result[0].startswith('vless://'))
        self.assertTrue(result[1].startswith('ss://'))
        self.assertEqual(len(result), 2)

    def test_server_subscription_order_api_persists_normalized_inbound_ids(self):
        GLOBAL_SERVER_DATA['inbounds'] = [
            {'id': 42, 'server_id': self.server.id, 'protocol': 'shadowsocks', 'port': 15001, 'remark': 'Germany', 'enable': True},
            {'id': 7, 'server_id': self.server.id, 'protocol': 'vless', 'port': 443, 'remark': 'Turkey', 'enable': True},
            {'id': 99, 'server_id': self.server.id, 'protocol': 'trojan', 'port': 8443, 'remark': 'USA', 'enable': False},
        ]

        response = self.client.put(
            f'/api/servers/{self.server.id}/subscription-order',
            json={'inbound_ids': [99, 7, 42]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['inbound_ids'], [99, 7, 42])

        db.session.refresh(self.server)
        self.assertEqual(json.loads(self.server.subscription_inbound_order), [99, 7, 42])

        response = self.client.get(
            f'/api/servers/{self.server.id}/subscription-order',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['inbound_ids'], [99, 7, 42])
        self.assertEqual([row['id'] for row in payload['inbounds']], [99, 7, 42])

    def test_subscription_sort_uses_subid_membership_when_public_ports_differ(self):
        self.server.subscription_inbound_order = json.dumps([7, 42])
        db.session.commit()
        inbounds = [
            {
                'id': 42,
                'server_id': self.server.id,
                'protocol': 'shadowsocks',
                'port': 15001,
                'remark': '',
                'clients': [{'subId': SUB_ID, 'email': 'shadow-user'}],
            },
            {
                'id': 7,
                'server_id': self.server.id,
                'protocol': 'vless',
                'port': 443,
                'remark': '',
                'clients': [{'subId': SUB_ID, 'email': 'shadow-user'}],
            },
        ]
        links = [
            'ss://YWVzLTI1Ni1nY206cGFzcw==@public.example:20001#Custom-A',
            'vless://00000000-1111-2222-3333-444444444444@public.example:2443?type=tcp#Custom-B',
        ]

        result = subscription_service.sort_subscription_configs(
            links,
            self.server,
            inbounds=inbounds,
            sub_id=SUB_ID,
        )

        self.assertTrue(result[0].startswith('vless://'))
        self.assertTrue(result[1].startswith('ss://'))
