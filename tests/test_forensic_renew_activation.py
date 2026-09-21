"""The activation forensic tool must name the divergent layer, not guess.

Its whole value is that an operator reads one line and knows which layer to fix, so
the classifications are asserted here against synthetic panels. It is also a
read-only tool: the tests assert that its client view carries no credential.
"""
import base64
import os
import tempfile
import unittest
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
from panel.adapters import xui as xui_adapter  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.models import Server  # noqa: E402
from panel.services import panel_capabilities  # noqa: E402
from scripts import forensic_renew_activation as forensic  # noqa: E402

GB = 1024 ** 3
EMAIL = 'renewed@example.com'
EXPIRY = 2_000_000_000_000


def _client(*, enable=True, expiry=EXPIRY, total=20 * GB):
    return {'email': EMAIL, 'id': 'uuid-x', 'enable': enable, 'expiryTime': expiry,
            'totalGB': total, 'up': 0, 'down': 0}


def _inbound(inbound_id, *, enable=True):
    return {'id': inbound_id, 'settings': {'clients': [dict(_client(enable=enable))]}}


def _caps(family=panel_capabilities.CLIENT_API_FIRST_CLASS):
    return panel_capabilities.PanelClientCapabilities(
        client_api_family=family,
        client_get=family == panel_capabilities.CLIENT_API_FIRST_CLASS,
        client_update=family == panel_capabilities.CLIENT_API_FIRST_CLASS,
        client_traffic=family == panel_capabilities.CLIENT_API_FIRST_CLASS,
        client_reset_traffic=family == panel_capabilities.CLIENT_API_FIRST_CLASS,
        version='3.8.5', version_family=(3, 8), profile='xui_3_8',
        probe_state=panel_capabilities.PROBE_SUPPORTED)


class ActivationForensicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        Server.query.delete()
        db.session.commit()
        self.server = Server(name='forensic', host='https://panel.example:8443/base',
                             username='u', password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()

    def _run(self, *, client, inbounds, traffic=None, caps=None, caps_reason=None,
             blocked=False):
        patches = [
            mock.patch.object(xui_adapter, 'get_xui_session',
                              return_value=(mock.Mock(), None)),
            mock.patch.object(xui_adapter, 'fetch_inbounds',
                              return_value=(inbounds, None, '3x-ui')),
            mock.patch.object(xui_adapter, 'v3_get_client_details',
                              return_value={'ok': client is not None, 'client': client,
                                            'inbound_ids': [row['id'] for row in inbounds
                                                            if row['id'] != 99],
                                            'raw': None, 'error': None}),
            mock.patch.object(xui_adapter, 'v3_client_traffic',
                              return_value=traffic or {'available': False,
                                                       'reason': 'not modelled'}),
        ]
        if blocked:
            patches.append(mock.patch.object(
                panel_capabilities, 'capabilities_for',
                return_value=(panel_capabilities.blocked_capabilities(
                    probe_state='AUTH_INVALID', reason='the panel rejected the credential'),
                    'the panel rejected the credential')))
        else:
            patches.append(mock.patch.object(
                panel_capabilities, 'capabilities_for',
                return_value=(caps or _caps(), caps_reason)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return forensic.collect(self.server.id, EMAIL)

    def test_a_globally_disabled_client_is_named(self):
        report = self._run(client=_client(enable=False), inbounds=[_inbound(1)])
        self.assertEqual(report['classification'], 'GLOBAL_DISABLED')

    def test_a_disabled_membership_is_named_with_its_inbound(self):
        report = self._run(client=_client(enable=True),
                           inbounds=[_inbound(1), _inbound(24, enable=False)])
        self.assertEqual(report['classification'], 'MEMBERSHIP_DIVERGENCE')
        self.assertIn('24', report['detail'])

    def test_a_missing_membership_is_named(self):
        report = self._run(
            client=_client(enable=True),
            inbounds=[{'id': 1, 'settings': {'clients': []}},
                      {'id': 24, 'settings': {'clients': []}}])
        self.assertEqual(report['classification'], 'MEMBERSHIP_DIVERGENCE')
        self.assertIn('absent', report['detail'])

    def test_a_traffic_row_that_still_says_disabled_is_named(self):
        report = self._run(client=_client(enable=True), inbounds=[_inbound(1)],
                           traffic={'available': True, 'enable': False, 'up': 0,
                                    'down': 0, 'total': 20 * GB, 'expiry': EXPIRY})
        self.assertEqual(report['classification'], 'TRAFFIC_STATE_DIVERGENCE')

    def test_a_converged_account_is_reported_as_such_not_as_a_failure(self):
        report = self._run(client=_client(enable=True), inbounds=[_inbound(1)],
                           traffic={'available': True, 'enable': True, 'up': 0,
                                    'down': 0, 'total': 20 * GB, 'expiry': EXPIRY})
        self.assertEqual(report['classification'], 'UNKNOWN')
        self.assertIn('active across every layer', report['detail'])

    def test_an_unclassifiable_panel_is_auth_degraded(self):
        report = self._run(client=None, inbounds=[], blocked=True)
        self.assertEqual(report['classification'], 'AUTH_DEGRADED')
        self.assertIn('credential', report['detail'])

    def test_credentials_are_never_exposed(self):
        report = self._run(
            client=dict(_client(), subId='secret-sub', password='pw', uuid='uuid-x'),
            inbounds=[_inbound(1)])
        view = report['client_record']
        self.assertEqual(set(view), {'email', 'enable', 'expiryTime', 'totalGB',
                                     'up', 'down', 'limitHwid'})
        text = str(report).lower()
        for secret in ('secret-sub', 'password', 'subid', 'uuid-x'):
            self.assertNotIn(secret, text)

    def test_the_classification_set_is_fixed(self):
        self.assertEqual(len(forensic.CLASSIFICATIONS),
                         len(set(forensic.CLASSIFICATIONS)))
        for name in ('LEGACY_STATE_DIVERGENCE', 'GLOBAL_DISABLED',
                     'MEMBERSHIP_DIVERGENCE', 'TRAFFIC_STATE_DIVERGENCE',
                     'NODE_PENDING', 'EVE_SNAPSHOT_STALE', 'PARTIAL_RENEW',
                     'AUTH_DEGRADED', 'UNKNOWN'):
            self.assertIn(name, forensic.CLASSIFICATIONS)


if __name__ == '__main__':
    unittest.main()
