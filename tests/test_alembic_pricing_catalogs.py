"""Upgrade the pre-contract schema without discarding existing package prices."""
import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class PricingCatalogMigrationTests(unittest.TestCase):
    def test_upgrade_can_resume_after_schema_creation(self):
        path = Path(__file__).resolve().parents[1] / 'alembic/versions/6c2b98d107ef_pricing_catalogs_and_reseller_contracts.py'
        spec = importlib.util.spec_from_file_location('pricing_catalog_revision', path)
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)

        engine = sa.create_engine('sqlite://')
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE packages (id INTEGER PRIMARY KEY, price INTEGER)')
                connection.exec_driver_sql('INSERT INTO packages VALUES (1, 150000)')
                for table in (
                    'admins', 'client_operations', 'telegram_purchase_requests', 'telegram_purchase_sessions',
                    'telegram_service_requests', 'telegram_service_sessions',
                ):
                    connection.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
                with Operations.context(MigrationContext.configure(connection)):
                    revision.upgrade()
                    revision.upgrade()
                inspector = sa.inspect(connection)
                self.assertTrue(inspector.has_table('reseller_contracts'))
                self.assertTrue(inspector.has_table('reseller_offers'))
                self.assertEqual(connection.exec_driver_sql('SELECT price, panel_roles FROM packages').one(),
                                 (150000, None))
                columns = {column['name'] for column in inspector.get_columns('client_operations')}
                self.assertTrue({'quota_mib', 'pricing_quote', 'billing_admin_id'} <= columns)
                columns = {column['name'] for column in inspector.get_columns('telegram_service_sessions')}
                self.assertIn('pricing_quote', columns)
        finally:
            engine.dispose()
