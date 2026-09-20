"""Normalized cached mutations preserve shared entity identity."""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import app  # noqa: E402
from panel.core.redis_client import GLOBAL_SERVER_DATA  # noqa: E402
from panel.core import snapshot_model  # noqa: E402
from panel.jobs import refresh  # noqa: E402


class NormalizedMutationTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(GLOBAL_SERVER_DATA)
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({
            'inbounds': [
                {'server_id': 7, 'id': 10, 'clients': []},
                {'server_id': 7, 'id': 20, 'clients': []},
            ],
            'servers_status': [], 'stats': {}, 'last_update': None,
            'normalized_server_ids': {7},
            'normalized_indexes': {7: {'entities': {}, 'memberships': {}}},
        })
        self.ctx = app.app_context()
        self.ctx.push()
        self.addCleanup(self.restore)

    def restore(self):
        self.ctx.pop()
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self.saved)

    def test_add_uses_one_entity_for_all_memberships(self):
        raw = {'id': '4ce7db6e-4576-4e55-bb2a-452487fe1bb6',
               'email': 'a@x', 'enable': True, 'totalGB': 0, 'expiryTime': 0}
        with mock.patch.object(refresh, '_recompute_cached_client'), \
                mock.patch.object(refresh, '_recompute_cached_server_stats'), \
                mock.patch('app._get_dashboard_status_thresholds', return_value={}), \
                mock.patch('app._get_panel_ui_lang', return_value='en'):
            self.assertTrue(refresh.add_cached_client(7, [10, 20], raw, publish=False))
        rows = GLOBAL_SERVER_DATA['inbounds']
        self.assertIs(rows[0]['clients'][0], rows[1]['clients'][0])
        self.assertNotIn('inbound_id', rows[0]['clients'][0])
        class NoFleetScan(list):
            def __iter__(self):
                raise AssertionError('normalized lookup must use the entity index')

        GLOBAL_SERVER_DATA['inbounds'] = NoFleetScan(rows)
        copies = list(refresh._iter_cached_client_copies(7, 'a@x', raw['id']))
        GLOBAL_SERVER_DATA['inbounds'] = rows
        self.assertEqual(len(copies), 2)

    def test_clone_adds_a_membership_reference_not_a_deepcopy(self):
        entity = {'id': '4ce7db6e-4576-4e55-bb2a-452487fe1bb6', 'email': 'a@x'}
        GLOBAL_SERVER_DATA['inbounds'][0]['clients'] = [entity]
        GLOBAL_SERVER_DATA['normalized_indexes'][7] = snapshot_model.build_retained_index(
            GLOBAL_SERVER_DATA['inbounds'])
        with mock.patch.object(refresh, '_recompute_cached_server_stats'):
            self.assertTrue(refresh.clone_cached_client_into_inbound(
                7, 20, 'a@x', publish=False))
        self.assertIs(GLOBAL_SERVER_DATA['inbounds'][1]['clients'][0], entity)


if __name__ == '__main__':
    unittest.main()
