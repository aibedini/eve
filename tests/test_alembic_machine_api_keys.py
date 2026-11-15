"""machine_api_keys (revision f4c1a9b2d3e5) must be additive and self-healing.

`panel/migrate.py` runs `db.create_all()` before the Alembic upgrade, so the
table and its unique index can already exist while the ledger still sits on the
previous revision. The revision must adopt that state instead of failing with
DuplicateTable/DuplicateIndex.
"""
import os
import sqlite3
import tempfile
import unittest

from alembic import command
from alembic.config import Config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(REPO_ROOT, 'alembic.ini')
PREVIOUS = 'm0b1c2d3e4f5'
TARGET = 'f4c1a9b2d3e5'

_REVISION_COLUMNS = {
    'id', 'name', 'key_hash', 'scopes', 'expires_at', 'last_used_at',
    'revoked_at', 'created_by', 'created_at',
}


class MachineApiKeysRevisionTests(unittest.TestCase):
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
            # Exactly what db.create_all() does from panel/models/machine.py.
            conn.executescript("""
                CREATE TABLE machine_api_keys (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR(120) NOT NULL,
                    key_hash VARCHAR(64) NOT NULL,
                    scopes TEXT NOT NULL DEFAULT '[]',
                    expires_at DATETIME,
                    last_used_at DATETIME,
                    revoked_at DATETIME,
                    created_by VARCHAR(100),
                    created_at DATETIME NOT NULL
                );
                CREATE UNIQUE INDEX ix_machine_api_keys_key_hash
                    ON machine_api_keys (key_hash);
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
        columns = ({row[1] for row in conn.execute('PRAGMA table_info(machine_api_keys)')}
                   if 'machine_api_keys' in tables else set())
        indexes = ({row[1]: bool(row[2]) for row in conn.execute(
            "PRAGMA index_list('machine_api_keys')")}
                   if 'machine_api_keys' in tables else {})
        version = conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        conn.close()
        return tables, columns, indexes, version

    def test_upgrade_creates_the_table_and_unique_key_hash_index(self):
        self._init_previous()
        self._run('upgrade')
        tables, columns, indexes, version = self._state()
        self.assertIn('machine_api_keys', tables)
        self.assertEqual(version, TARGET)
        self.assertEqual(columns, _REVISION_COLUMNS)
        self.assertTrue(indexes.get('ix_machine_api_keys_key_hash'))

    def test_upgrade_self_heals_when_create_all_already_made_the_table(self):
        self._init_previous(table_already_created=True)
        self._run('upgrade')
        tables, columns, indexes, version = self._state()
        self.assertIn('machine_api_keys', tables)
        self.assertEqual(version, TARGET)
        self.assertTrue(_REVISION_COLUMNS.issubset(columns))
        self.assertTrue(indexes.get('ix_machine_api_keys_key_hash'))

    def test_unique_index_rejects_a_duplicate_digest(self):
        self._init_previous()
        self._run('upgrade')
        conn = self._connect()
        insert = ('INSERT INTO machine_api_keys '
                  '(name, key_hash, scopes, created_at) VALUES (?, ?, ?, ?)')
        conn.execute(insert, ('one', 'a' * 64, '[]', '2026-11-15 10:00:00'))
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(insert, ('two', 'a' * 64, '[]', '2026-11-15 10:00:00'))
        conn.close()

    def test_downgrade_removes_the_table(self):
        self._init_previous()
        self._run('upgrade')
        self._run('downgrade')
        tables, _columns, _indexes, version = self._state()
        self.assertNotIn('machine_api_keys', tables)
        self.assertEqual(version, PREVIOUS)


if __name__ == '__main__':
    unittest.main()
