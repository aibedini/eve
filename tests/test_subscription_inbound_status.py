"""Public inbound status must include only the subscriber's assigned inbounds."""
import base64
import os
import tempfile
import unittest
from unittest import mock

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode(),
)
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from panel.routes import subscription_pages  # noqa: E402

_assigned_inbound_statuses = subscription_pages._assigned_inbound_statuses


class AssignedInboundStatusesTests(unittest.TestCase):
    def test_only_matching_subscription_receives_inbound_names(self):
        inbounds = [
            {'id': 1, 'remark': 'DE main', 'enable': True,
             'settings': {'clients': [{'subId': 'mine'}, {'subId': 'other'}]}},
            {'id': 2, 'remark': 'US private', 'enable': True,
             'settings': {'clients': [{'subId': 'other'}]}},
            {'id': 3, 'remark': 'FR backup', 'enable': False,
             'settings': '{"clients":[{"subId":"mine"}]}'},
            {'id': 4, 'remark': 'Legacy', 'enable': True,
             'settings': {'clients': [{'id': 'mine'}]}},
            {'id': 5, 'remark': 'Mirrored but not assigned', 'enable': True,
             'settings': {'clients': [{'subId': 'mine', 'inboundIds': [1, 3]}]}},
            {'id': 6, 'remark': 'Mirrored with empty assignment', 'enable': True,
             'settings': {'clients': [{'subId': 'mine', 'inboundIds': []}]}},
        ]
        result = _assigned_inbound_statuses(inbounds, 'mine')
        self.assertEqual([item['id'] for item in result], [1, 3, 4])
        self.assertEqual([item['state'] for item in result], ['active', 'disabled', 'active'])
        self.assertNotIn('US private', [item['name'] for item in result])

    def test_public_endpoint_scopes_results_to_subscription(self):
        inbounds = [
            {'id': 1, 'remark': 'My inbound', 'enable': True,
             'settings': {'clients': [{'subId': 'mine'}]}},
            {'id': 2, 'remark': 'Someone else', 'enable': True,
             'settings': {'clients': [{'subId': 'other'}]}},
        ]
        with app_module.app.test_client() as client, \
                mock.patch.object(subscription_pages.db.session, 'get', return_value=object()), \
                mock.patch.object(subscription_pages, '_fetch_live_subscription_context',
                                  return_value=(None, inbounds, {'subId': 'mine'}, inbounds[0], None)):
            response = client.get('/s/1/mine/inbound-status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['name'] for item in response.json['inbounds']], ['My inbound'])
        self.assertIn('no-store', response.headers['Cache-Control'])


if __name__ == '__main__':
    unittest.main()
