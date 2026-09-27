"""The SMS event revision must adopt tables created before Alembic runs."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from alembic import command
from alembic.config import Config


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALEMBIC_INI = os.path.join(REPO_ROOT, 'alembic.ini')
PREVIOUS = 'h5c6d7e8f9a0'
TARGET = 'i6d7e8f9a0b1'
INDEXES = {
    'ix_sms_gateway_events_trace_id',
    'ix_sms_gateway_events_message_id',
    'ix_sms_gateway_events_eve_notification_id',
    'ix_sms_gateway_events_event_type',
}


class SmsGatewayEventsRevisionTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)

    def tearDown(self):
        os.unlink(self.db_path)

    def _init_db(self, *, with_table):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
            conn.execute('INSERT INTO alembic_version VALUES (?)', (PREVIOUS,))
            if with_table:
                conn.execute('''
                    CREATE TABLE sms_gateway_events (
                        event_id VARCHAR(160) PRIMARY KEY,
                        trace_id VARCHAR(64) NOT NULL,
                        message_id VARCHAR(128) NOT NULL,
                        eve_notification_id VARCHAR(128),
                        event_type VARCHAR(64) NOT NULL,
                        occurred_at DATETIME NOT NULL,
                        received_at DATETIME NOT NULL,
                        attempt INTEGER,
                        device_id VARCHAR(64),
                        reason_code VARCHAR(64),
                        stage VARCHAR(64)
                    )
                ''')
                conn.execute("CREATE INDEX ix_sms_gateway_events_trace_id ON sms_gateway_events (trace_id)")
                conn.execute('''
                    INSERT INTO sms_gateway_events
                    (event_id, trace_id, message_id, event_type, occurred_at, received_at)
                    VALUES ('evt_existing', 'trace_existing', 'send_1', 'gateway.accepted',
                            '2026-09-27 00:00:00', '2026-09-27 00:00:00')
                ''')

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

    def _assert_state(self, *, retained):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            version = conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]
            indexes = {row[1] for row in conn.execute("PRAGMA index_list('sms_gateway_events')")}
            count = conn.execute('SELECT COUNT(*) FROM sms_gateway_events').fetchone()[0]
        self.assertEqual(version, TARGET)
        self.assertTrue(INDEXES <= indexes)
        self.assertEqual(count, 1 if retained else 0)

    def test_adopts_existing_table_and_preserves_evidence(self):
        self._init_db(with_table=True)
        self._upgrade()
        self._assert_state(retained=True)

    def test_creates_table_on_clean_database(self):
        self._init_db(with_table=False)
        self._upgrade()
        self._assert_state(retained=False)


if __name__ == '__main__':
    unittest.main()
