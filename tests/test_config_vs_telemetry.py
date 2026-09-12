"""Phase 6: configuration state and telemetry state age independently.

A renew must show its new expiry without waiting for a traffic poll, and a traffic poll
must not look like a configuration change. Both layers live on the same cached row but
carry their own freshness stamp, and the normalized client shape exposes both.
"""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
import panel.core.redis_client as redis_cache  # noqa: E402
import panel.jobs.refresh as refresh_jobs  # noqa: E402
from app import db  # noqa: E402

GB = 1024 ** 3


def _row():
    raw = {'id': 'uuid-1', 'email': 'bob', 'enable': True, 'totalGB': 25 * GB,
           'expiryTime': 1_800_000_000_000, 'comment': ''}
    return {'server_id': 7, 'inbound_id': 1, 'email': 'bob', 'id': 'uuid-1',
            'up': 10 * GB, 'down': 0, 'raw_client': raw}


class FreshnessStampTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The display helpers the recompute uses resolve panel language/config, and a
        # suite that ran before this one may have dropped the tables.
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        self.row = _row()

    def test_a_configuration_write_stamps_only_configuration(self):
        refresh_jobs._recompute_cached_client(
            self.row, config_changed=True, telemetry_changed=False)
        self.assertIn('config_updated_at', self.row)
        self.assertNotIn('telemetry_updated_at', self.row)

    def test_a_telemetry_write_stamps_only_telemetry(self):
        refresh_jobs._recompute_cached_client(
            self.row, config_changed=False, telemetry_changed=True)
        self.assertIn('telemetry_updated_at', self.row)
        self.assertNotIn('config_updated_at', self.row)

    def test_a_panel_read_stamps_both_layers_on_every_row(self):
        block = [{'server_id': 7, 'id': 1, 'clients': [_row(), _row()]}]
        refresh_jobs._stamp_snapshot_rows(block, config=True, telemetry=True)
        for client in block[0]['clients']:
            self.assertIn('config_updated_at', client)
            self.assertIn('telemetry_updated_at', client)

    def test_the_write_through_stamps_what_it_actually_wrote(self):
        original = dict(redis_cache.GLOBAL_SERVER_DATA)
        latest = {'server_id': 7, 'id': 1, 'clients': [_row()]}

        class SnapshotContext:
            def __enter__(self):
                redis_cache.GLOBAL_SERVER_DATA['inbounds'] = [latest]

            def __exit__(self, *_args):
                return False

        try:
            with (
                mock.patch.object(refresh_jobs, 'bump_server_revision'),
                mock.patch.object(refresh_jobs, 'get_server_revision', return_value=0),
                mock.patch.object(refresh_jobs, 'enqueue_refresh_job'),
                mock.patch.object(refresh_jobs, 'serialized_server_snapshot_write',
                                  return_value=SnapshotContext()),
                mock.patch.object(refresh_jobs, 'publish_snapshot_to_redis', return_value=True),
                mock.patch.object(app_module, '_get_dashboard_status_thresholds', return_value={}),
                mock.patch.object(app_module, '_get_panel_ui_lang', return_value='en'),
            ):
                with app_module.app.app_context():
                    # Configuration only: the renew path.
                    refresh_jobs.patch_cached_client(7, 'bob', total_gb_bytes=35 * GB,
                                                     expiry_ts=1_900_000_000_000, enable=True)
                    row = latest['clients'][0]
                    self.assertIn('config_updated_at', row)
                    self.assertNotIn('telemetry_updated_at', row,
                                     'a config write must not claim telemetry is fresh')

                    # Telemetry only: a usage refresh.
                    refresh_jobs.patch_cached_client(7, 'bob', up=0, down=0)
                    self.assertIn('telemetry_updated_at', row)
        finally:
            redis_cache.GLOBAL_SERVER_DATA.clear()
            redis_cache.GLOBAL_SERVER_DATA.update(original)

    def test_the_normalized_state_exposes_both_stamps(self):
        from panel.services.client_state import CLIENT_STATE_FIELDS, normalize_client_state

        row = _row()
        row['config_updated_at'] = '2026-01-01T00:00:00'
        row['telemetry_updated_at'] = '2026-01-01T00:00:05'
        state = normalize_client_state(row=row)
        self.assertEqual(set(state), set(CLIENT_STATE_FIELDS))
        self.assertEqual(state['config_updated_at'], '2026-01-01T00:00:00')
        self.assertEqual(state['telemetry_updated_at'], '2026-01-01T00:00:05')


if __name__ == '__main__':
    unittest.main()
