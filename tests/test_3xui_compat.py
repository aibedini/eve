import os
import json
import tempfile
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    XUI_CAPABILITY_CACHE,
    _json_field,
    _probe_v3_client_api,
    generate_client_link,
    server_is_v3,
    v3_attach_client,
)


class _Response:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else '')
        self.content = self.text.encode()
        self.headers = {'Content-Type': 'application/json' if payload is not None else 'text/html'}

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class _Session:
    def __init__(self, get_response=None, post_response=None):
        self.get_response = get_response
        self.post_response = post_response
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_response


class XuiCompatibilityTests(unittest.TestCase):
    def setUp(self):
        XUI_CAPABILITY_CACHE.clear()

    def tearDown(self):
        XUI_CAPABILITY_CACHE.clear()

    def test_cookie_authenticated_v3_is_detected_by_endpoint_capability(self):
        server = SimpleNamespace(id=4101, host='https://panel.example/base', api_token='')
        session = _Session(get_response=_Response(200, {
            'success': False,
            'msg': 'client not found',
            'obj': None,
        }))

        self.assertTrue(_probe_v3_client_api(server, session))
        self.assertTrue(server_is_v3(server))
        self.assertEqual(len(session.get_calls), 1)
        self.assertIn('/base/panel/api/clients/get/__eve_capability_probe__', session.get_calls[0][0])

    def test_legacy_html_or_missing_route_is_not_misdetected_as_v3(self):
        server = SimpleNamespace(id=4102, host='https://legacy.example', api_token='')
        session = _Session(get_response=_Response(404, None, '<html>not found</html>'))

        self.assertFalse(_probe_v3_client_api(server, session))
        self.assertFalse(server_is_v3(server))

    def test_invalid_bearer_response_does_not_override_capability_detection(self):
        server = SimpleNamespace(id=4104, host='https://panel.example', api_token='bad-token')
        session = _Session(get_response=_Response(401, {
            'success': False,
            'msg': 'unauthorized',
        }))

        self.assertFalse(server_is_v3(server, session, force_probe=True))

    def test_nested_and_string_inbound_json_both_remain_supported(self):
        value = {'clients': [{'email': 'new-panel'}]}
        self.assertEqual(_json_field(value), value)
        self.assertEqual(_json_field('{"clients":[{"email":"old-panel"}]}')['clients'][0]['email'],
                         'old-panel')

    def test_native_attach_uses_protocol_aware_v3_endpoint(self):
        server = SimpleNamespace(id=4103, host='https://panel.example/root', api_token='token')
        session = _Session(post_response=_Response(200, {'success': True, 'obj': {}}))

        ok, _payload, error = v3_attach_client(server, session, 'alice@example.com', [7, 9])

        self.assertTrue(ok, error)
        self.assertEqual(len(session.post_calls), 1)
        url, kwargs = session.post_calls[0]
        self.assertTrue(url.endswith('/root/panel/api/clients/alice%40example.com/attach'))
        self.assertEqual(kwargs['json'], {'inboundIds': [7, 9]})

    def test_wireguard_fallback_link_contains_generated_contract_fields(self):
        # RFC 7748-style 32-byte private material; the implementation derives
        # the server public key exactly as 3x-ui does when only secretKey exists.
        inbound = {
            'protocol': 'wireguard',
            'port': 51820,
            'remark': 'WG',
            'settings': {
                'secretKey': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
                'mtu': 1420,
                'dns': '1.1.1.1',
            },
            'streamSettings': {},
        }
        client = {
            'email': 'alice',
            'privateKey': 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=',
            'allowedIPs': ['10.0.0.2/32'],
            'preSharedKey': 'shared/key=',
            'keepAlive': 25,
        }

        link = generate_client_link(client, inbound, 'https://vpn.example:2053')

        parsed = urlparse(link)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, 'wireguard')
        self.assertEqual(parsed.hostname, 'vpn.example')
        self.assertEqual(parsed.port, 51820)
        self.assertEqual(query['address'], ['10.0.0.2/32'])
        self.assertEqual(query['mtu'], ['1420'])
        self.assertEqual(query['dns'], ['1.1.1.1'])
        self.assertEqual(query['presharedkey'], ['shared/key='])
        self.assertEqual(query['keepalive'], ['25'])
        self.assertTrue(query['publickey'][0])


if __name__ == '__main__':
    unittest.main()
