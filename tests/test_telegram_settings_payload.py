import atexit
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine, inspect, text

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'


def _cleanup_module_db_file():
    try:
        os.unlink(_DB_FILE.name)
    except OSError:
        pass


atexit.register(_cleanup_module_db_file)

from app import (  # noqa: E402
    Admin,
    TelegramBotInstance,
    TelegramBotStartEvent,
    TelegramPurchasePolicy,
    _migrate_add_columns,
    app,
    db,
)


class TelegramSettingsPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def tearDown(self):
        db.session.rollback()
        TelegramBotStartEvent.query.delete()
        TelegramPurchasePolicy.query.delete()
        TelegramBotInstance.query.delete()
        Admin.query.filter(Admin.username.like('payload-test-%')).delete(
            synchronize_session=False)
        db.session.commit()
        db.session.remove()

    def _superadmin(self):
        admin = Admin(username='payload-test-super', role='superadmin',
                      is_superadmin=True, enabled=True)
        admin.set_password('StrongPayloadPassword123!')
        db.session.add(admin)
        db.session.commit()
        return admin

    def test_get_settings_returns_full_payload(self):
        admin = self._superadmin()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = admin.id
        response = client.get('/api/settings/telegram-bots')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        for key in (
                'bot', 'runtime', 'purchase_policy', 'purchase_servers',
                'purchase_packages', 'trial_packages', 'purchase_inbound_routes',
                'purchase_inbounds', 'test_users', 'proxies', 'egress_profiles',
                'start_stats'):
            self.assertIn(key, payload, key)
        self.assertEqual(payload['bot']['scope_key'], 'system')
        for key in (
                'trial_enabled', 'trial_package_id', 'emergency_enabled',
                'emergency_days', 'emergency_volume_gb', 'emergency_cooldown_days'):
            self.assertIn(key, payload['purchase_policy'], key)
        self.assertIn('archived', payload['bot'])

    def test_required_channels_and_start_statistics_are_per_bot(self):
        admin = self._superadmin()
        bot = TelegramBotInstance(scope_key='system', owner_type='system',
                                  display_name='Payload test bot')
        db.session.add(bot)
        db.session.flush()
        db.session.add_all([
            TelegramBotStartEvent(bot_instance_id=bot.id, telegram_user_id=10, is_new_user=True),
            TelegramBotStartEvent(bot_instance_id=bot.id, telegram_user_id=10, is_new_user=False),
        ])
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = admin.id
        response = client.post('/api/settings/telegram-bots', json={
            'display_name': bot.display_name,
            'required_channels': [{
                'chat_id': -1001234567890,
                'title': 'News',
                'invite_url': 'https://t.me/example_channel',
            }],
            'require_membership_on_start': True,
            'require_membership_on_delivery': True,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = client.get('/api/settings/telegram-bots').get_json()
        self.assertTrue(payload['bot']['require_membership_on_start'])
        self.assertTrue(payload['bot']['require_membership_on_delivery'])
        self.assertEqual(payload['bot']['required_channels'][0]['title'], 'News')
        self.assertEqual(payload['start_stats'], {
            'total': 2, 'new_users': 1, 'existing_users': 1, 'unique_users': 1,
        })

    def test_migrate_add_columns_resumes_after_partial_failure(self):
        """A failed ALTER must never skip the remaining columns (the 2.5.0
        production bug: partially applied telegram_purchase_policies migration)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, 'migration.db')
            engine = create_engine(f"sqlite:///{db_path.replace(os.sep, '/')}")
            with engine.begin() as connection:
                connection.execute(text(
                    'CREATE TABLE telegram_purchase_policies '
                    '(id INTEGER PRIMARY KEY, emergency_volume_gb INTEGER DEFAULT 1)'))

            isolated_db = SimpleNamespace(engine=engine)
            columns = [
                # Deliberately invalid SQL proves that one failed ALTER does not
                # prevent the valid columns that follow it from being applied.
                ('broken-column', 'BOOLEAN'),
                ('trial_enabled', 'BOOLEAN DEFAULT 0'),
                ('trial_package_id', 'INTEGER'),
                ('emergency_enabled', 'BOOLEAN DEFAULT 0'),
                ('emergency_days', 'INTEGER DEFAULT 1'),
                ('emergency_volume_gb', 'INTEGER DEFAULT 1'),
                ('emergency_cooldown_days', 'INTEGER DEFAULT 30'),
            ]

            try:
                with mock.patch('panel.migrate.db', isolated_db):
                    _migrate_add_columns('telegram_purchase_policies', columns)

                    inspector_columns = {
                        column['name']
                        for column in inspect(engine).get_columns(
                            'telegram_purchase_policies')
                    }
                    self.assertNotIn('broken-column', inspector_columns)
                    self.assertIn('trial_enabled', inspector_columns)
                    self.assertIn('emergency_days', inspector_columns)

                    # Re-running over an existing column must not raise or stop.
                    _migrate_add_columns('telegram_purchase_policies', [
                        ('emergency_volume_gb', 'INTEGER DEFAULT 1'),
                    ])
                    inspector_columns = {
                        column['name']
                        for column in inspect(engine).get_columns(
                            'telegram_purchase_policies')
                    }
                    self.assertIn('emergency_volume_gb', inspector_columns)
            finally:
                engine.dispose()


if __name__ == '__main__':
    unittest.main()
