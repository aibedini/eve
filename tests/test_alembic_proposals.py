"""proposals (revision c7e1a4b9d2f6) must be additive and self-healing.

`panel/migrate.py` runs `db.create_all()` before the Alembic upgrade, so the table
and its indexes can already exist while the ledger still sits on the previous
revision (XR-GA-009's machine_api_keys). The revision must adopt that state
instead of failing with DuplicateTable/DuplicateIndex.
"""
import os
import sqlite3
import tempfile
import unittest

from alembic import command
from alembic.config import Config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(REPO_ROOT, 'alembic.ini')
PREVIOUS = 'f4c1a9b2d3e5'
TARGET = 'c7e1a4b9d2f6'

_REVISION_COLUMNS = {
    'id', 'proposal_id', 'type', 'customer_id', 'service_key', 'generation',
    'payload', 'status', 'reason', 'created_by_key_id', 'created_at',
    'decided_by', 'decided_at',
}


class ProposalsRevisionTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_previous(self, *, table_already_created=False):
        conn = self._connect()
        conn.executescript(
            'CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);')
        conn.execute('INSERT INTO alembic_version (version_num) VALUES (?)',
                     (PREVIOUS,))
        if table_already_created:
            # Exactly what db.create_all() does from panel/models/proposals.py.
            conn.executescript("""
                CREATE TABLE proposals (
                    id INTEGER NOT NULL PRIMARY KEY,
                    proposal_id VARCHAR(128) NOT NULL,
                    type VARCHAR(32) NOT NULL,
                    customer_id INTEGER,
                    service_key VARCHAR(255),
                    generation BIGINT,
                    payload TEXT NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    reason VARCHAR(500),
                    created_by_key_id INTEGER,
                    created_at DATETIME NOT NULL,
                    decided_by VARCHAR(100),
                    decided_at DATETIME
                );
                CREATE UNIQUE INDEX ix_proposals_proposal_id
                    ON proposals (proposal_id);
                CREATE INDEX ix_proposals_status_created
                    ON proposals (status, created_at);
            """)
        conn.commit()
        conn.close()

    def _run(self, direction):
        previous_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = 'sqlite:///' + self.db_path.replace(os.sep, '/')
        try:
            if direction == 'upgrade':
                command.upgrade(Config(ALEMBIC_INI), TARGET)
            else:
                command.downgrade(Config(ALEMBIC_INI), PREVIOUS)
        finally:
            if previous_url is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = previous_url

    def _state(self):
        conn = self._connect()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        columns = ({row[1] for row in conn.execute('PRAGMA table_info(proposals)')}
                   if 'proposals' in tables else set())
        indexes = ({row[1]: bool(row[2]) for row in conn.execute(
            "PRAGMA index_list('proposals')")}
                   if 'proposals' in tables else {})
        version = conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        conn.close()
        return tables, columns, indexes, version

    def test_upgrade_creates_the_table_and_both_indexes(self):
        self._init_previous()
        self._run('upgrade')
        tables, columns, indexes, version = self._state()
        self.assertIn('proposals', tables)
        self.assertEqual(version, TARGET)
        self.assertEqual(columns, _REVISION_COLUMNS)
        self.assertTrue(indexes.get('ix_proposals_proposal_id'))
        self.assertIn('ix_proposals_status_created', indexes)

    def test_upgrade_self_heals_when_create_all_already_made_the_table(self):
        self._init_previous(table_already_created=True)
        self._run('upgrade')
        tables, columns, indexes, version = self._state()
        self.assertIn('proposals', tables)
        self.assertEqual(version, TARGET)
        self.assertTrue(_REVISION_COLUMNS.issubset(columns))
        self.assertTrue(indexes.get('ix_proposals_proposal_id'))
        self.assertIn('ix_proposals_status_created', indexes)

    def test_unique_index_rejects_a_duplicate_proposal_id(self):
        self._init_previous()
        self._run('upgrade')
        conn = self._connect()
        insert = ('INSERT INTO proposals '
                  '(proposal_id, type, payload, created_at) VALUES (?, ?, ?, ?)')
        conn.execute(insert, ('prop-1', 'reminder_text', '{}', '2026-11-15 10:00:00'))
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(insert,
                         ('prop-1', 'reminder_text', '{}', '2026-11-15 10:00:00'))
        conn.close()

    def test_status_defaults_to_pending(self):
        self._init_previous()
        self._run('upgrade')
        conn = self._connect()
        conn.execute('INSERT INTO proposals (proposal_id, type, payload, created_at) '
                     "VALUES ('prop-default', 'reminder_text', '{}', "
                     "'2026-11-15 10:00:00')")
        conn.commit()
        status = conn.execute(
            "SELECT status FROM proposals WHERE proposal_id = 'prop-default'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, 'pending')

    def test_downgrade_removes_the_table(self):
        self._init_previous()
        self._run('upgrade')
        self._run('downgrade')
        tables, _columns, _indexes, version = self._state()
        self.assertNotIn('proposals', tables)
        self.assertEqual(version, PREVIOUS)


if __name__ == '__main__':
    unittest.main()
