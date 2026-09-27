"""The state-age revision works on both legacy and create_all-first databases."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import sqlalchemy as sa


REVISION = (Path(__file__).resolve().parents[1] / 'alembic' / 'versions' /
            'h5c6d7e8f9a0_service_state_entry_age.py')


class StateEntryMigrationTests(unittest.TestCase):
    def test_upgrade_adds_missing_columns_and_is_idempotent(self):
        engine = sa.create_engine('sqlite:///:memory:')
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE service_observed_states (id INTEGER PRIMARY KEY)')

            class FakeOp:
                def get_bind(self):
                    return connection

                def add_column(self, table, column):
                    connection.exec_driver_sql(
                        f'ALTER TABLE {table} ADD COLUMN {column.name} '
                        f'{column.type.compile(dialect=connection.dialect)}')

            fake_alembic = types.ModuleType('alembic')
            fake_alembic.op = FakeOp()
            spec = importlib.util.spec_from_file_location('state_age_revision', REVISION)
            revision = importlib.util.module_from_spec(spec)
            with mock.patch.dict(sys.modules, {'alembic': fake_alembic}):
                spec.loader.exec_module(revision)
                revision.upgrade()
                revision.upgrade()
            columns = {column['name'] for column in
                       sa.inspect(connection).get_columns('service_observed_states')}
        self.assertIn('state_entered_at', columns)
        self.assertIn('state_entered_at_quality', columns)
