"""RenewalEvent v2 (revision b8d2e3f4a5c6) must be additive and self-healing."""
import os
import sqlite3
import tempfile
import unittest

from alembic import command
from alembic.config import Config

# A scratch database for the model contract tests: importing app() runs the runtime
# migration runner, which must never touch a developer's instance/servers.db.
_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ.setdefault('FLASK_ENV', 'development')
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(REPO_ROOT, 'alembic.ini')
PREVIOUS = 'c9d8e7f6a5b4'
TARGET = 'b8d2e3f4a5c6'

_V1_TABLE = """
CREATE TABLE renewal_events (
    id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL,
    sub_id VARCHAR(128) NOT NULL,
    renewed_at DATETIME NOT NULL,
    volume_bytes BIGINT,
    days INTEGER,
    is_unlimited_volume BOOLEAN,
    is_unlimited_time BOOLEAN
)
"""

_V2_COLUMNS = {
    'client_uuid', 'client_email_snapshot', 'event_type', 'source',
    'previous_volume_limit_bytes', 'new_volume_limit_bytes',
    'previous_remaining_bytes', 'carried_over_bytes', 'granted_volume_bytes',
    'previous_expiry_at', 'new_expiry_at', 'traffic_reset', 'operation_id',
    'verified', 'verified_at', 'created_at',
}


class RenewalEventV2RevisionTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _init_db(self, *, with_v2_columns=False, legacy_rows=0):
        conn = sqlite3.connect(self.db_path)
        cols = _V1_TABLE
        if with_v2_columns:
            # The broken-in-between state: panel.migrate's column catch-up ran before
            # alembic did, so the ledger is still on the previous revision.
            cols = cols.rstrip()
            cols = cols[:-1] + """,
                client_uuid VARCHAR(64),
                client_email_snapshot VARCHAR(255),
                event_type VARCHAR(32) DEFAULT 'inferred_reset',
                source VARCHAR(32) DEFAULT 'inferred',
                previous_volume_limit_bytes BIGINT,
                new_volume_limit_bytes BIGINT,
                previous_remaining_bytes BIGINT,
                carried_over_bytes BIGINT,
                granted_volume_bytes BIGINT,
                previous_expiry_at DATETIME,
                new_expiry_at DATETIME,
                traffic_reset BOOLEAN DEFAULT FALSE,
                operation_id VARCHAR(64),
                verified BOOLEAN DEFAULT FALSE,
                verified_at DATETIME,
                created_at DATETIME
            )
            """
        conn.executescript(cols)
        # The indexes the baseline created for this table: an additive upgrade must
        # keep them (SQLite batch mode rebuilds the table).
        conn.executescript(
            """
            CREATE INDEX ix_renewal_events_server_id ON renewal_events (server_id);
            CREATE INDEX ix_renewal_events_sub_id ON renewal_events (sub_id);
            CREATE INDEX ix_renewal_events_server_sub ON renewal_events (server_id, sub_id);
            """
        )
        conn.executescript(
            'CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);'
        )
        conn.execute('INSERT INTO alembic_version (version_num) VALUES (?)', (PREVIOUS,))
        for index in range(legacy_rows):
            conn.execute(
                'INSERT INTO renewal_events (server_id, sub_id, renewed_at, volume_bytes) '
                'VALUES (?, ?, ?, ?)',
                (10, 'legacy-%d' % index, '2026-09-01 10:00:00', 50 * 1024 ** 3),
            )
        conn.commit()
        conn.close()

    def _upgrade(self):
        previous_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f"sqlite:///{self.db_path.replace(os.sep, '/')}"
        try:
            command.upgrade(Config(ALEMBIC_INI), TARGET)
        finally:
            if previous_url is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = previous_url

    def _downgrade(self):
        previous_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f"sqlite:///{self.db_path.replace(os.sep, '/')}"
        try:
            command.downgrade(Config(ALEMBIC_INI), PREVIOUS)
        finally:
            if previous_url is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = previous_url

    def _state(self):
        conn = sqlite3.connect(self.db_path)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(renewal_events)')}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list('renewal_events')")}
        version = conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        rows = []
        if 'event_type' in columns:
            rows = list(conn.execute(
                'SELECT event_type, source, verified, created_at, renewed_at '
                'FROM renewal_events'))
        conn.close()
        return columns, indexes, version, rows

    def test_upgrade_adds_the_v2_columns_and_indexes(self):
        self._init_db(legacy_rows=2)
        self._upgrade()
        columns, indexes, version, _rows = self._state()
        self.assertEqual(version, TARGET)
        self.assertTrue(_V2_COLUMNS.issubset(columns), _V2_COLUMNS - columns)
        self.assertIn('ix_renewal_events_server_sub_renewed', indexes)
        self.assertIn('ix_renewal_events_server_sub_verified_renewed', indexes)
        self.assertIn('ix_renewal_events_operation_id', indexes)

    def test_legacy_rows_become_unverified_inferred_resets(self):
        self._init_db(legacy_rows=3)
        self._upgrade()
        _columns, _indexes, _version, rows = self._state()
        self.assertEqual(len(rows), 3)
        for event_type, source, verified, created_at, renewed_at in rows:
            self.assertEqual(event_type, 'inferred_reset')
            self.assertEqual(source, 'counter_reset')
            # A pre-v2 row was never read back from the panel: it must not become a
            # cycle boundary (the bug this revision exists to prevent).
            self.assertFalse(verified)
            self.assertEqual(created_at, renewed_at)

    def test_upgrade_self_heals_when_the_runtime_catchup_already_added_columns(self):
        self._init_db(with_v2_columns=True, legacy_rows=1)
        self._upgrade()
        columns, indexes, version, rows = self._state()
        self.assertEqual(version, TARGET)
        self.assertTrue(_V2_COLUMNS.issubset(columns))
        self.assertIn('ix_renewal_events_server_sub_renewed', indexes)
        self.assertEqual(rows[0][1], 'counter_reset')

    def test_downgrade_removes_only_the_v2_columns(self):
        self._init_db(legacy_rows=1)
        self._upgrade()
        self._downgrade()
        columns, indexes, version, _rows = self._state()
        self.assertEqual(version, PREVIOUS)
        self.assertFalse(_V2_COLUMNS & columns)
        # The v1 columns and the original composite index survive.
        self.assertIn('volume_bytes', columns)
        self.assertIn('ix_renewal_events_server_sub', indexes)

    def test_the_unique_operation_constraint_enforces_one_event_per_operation(self):
        self._init_db(legacy_rows=0)
        self._upgrade()
        conn = sqlite3.connect(self.db_path)
        unique = [row for row in conn.execute("PRAGMA index_list('renewal_events')")
                  if row[2]]
        self.assertTrue(unique, 'no unique index or constraint on renewal_events')

        def insert(event_type, operation_id, at):
            conn.execute(
                'INSERT INTO renewal_events (server_id, sub_id, renewed_at, event_type, '
                'operation_id) VALUES (1, ?, ?, ?, ?)',
                ('retry-account', at, event_type, operation_id))

        insert('renewal', 'op-1', '2026-09-12 00:00:00')
        conn.commit()
        # The retry of the same renewal must not open a second cycle ...
        with self.assertRaises(sqlite3.IntegrityError):
            insert('renewal', 'op-1', '2026-09-12 00:00:05')
            conn.commit()
        conn.rollback()
        # ... while a different event type on the same operation stays possible.
        insert('traffic_reset', 'op-1', '2026-09-12 00:00:06')
        # Telemetry rows have no operation and never collide with each other.
        insert('inferred_reset', None, '2026-09-12 00:00:07')
        insert('inferred_reset', None, '2026-09-12 00:00:08')
        conn.commit()
        count = conn.execute('SELECT COUNT(*) FROM renewal_events').fetchone()[0]
        conn.close()
        self.assertEqual(count, 4)


