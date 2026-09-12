"""Phase 0 gate: a migration must not disable or reconfigure application logging.

`panel.migrate` runs Alembic inside the application process, and `alembic/env.py`
used to call `logging.config.fileConfig()` with its default
`disable_existing_loggers=True`. That switches off every logger that already
exists at that moment -- `app.logger` and each module logger imported before the
migration -- so their records were silently dropped for the rest of the process
lifetime (dev and tests; the production entrypoint only escaped it because it
migrates in a separate process first).
"""
import logging
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
PROBE_NAME = 'eve.logging.lifecycle'

# This module imports `app` (to inspect the loggers it creates), so it sets the same
# test environment the other suites set before their app import.
_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'


class MigrationLoggingTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute('CREATE TABLE packages (id INTEGER PRIMARY KEY, name VARCHAR(100))')
        conn.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
        conn.execute('INSERT INTO alembic_version (version_num) VALUES (?)', (BASELINE,))
        conn.commit()
        conn.close()

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _upgrade(self):
        old_url = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = f"sqlite:///{self.db_path.replace(os.sep, '/')}"
        try:
            command.upgrade(Config(ALEMBIC_INI), HEAD)
        finally:
            if old_url is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = old_url

    def test_a_logger_created_before_the_migration_keeps_working_after_it(self):
        probe = logging.getLogger(PROBE_NAME)
        probe.disabled = False

        self._upgrade()

        self.assertFalse(probe.disabled, 'the migration disabled an application logger')
        with self.assertLogs(PROBE_NAME, level='INFO') as captured:
            probe.info('still reporting after the migration')
        self.assertIn('still reporting after the migration', captured.output[0])

    def test_app_and_module_loggers_survive_the_migration(self):
        import app as app_module  # noqa: F401  (importing it runs the runtime migrations)
        import panel.services.backup as backup_service

        self._upgrade()

        self.assertFalse(app_module.app.logger.disabled, 'app.logger was disabled')
        self.assertFalse(backup_service._security_logger().disabled,
                         'the security channel was disabled')

    def test_the_app_logger_still_emits_after_the_migration(self):
        import app as app_module

        self._upgrade()

        with self.assertLogs(app_module.app.logger, level='ERROR') as captured:
            app_module.app.logger.error('phase 0 probe')
        self.assertIn('phase 0 probe', captured.output[0])


if __name__ == '__main__':
    unittest.main()
