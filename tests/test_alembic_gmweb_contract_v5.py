"""Contract v5 evidence migration is additive, reversible and single-headed."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INI = os.path.join(ROOT, 'alembic.ini')
PREVIOUS = 'i6d7e8f9a0b1'
TARGET = 'j7e8f9a0b1c2'


class ContractV5MigrationTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript('''
                CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
                INSERT INTO alembic_version VALUES ('i6d7e8f9a0b1');
                CREATE TABLE sms_gateway_events (
                    event_id VARCHAR(160) PRIMARY KEY,
                    trace_id VARCHAR(64) NOT NULL,
                    message_id VARCHAR(128) NOT NULL,
                    eve_notification_id VARCHAR(128),
                    event_type VARCHAR(64) NOT NULL,
                    occurred_at DATETIME NOT NULL,
                    received_at DATETIME NOT NULL,
                    attempt INTEGER, device_id VARCHAR(64), reason_code VARCHAR(64), stage VARCHAR(64)
                );
                CREATE INDEX ix_sms_gateway_events_trace_id ON sms_gateway_events (trace_id);
                CREATE INDEX ix_sms_gateway_events_message_id ON sms_gateway_events (message_id);
                CREATE INDEX ix_sms_gateway_events_eve_notification_id ON sms_gateway_events (eve_notification_id);
                CREATE INDEX ix_sms_gateway_events_event_type ON sms_gateway_events (event_type);
                INSERT INTO sms_gateway_events
                    (event_id, trace_id, message_id, event_type, occurred_at, received_at)
                VALUES ('evt_keep', 'trace_keep', 'send_1', 'send.sent', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                CREATE TABLE sms_send_log (id INTEGER PRIMARY KEY);
            ''')

    def tearDown(self):
        os.unlink(self.path)

    def _command(self, fn, revision):
        previous = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = 'sqlite:///' + self.path.replace(os.sep, '/')
        try:
            fn(Config(INI), revision)
        finally:
            if previous is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = previous

    def test_upgrade_preserves_rows_adds_bounded_columns_and_indexes(self):
        self._command(command.upgrade, TARGET)
        with closing(sqlite3.connect(self.path)) as conn:
            event_columns = {row[1]: row[2] for row in
                             conn.execute("PRAGMA table_info('sms_gateway_events')")}
            event_indexes = {row[1] for row in
                             conn.execute("PRAGMA index_list('sms_gateway_events')")}
            log_columns = {row[1]: row[2] for row in
                           conn.execute("PRAGMA table_info('sms_send_log')")}
            log_indexes = {row[1] for row in
                           conn.execute("PRAGMA index_list('sms_send_log')")}
            count = conn.execute('SELECT COUNT(*) FROM sms_gateway_events').fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(event_columns['event_id'], 'VARCHAR(196)')
        self.assertEqual(event_columns['eve_notification_id'], 'VARCHAR(120)')
        for name in ('request_id', 'gateway_request_id', 'carrier_status', 'evidence'):
            self.assertIn(name, event_columns)
        self.assertTrue({'ix_sms_gateway_events_request_id',
                         'ix_sms_gateway_events_gateway_request_id',
                         'ix_sms_gateway_events_occurred_at'} <= event_indexes)
        for name in ('eve_notification_id', 'gateway_request_id', 'carrier_state',
                     'carrier_occurred_at', 'carrier_evidence'):
            self.assertIn(name, log_columns)
        self.assertTrue({'ix_sms_send_log_eve_notification_id',
                         'ix_sms_send_log_gateway_request_id'} <= log_indexes)

    def test_downgrade_restores_previous_shape(self):
        self._command(command.upgrade, TARGET)
        self._command(command.downgrade, PREVIOUS)
        with closing(sqlite3.connect(self.path)) as conn:
            event_columns = {row[1]: row[2] for row in
                             conn.execute("PRAGMA table_info('sms_gateway_events')")}
            log_columns = {row[1] for row in
                           conn.execute("PRAGMA table_info('sms_send_log')")}
        self.assertEqual(event_columns['event_id'], 'VARCHAR(160)')
        self.assertEqual(event_columns['eve_notification_id'], 'VARCHAR(128)')
        self.assertNotIn('request_id', event_columns)
        self.assertNotIn('carrier_state', log_columns)

    def test_repository_has_one_alembic_head(self):
        script = ScriptDirectory.from_config(Config(INI))
        self.assertEqual(script.get_heads(), [TARGET])


if __name__ == '__main__':
    unittest.main()