class RenewalEventModelContractTests(unittest.TestCase):
    """The model's defaults must fail safe, and the boundary rule must be explicit."""

    def setUp(self):
        from app import app, db
        self.app, self.db = app, db
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.ctx.pop()

    def test_a_default_row_is_not_a_cycle_boundary(self):
        from panel.models import RenewalEvent
        event = RenewalEvent(server_id=1, sub_id='contract-account')
        self.db.session.add(event)
        self.db.session.commit()
        self.assertEqual(event.event_type, 'inferred_reset')
        self.assertEqual(event.source, 'inferred')
        self.assertFalse(event.verified)
        self.assertFalse(event.is_cycle_boundary)

    def test_only_verified_renewal_or_package_change_is_a_boundary(self):
        from panel.models import RenewalEvent
        cases = (
            ('renewal', True, True),
            ('package_change', True, True),
            ('renewal', False, False),
            ('quota_topup', True, False),
            ('traffic_reset', True, False),
            ('inferred_reset', True, False),
            ('expiry_extension', True, False),
        )
        for event_type, verified, expected in cases:
            event = RenewalEvent(server_id=1, sub_id='boundary', event_type=event_type,
                                 verified=verified)
            self.assertEqual(event.is_cycle_boundary, expected,
                             '%s verified=%s' % (event_type, verified))

    def test_a_retried_operation_cannot_open_two_cycles_of_the_same_type(self):
        from panel.models import RenewalEvent
        from sqlalchemy.exc import IntegrityError
        self.db.session.add(RenewalEvent(
            server_id=1, sub_id='retry', event_type='renewal', source='explicit_renew',
            verified=True, operation_id='op-1'))
        self.db.session.commit()
        self.db.session.add(RenewalEvent(
            server_id=1, sub_id='retry', event_type='renewal', source='explicit_renew',
            verified=True, operation_id='op-1'))
        with self.assertRaises(IntegrityError):
            self.db.session.commit()
        self.db.session.rollback()

    def test_to_dict_keeps_the_email_snapshot_out_by_default(self):
        from panel.models import RenewalEvent
        event = RenewalEvent(server_id=1, sub_id='privacy', operation_id='op-2',
                             client_email_snapshot='09120000000@example.test',
                             event_type='renewal', verified=True)
        self.assertIsNone(event.to_dict()['client_email'])
        self.assertEqual(event.to_dict(redact=False)['client_email'],
                         '09120000000@example.test')


if __name__ == '__main__':
    unittest.main()
