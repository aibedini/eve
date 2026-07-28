import os
import sqlite3
import tempfile
import unittest

from alembic import command
from alembic.config import Config


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(REPO_ROOT, 'alembic.ini')
BASELINE = '11b7afcfe0ee'
HEAD = 'a3f9c2d71e84'


class PackageFlagsRevisionTests(unittest.TestCase):
    """a3f9c2d71e84 must be self-healing: v2.5.44 briefly added these columns
    via the runtime catch-up BEFORE Alembic ran, so production databases can
    already carry them while their ledger still sits at the baseline."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _init_db(self, with_columns):
        conn = sqlite3.connect(self.db_path)
        cols = 'id INTEGER PRIMARY KEY, name VARCHAR(100)'
        if with_columns:
            cols += ', show_on_create BOOLEAN DEFAULT 1, show_on_renew BOOLEAN DEFAULT 1'
        conn.execute(f'CREATE TABLE packages ({cols})')
        conn.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
        conn.execute('INSERT INTO alembic_version (version_num) VALUES (?)', (BASELINE,))
        conn.commit()
        conn.close()

    def _upgrade_head(self):
        # alembic/env.py resolves the URL from DATABASE_URL (it overrides
        # sqlalchemy.url), so point the env var at the scratch database.
        old_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f"sqlite:///{self.db_path.replace(os.sep, '/')}"
        try:
            cfg = Config(ALEMBIC_INI)
            command.upgrade(cfg, 'head')
        finally:
            if old_url is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = old_url

    def _state(self):
        conn = sqlite3.connect(self.db_path)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(packages)')}
        version = conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        conn.close()
        return cols, version

    def test_upgrade_self_heals_when_columns_already_exist(self):
        # The broken v2.5.44 state: columns added by the runtime catch-up,
        # ledger still at the baseline. Upgrade must not raise DuplicateColumn.
        self._init_db(with_columns=True)
        self._upgrade_head()
        cols, version = self._state()
        self.assertIn('show_on_create', cols)
        self.assertIn('show_on_renew', cols)
        self.assertEqual(version, HEAD)

    def test_upgrade_adds_columns_on_clean_baseline(self):
        self._init_db(with_columns=False)
        self._upgrade_head()
        cols, version = self._state()
        self.assertIn('show_on_create', cols)
        self.assertIn('show_on_renew', cols)
        self.assertEqual(version, HEAD)


if __name__ == '__main__':
    unittest.main()
