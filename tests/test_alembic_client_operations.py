import os
import sqlite3
import tempfile
import unittest

from alembic import command
from alembic.config import Config


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(REPO_ROOT, 'alembic.ini')
PREVIOUS = 'f3a8b9c0d1e2'
TARGET = '04b9c0d1e2f3'


class ClientOperationsRevisionTests(unittest.TestCase):
    """The revision must adopt tables created early by db.create_all()."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _init_db(self, *, with_client_operations):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE admins (id INTEGER PRIMARY KEY);
            CREATE TABLE servers (id INTEGER PRIMARY KEY);
            CREATE TABLE transactions (id INTEGER PRIMARY KEY);
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
            """
        )
        conn.execute('INSERT INTO alembic_version (version_num) VALUES (?)', (PREVIOUS,))
        if with_client_operations:
            conn.execute(
                """
                CREATE TABLE client_operations (
                    id INTEGER PRIMARY KEY,
                    idempotency_key VARCHAR(160) NOT NULL,
                    request_hash VARCHAR(64) NOT NULL,
                    action VARCHAR(32) NOT NULL,
                    admin_id INTEGER NOT NULL,
                    server_id INTEGER,
                    inbound_id INTEGER,
                    client_email VARCHAR(100),
                    amount INTEGER NOT NULL DEFAULT 0,
                    credit_reserved BOOLEAN NOT NULL DEFAULT 0,
                    state VARCHAR(32) NOT NULL DEFAULT 'reserved',
                    expected_json TEXT,
                    response_json TEXT,
                    error TEXT,
                    transaction_id INTEGER,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    completed_at DATETIME
                )
                """
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

    def _state(self):
        conn = sqlite3.connect(self.db_path)
        version = conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        indexes = {
            row[1]: bool(row[2])
            for row in conn.execute("PRAGMA index_list('client_operations')")
        }
        conn.close()
        return version, indexes

    def test_upgrade_adopts_create_all_table_and_adds_indexes(self):
        self._init_db(with_client_operations=True)
        self._upgrade()
        version, indexes = self._state()
        self.assertEqual(version, TARGET)
        self.assertEqual(len(indexes), 7)
        self.assertTrue(indexes['ix_client_operations_idempotency_key'])

    def test_upgrade_creates_table_on_clean_previous_revision(self):
        self._init_db(with_client_operations=False)
        self._upgrade()
        version, indexes = self._state()
        self.assertEqual(version, TARGET)
        self.assertEqual(len(indexes), 7)


if __name__ == '__main__':
    unittest.main()
