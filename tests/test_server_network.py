"""Network rates stay distinct from byte counters and stale/unauthorized data."""
import base64
import os
import tempfile
import unittest
from unittest.mock import patch, Mock

_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_db.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _db.name.replace(os.sep, '/'))
os.environ.setdefault('SESSION_SECRET', 'server-network-test')
os.environ.setdefault('SERVER_PASSWORD_KEY', base64.urlsafe_b64encode(b'0' * 32).decode())
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
os.environ['FLASK_ENV'] = 'development'

from panel.adapters.xui import _normalize_server_status_payload
from panel.services import server_network as network


class NetworkRatesTests(unittest.TestCase):
    def setUp(self):
        network._cache.clear()
        network._inflight.clear()
        network._scheduled.clear()
        self.redis = patch.object(network, 'get_redis', return_value=None)
        self.redis.start()
        self.addCleanup(self.redis.stop)

    def test_netio_is_bytes_per_second_and_not_cumulative_traffic(self):
        result = _normalize_server_status_payload({
            'netIO': {'up': 10485760, 'down': 0},
            'netTraffic': {'sent': 999999999, 'recv': 888888888},
        })
        self.assertEqual(result['network_up_bps'], 10485760)
        self.assertEqual(result['network_down_bps'], 0)
        self.assertIsNone(_normalize_server_status_payload({'netTraffic': {'sent': 4}})['network_up_bps'])

    def test_invalid_rates_are_unknown_and_zero_is_valid(self):
        for value in (None, '123', True, -1, float('nan'), float('inf'), 10**1000):
            with self.subTest(value=str(value)[:20]):
                result = _normalize_server_status_payload({'netIO': {'up': value, 'down': 0}})
                self.assertIsNone(result['network_up_bps'])
                self.assertEqual(result['network_down_bps'], 0)

    def test_viewers_share_sample_and_failure_does_not_turn_into_zero(self):
        with patch.object(network, '_read_server', return_value=(50, 70, None)) as read:
            with patch.object(network.time, 'time', return_value=100):
                first = network._sample(1, None)
                self.assertEqual(network._sample(1, None), first)
            self.assertEqual(read.call_count, 1)
        with patch.object(network, '_read_server', side_effect=RuntimeError('secret must not leak')):
            with patch.object(network.time, 'time', return_value=106):
                failed = network._sample(1, None)
        self.assertEqual(failed['sampled_at'], 100)
        self.assertEqual(failed['upload_bps'], 50)
        self.assertEqual(failed['error'], 'connection')

    def test_totals_include_only_fresh_rows_and_zero_counts_as_reporting(self):
        network._cache.update({
            1: dict(server_id=1, checked_at=100, sampled_at=100, upload_bps=0, download_bps=80, error=None),
            2: dict(server_id=2, checked_at=100, sampled_at=50, upload_bps=900, download_bps=900, error=None),
            3: dict(server_id=3, checked_at=100, sampled_at=100, upload_bps=900, download_bps=900, error='connection'),
        })
        with patch.object(network.time, 'time', return_value=102):
            result = network.collect_network([1, 2, 3], None)
        self.assertEqual(result['totals'], dict(upload_bps=0, download_bps=80, reporting=1, expected=3))
        self.assertFalse(result['servers'][1]['fresh'])
        self.assertFalse(result['servers'][2]['fresh'])

    def test_initial_response_does_not_wait_for_panels_and_bounds_queue(self):
        with patch.object(network._pool, 'submit', return_value=Mock(done=lambda: False)) as submit:
            with patch.object(network.time, 'time', return_value=100):
                first = network.collect_network(list(range(20)), None)
                network.collect_network(list(range(20)), None)
        self.assertEqual(submit.call_count, 8)
        self.assertIsNone(first['totals']['upload_bps'])
        self.assertEqual(first['totals']['expected'], 20)

    def test_redis_sample_is_reused_across_workers(self):
        import json
        sample = dict(server_id=1, checked_at=100, sampled_at=100, upload_bps=4, download_bps=5, error=None)
        redis = Mock()
        redis.get.return_value = json.dumps(sample)
        with patch.object(network, 'get_redis', return_value=redis), patch.object(network.time, 'time', return_value=102):
            with patch.object(network, '_read_server') as read:
                self.assertEqual(network._sample(1, None), sample)
                read.assert_not_called()

    def test_another_worker_owns_the_probe_and_is_not_duplicated(self):
        redis = Mock()
        redis.get.return_value = None
        redis.set.return_value = False
        with patch.object(network, 'get_redis', return_value=redis):
            with patch.object(network, '_read_server') as read:
                self.assertEqual(network._sample(1, None)['error'], 'pending')
                read.assert_not_called()
                redis.eval.assert_not_called()
        self.assertNotIn(1, network._inflight)

    def test_missing_netio_is_unavailable_not_a_live_zero(self):
        with patch.object(network, '_read_server', return_value=(None, None, 'unsupported')):
            with patch.object(network.time, 'time', return_value=100):
                network._sample(1, None)
                result = network.collect_network([1], None)
        self.assertIsNone(result['totals']['upload_bps'])
        self.assertEqual(result['totals']['reporting'], 0)
        self.assertEqual(result['servers'][0]['error'], 'unsupported')


class NetworkAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.app = app.app
        cls.context = cls.app.app_context()
        cls.context.push()
        app.db.create_all()

    @classmethod
    def tearDownClass(cls):
        from panel.extensions import db
        db.session.remove()
        cls.context.pop()

    def test_anonymous_cannot_read_network(self):
        with patch.object(network, 'collect_network') as collect:
            response = self.app.test_client().get('/api/servers/network')
            self.assertIn(response.status_code, (302, 401))
            collect.assert_not_called()

    def test_only_accessible_enabled_visible_servers_are_collected(self):
        from panel.extensions import db
        from panel.models import Admin
        from types import SimpleNamespace
        admin = Admin(username='network-access-test', password_hash='x', role='reseller')
        db.session.add(admin)
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as browser:
            browser.update(admin_id=admin.id, role='reseller', admin_username=admin.username)
        # The established access helper filters disabled and unassigned servers.
        rows = [SimpleNamespace(id=1, hidden=False), SimpleNamespace(id=2, hidden=True)]
        with patch('app.get_accessible_servers', return_value=rows) as accessible:
            with patch.object(network, 'collect_network', return_value={'servers': [], 'totals': {}}) as collect:
                response = client.get('/api/servers/network')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(accessible.call_args.args[0].id, admin.id)
        self.assertEqual(collect.call_args.args[0], [1])


if __name__ == '__main__':
    unittest.main()
