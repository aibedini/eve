"""The transport-health ROUTE: a failed probe must name its own failure.

These pin the six "Unknown" delivery cards. The chain was: GMweb had no
/eve/v1/transport-health, the consumer got a 404, the frontend collapsed every
failure into a generic "Unknown", and a missing endpoint became indistinguishable
from a dead host or a rejected key.

They also pin the two defects the route itself carried: it hard-coded the path
and sent X-API-Key where every other GMweb call sends Authorization: Bearer, and
its hand-written field list asked for device.age_ms so device.last_seen_age_ms was
silently dropped.

The valid case is driven by the SHARED fixture (shared/eve-gmweb-contract-v1.json)
- the same samples GMweb asserts against - so provider and consumer cannot drift.
"""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL',
                      'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.models import Admin  # noqa: E402
from panel.services import gmweb_contract  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "shared" / "eve-gmweb-contract-v1.json").read_text(
    encoding="utf-8"))
SAMPLE = CONTRACT["transportHealthResponse"]["samples"]["android_pull_connected"]

API_KEY = 'gmw_route_test_key'


def _response(status_code, payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.content = b'{}'
    response.json.return_value = payload if payload is not None else {}
    return response


class TransportHealthRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username='transport-health-admin', password_hash='x',
                          role='superadmin', is_superadmin=True, enabled=True)
        db.session.add(cls.admin)
        db.session.commit()
        cls.client = app_module.app.test_client()
        with cls.client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = cls.admin.id
            sess['role'] = 'superadmin'
            sess['is_superadmin'] = True

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    # ── driver ────────────────────────────────────────────────────────────────

    def _call(self, *, base_url='https://gw.example.com', api_key=API_KEY,
              response=None, side_effect=None):
        from panel.routes import messaging as routes
        settings = {'base_url': base_url, 'api_key': api_key, 'timeout_seconds': 5}
        patches = [
            mock.patch.object(app_module, '_get_sms_runtime_settings',
                              return_value=settings),
            # Transport verification is an environment concern, not the subject.
            mock.patch.object(routes, 'outbound_tls_verify', lambda *a, **k: True),
        ]
        if side_effect is not None:
            patches.append(mock.patch.object(routes.requests, 'get',
                                             side_effect=side_effect))
        else:
            patches.append(mock.patch.object(routes.requests, 'get',
                                             return_value=response))
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        result = self.client.get('/api/sms/transport-health')
        self.request_mock = started[-1]
        return result

    def _body(self, response):
        return json.loads(response.data.decode('utf-8'))

    # ── case 3: GMweb down ────────────────────────────────────────────────────

    def test_gmweb_down_is_reported_as_unreachable(self):
        response = self._call(side_effect=requests.ConnectionError('refused'))
        self.assertEqual(response.status_code, 502)
        body = self._body(response)
        self.assertFalse(body['success'])
        self.assertEqual(body['probe_state'], 'gmweb_unreachable')
        self.assertTrue(body['diagnostic'])
        self.assertNotEqual(body['probe_state'], 'unknown')

    def test_a_timeout_is_reported_as_unreachable(self):
        response = self._call(side_effect=requests.Timeout('slow'))
        body = self._body(response)
        self.assertEqual(body['probe_state'], 'gmweb_unreachable')

    # ── case 4: route missing / old GMweb ─────────────────────────────────────

    def test_a_missing_route_is_reported_as_a_missing_contract(self):
        response = self._call(response=_response(404))
        self.assertEqual(response.status_code, 502)
        body = self._body(response)
        self.assertEqual(body['probe_state'], 'contract_missing')
        # It must be actionable, not a generic "Unknown".
        self.assertNotEqual(body['probe_state'], 'unknown')
        self.assertIn('upgrade', body['diagnostic'].lower())
        self.assertEqual(body['http_status'], 404)

    # ── cases 5 and 6: credential and scope ───────────────────────────────────

    def test_401_is_reported_as_an_authentication_failure(self):
        body = self._body(self._call(response=_response(401)))
        self.assertEqual(body['probe_state'], 'auth_failed')
        self.assertIn('key', body['diagnostic'].lower())

    def test_403_is_reported_as_a_scope_denial(self):
        body = self._body(self._call(response=_response(403)))
        self.assertEqual(body['probe_state'], 'scope_denied')
        self.assertIn('transport:read', body['diagnostic'])

    def test_a_5xx_is_reported_as_unreachable(self):
        body = self._body(self._call(response=_response(503)))
        self.assertEqual(body['probe_state'], 'gmweb_unreachable')

    def test_the_failure_verdicts_are_all_distinct(self):
        states = set()
        for status in (401, 403, 404, 500):
            states.add(self._body(self._call(response=_response(status)))['probe_state'])
        self.assertEqual(len(states), 4)

    # ── versions and unusable bodies ──────────────────────────────────────────

    def test_a_version_mismatch_is_reported_as_such(self):
        stale = dict(SAMPLE)
        stale['contract_version'] = 99
        body = self._body(self._call(response=_response(200, stale)))
        self.assertEqual(body['probe_state'], 'contract_version_mismatch')
        self.assertFalse(body['contract_supported'])
        self.assertEqual(body['contract_version'], 99)

    def test_a_body_without_readiness_is_invalid_not_connected(self):
        body = self._body(self._call(response=_response(200, {'contract_version': 1})))
        self.assertEqual(body['probe_state'], 'invalid_response')

    def test_a_non_json_body_is_invalid(self):
        response = _response(200)
        response.json.side_effect = ValueError('not json')
        body = self._body(self._call(response=response))
        self.assertEqual(body['probe_state'], 'invalid_response')

    def test_an_unconfigured_gateway_is_reported_as_not_configured(self):
        response = self._call(base_url='', api_key='', response=_response(200, SAMPLE))
        body = self._body(response)
        self.assertEqual(body['probe_state'], 'gmweb_not_configured')
        self.assertFalse(body['success'])
        self.assertTrue(body['diagnostic'])
        # "Not configured" is a BUSINESS error, so the app-wide CDN workaround
        # (app.py after_request) passes the JSON body through as 200 and keeps the
        # real code in X-Eve-Status. Asserting 400 here would be asserting against
        # deliberate, documented behaviour.
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')

    def test_a_gateway_failure_is_not_downgraded_by_the_cdn_workaround(self):
        """401/403/404/5xx are explicitly untouched, so an operator and the CDN
        both see the failure - and the body still names it."""
        response = self._call(side_effect=requests.ConnectionError('refused'))
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(response.headers.get('X-Eve-Status'))
        self.assertEqual(self._body(response)['probe_state'], 'gmweb_unreachable')

    # ── the success path, driven by the shared fixture ────────────────────────

    def test_a_valid_contract_is_returned_with_every_declared_field(self):
        response = self._call(response=_response(200, SAMPLE))
        self.assertEqual(response.status_code, 200, response.data)
        body = self._body(response)
        self.assertTrue(body['success'])
        self.assertEqual(body['probe_state'], 'connected')
        self.assertEqual(body['contract_version'], 1)
        self.assertTrue(body['contract_supported'])
        self.assertIsNone(body.get('diagnostic'))
        declared = gmweb_contract.transport_health_sections()
        for section, fields in declared.items():
            self.assertIn(section, body['health'], section)
            for field in fields:
                self.assertIn(field, body['health'][section], f'{section}.{field}')

    def test_the_device_age_field_is_not_dropped(self):
        """The hand-written list asked for age_ms and lost last_seen_age_ms."""
        body = self._body(self._call(response=_response(200, SAMPLE)))
        device = body['health']['device']
        self.assertEqual(device['last_seen_age_ms'], SAMPLE['device']['last_seen_age_ms'])
        self.assertEqual(device['age_ms'], SAMPLE['device']['age_ms'])

    # ── the request itself ────────────────────────────────────────────────────

    def test_the_route_resolves_the_path_and_sends_the_bearer_header(self):
        self._call(response=_response(200, SAMPLE))
        args, kwargs = self.request_mock.call_args
        self.assertTrue(args[0].endswith(gmweb_contract.endpoint_path('transport_health')))
        headers = kwargs['headers']
        self.assertEqual(headers.get('Authorization'), 'Bearer %s' % API_KEY)
        self.assertNotIn('X-API-Key', headers)
        self.assertEqual(kwargs['verify'], True)

    def test_the_response_is_never_cached(self):
        response = self._call(response=_response(200, SAMPLE))
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')


if __name__ == '__main__':
    unittest.main()
