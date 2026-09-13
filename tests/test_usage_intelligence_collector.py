"""The usage collector records a counter decrease as telemetry, never as a renewal.

RFP sections 5, 33, 34 and tests 45.6/45.7/47: an explicit verified renewal opens a
cycle even when no counter moved; a counter decrease on its own can never open one.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import GLOBAL_SERVER_DATA, RenewalEvent, Server, app, db  # noqa: E402
from panel.jobs import schedulers  # noqa: E402
from panel.services.usage_intelligence import events as event_service  # noqa: E402

GB = 1024 ** 3


def _point(server_id, sub_id, total_bytes, limit=50 * GB, expiry_ms=0):
    return {
        'server_id': server_id,
        'sub_id': sub_id,
        'inbound_tag': 'inbound-1',
        'upload_bytes': 0,
        'download_bytes': total_bytes,
        'total_bytes': total_bytes,
        'remaining_bytes': max(limit - total_bytes, 0) if limit else None,
        'volume_limit_bytes': limit,
        'client': {
            'email': sub_id, 'id': 'uuid-%s' % sub_id,
            'expiryTimestamp': expiry_ms, 'totalGB': limit,
        },
    }


class CollectorResetSemanticsTests(unittest.TestCase):
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
        RenewalEvent.query.delete()
        Server.query.delete()
        db.session.commit()
        self.server = Server(name='collector', host='https://collector.invalid',
                             username='u', password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()
        self._saved = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore)

    def _restore(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved)
        db.session.rollback()

    def _run_collector(self, points):
        """Run the real rollup collector against a synthetic snapshot."""
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server.id,
            'id': 1,
            'remark': 'inbound-1',
            'clients': [point['client'] for point in points],
        }]
        for point in points:
            point['client']['up'] = point['upload_bytes']
            point['client']['down'] = point['download_bytes']
        with mock.patch.object(schedulers, '_usage_account_points',
                               return_value={(p['server_id'], p['sub_id']): p
                                             for p in points}):
            with mock.patch.object(schedulers, '_usage_tehran_date',
                                   return_value=datetime.utcnow().date()):
                return schedulers._collect_usage_rollups()

    def test_a_counter_decrease_creates_an_inferred_reset_not_a_renewal(self):
        now = datetime.utcnow()
        self._run_collector([_point(self.server.id, 'acct-a', 40 * GB)])
        # First observation: no counter movement possible yet.
        self.assertEqual(RenewalEvent.query.count(), 0)
        # The counter drops from 40GB to 2GB: telemetry sees a reset.
        self._run_collector([_point(self.server.id, 'acct-a', 2 * GB)])
        events = RenewalEvent.query.all()
        self.assertEqual(len(events), 1, [e.to_dict() for e in events])
        event = events[0]
        self.assertEqual(event.event_type, 'inferred_reset')
        self.assertEqual(event.source, 'counter_reset')
        self.assertFalse(event.verified)
        self.assertFalse(event.is_cycle_boundary)
        self.assertIsNone(event_service.latest_cycle_boundary(self.server.id, 'acct-a'))
        self.assertGreaterEqual(event.renewed_at, now - timedelta(minutes=1))

    def test_a_counter_decrease_right_after_an_explicit_renewal_is_deduplicated(self):
        event_service.record_verified_renewal(
            server_id=self.server.id, sub_id='acct-b', operation_id='op-b',
            previous_volume_limit_bytes=50 * GB, new_volume_limit_bytes=60 * GB,
            previous_remaining_bytes=0, granted_volume_bytes=10 * GB,
            previous_expiry_ms=1, new_expiry_ms=0)
        db.session.commit()

        self._run_collector([_point(self.server.id, 'acct-b', 40 * GB)])
        self._run_collector([_point(self.server.id, 'acct-b', 1 * GB)])

        events = RenewalEvent.query.filter_by(sub_id='acct-b').all()
        self.assertEqual(len(events), 1, [e.to_dict() for e in events])
        self.assertTrue(events[0].verified)
        self.assertTrue(events[0].is_cycle_boundary)

    def test_growth_is_not_a_reset(self):
        self._run_collector([_point(self.server.id, 'acct-c', 10 * GB)])
        self._run_collector([_point(self.server.id, 'acct-c', 20 * GB)])
        self.assertEqual(RenewalEvent.query.count(), 0)

    def test_the_collector_does_not_change_the_cycle_boundary_query(self):
        """A counter reset in the past cannot hide the real renewal in the present."""
        self._run_collector([_point(self.server.id, 'acct-d', 40 * GB)])
        self._run_collector([_point(self.server.id, 'acct-d', 1 * GB)])
        renewal = event_service.record_verified_renewal(
            server_id=self.server.id, sub_id='acct-d', operation_id='op-d',
            previous_volume_limit_bytes=50 * GB, new_volume_limit_bytes=50 * GB,
            previous_remaining_bytes=0, granted_volume_bytes=50 * GB,
            previous_expiry_ms=1, new_expiry_ms=0)
        db.session.commit()
        boundary = event_service.latest_cycle_boundary(self.server.id, 'acct-d')
        self.assertEqual(boundary.id, renewal.id)
        self.assertEqual(RenewalEvent.query.filter_by(
            sub_id='acct-d', verified=False).count(), 1)


if __name__ == '__main__':
    unittest.main()
