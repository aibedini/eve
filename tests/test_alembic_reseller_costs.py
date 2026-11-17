"""Cost-policy migration preserves existing rows and adopts create_all schemas."""
import importlib.util
from pathlib import Path
import unittest

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class ResellerCostMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_rows_and_is_resumable(self):
        path = Path(__file__).resolve().parents[1] / 'alembic/versions/4fdb05815625_separate_reseller_cost_from_selling_.py'
        spec = importlib.util.spec_from_file_location('reseller_cost_migration', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine('sqlite://')
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE packages (id INTEGER PRIMARY KEY, price INTEGER, reseller_price INTEGER)')
                connection.exec_driver_sql('CREATE TABLE price_tiers (id INTEGER PRIMARY KEY, cost_per_gb INTEGER)')
                connection.exec_driver_sql('INSERT INTO packages VALUES (1, 100000, 80000)')
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    migration.upgrade()
                row = connection.exec_driver_sql('SELECT price, reseller_price, reseller_cost_mode, reseller_rounding FROM packages').one()
                self.assertEqual(tuple(row), (100000, 80000, 'auto', 0))
                columns = {c['name'] for c in sa.inspect(connection).get_columns('price_tiers')}
                self.assertTrue({'reseller_cost_per_gb', 'reseller_cost_per_day'} <= columns)
        finally:
            engine.dispose()
