import os
import tempfile
import unittest

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
import base64  # noqa: E402
os.environ['SERVER_PASSWORD_KEY'] = base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode()

import json  # noqa: E402

from app import (  # noqa: E402
    Admin,
    TelegramBotInstance,
    app,
    db,
)
from telegram_bot_runtime import (  # noqa: E402
    COPY,
    CopyString,
    HIDEABLE_MENU_KEYS,
    copy_key_hidden,
    main_menu_keyboard,
    menu_label_map,
    resolve_copy,
)


class _FakeBot:
    """Minimal stand-in for a TelegramBotInstance for pure copy helpers."""

    def __init__(self, copy_overrides_json=''):
        self.copy_overrides_json = copy_overrides_json


class CopyHelperTests(unittest.TestCase):
    def test_resolve_copy_no_overrides_returns_copy_as_is(self):
        self.assertIs(resolve_copy(_FakeBot()), COPY)
        self.assertIs(resolve_copy(_FakeBot('not-json')), COPY)
        self.assertIs(resolve_copy({}), COPY)

    def test_resolve_copy_fa_override_leaves_en_untouched(self):
        bot = _FakeBot(json.dumps({'menu_services': {'fa': 'سرویس‌های کاستوم'}}))
        merged = resolve_copy(bot)
        self.assertEqual(merged['fa']['menu_services'], 'سرویس‌های کاستوم')
        self.assertEqual(merged['en']['menu_services'], COPY['en']['menu_services'])
        # Other fa keys untouched too.
        self.assertEqual(merged['fa']['menu_wallet'], COPY['fa']['menu_wallet'])

    def test_resolve_copy_empty_string_override_ignored(self):
        bot = _FakeBot(json.dumps({'menu_services': {'fa': '   ', 'en': ''}}))
        merged = resolve_copy(bot)
        self.assertEqual(merged['fa']['menu_services'], COPY['fa']['menu_services'])
        self.assertEqual(merged['en']['menu_services'], COPY['en']['menu_services'])

    def test_resolve_copy_unknown_key_ignored(self):
        bot = _FakeBot(json.dumps({'not_a_real_key': {'fa': 'x'}}))
        merged = resolve_copy(bot)
        self.assertEqual(merged['fa'], COPY['fa'])
        self.assertEqual(merged['en'], COPY['en'])

    def test_copy_key_hidden(self):
        overrides = {'menu_wallet': {'hidden': True}, 'menu_orders': {'fa': 'x'}}
        self.assertTrue(copy_key_hidden(overrides, 'menu_wallet'))
        self.assertFalse(copy_key_hidden(overrides, 'menu_orders'))
        self.assertFalse(copy_key_hidden(overrides, 'missing'))
        self.assertFalse(copy_key_hidden(None, 'menu_wallet'))
        self.assertFalse(copy_key_hidden({'menu_wallet': 'nope'}, 'menu_wallet'))

    def test_main_menu_keyboard_label_override(self):
        bot = _FakeBot(json.dumps({'menu_wallet': {'fa': 'موجودی من'}}))
        copy = resolve_copy(bot)
        keyboard = main_menu_keyboard('fa', copy=copy)
        texts = [btn['text'] for row in keyboard['keyboard'] for btn in row]
        self.assertIn('موجودی من', texts)
        self.assertNotIn(COPY['fa']['menu_wallet'], texts)

    def test_main_menu_keyboard_hidden_key_button_absent(self):
        overrides = {'menu_wallet': {'hidden': True}}
        keyboard = main_menu_keyboard('fa', overrides=overrides)
        texts = [btn['text'] for row in keyboard['keyboard'] for btn in row]
        self.assertNotIn(COPY['fa']['menu_wallet'], texts)
        # Its row-mate survives.
        self.assertIn(COPY['fa']['menu_orders'], texts)

    def test_main_menu_keyboard_all_hidden_falls_back(self):
        overrides = {key: {'hidden': True} for key in HIDEABLE_MENU_KEYS}
        keyboard = main_menu_keyboard('fa', overrides=overrides, show_trial=True)
        self.assertEqual(
            keyboard['keyboard'], [[{'text': COPY['fa']['menu_language']}]])

    def test_menu_label_map_custom_and_default_labels(self):
        bot = _FakeBot(json.dumps({'menu_wallet': {'fa': 'موجودی من'}}))
        mapping = menu_label_map(bot)
        self.assertEqual(mapping['موجودی من'], 'menu_wallet')
        # Default labels (fa and en) still route to their keys.
        self.assertEqual(mapping[COPY['fa']['menu_services']], 'menu_services')
        self.assertEqual(mapping[COPY['en']['menu_services']], 'menu_services')
        # en default for the overridden key still maps (only fa was overridden).
        self.assertEqual(mapping[COPY['en']['menu_wallet']], 'menu_wallet')

    def test_copystring_format_falls_back_on_bad_placeholder(self):
        value = CopyString('Order {wrong_name}', fallback='Order {order_id}')
        self.assertEqual(value.format(order_id=7), 'Order 7')

    def test_copystring_format_ok_when_placeholder_valid(self):
        value = CopyString('Order #{order_id}', fallback='fallback')
        self.assertEqual(value.format(order_id=3), 'Order #3')


class CopyOverridesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()
        try:
            os.unlink(_DB_FILE.name)
        except OSError:
            pass

    def tearDown(self):
        db.session.rollback()
        TelegramBotInstance.query.delete()
        Admin.query.filter(Admin.username.like('copy-test-%')).delete(
            synchronize_session=False)
        db.session.commit()

    def _admin(self, suffix, role='reseller', **kwargs):
        admin = Admin(username=f'copy-test-{suffix}', role=role, enabled=True, **kwargs)
        admin.set_password('StrongBotPassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _bot(self, owner=None, suffix='bot', **kwargs):
        bot = TelegramBotInstance(
            scope_key=f'copy-test-{suffix}-{id(object())}',
            owner_type='reseller' if owner else 'system',
            owner_admin_id=owner.id if owner else None,
            display_name=kwargs.pop('display_name', 'Copy Test Bot'),
            **kwargs,
        )
        db.session.add(bot)
        db.session.flush()
        return bot

    def _client(self, admin):
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = admin.id
        return client

    def test_save_and_get_copy_overrides(self):
        reseller = self._admin('owner')
        bot = self._bot(owner=reseller, suffix='save')
        db.session.commit()
        client = self._client(reseller)

        overrides = {
            'menu_wallet': {'fa': 'موجودی من', 'hidden': True},
            'menu_services': {'en': 'My Stuff'},
        }
        saved = client.post(
            f'/api/settings/telegram-bots?bot_id={bot.id}',
            json={'copy_overrides': overrides},
        )
        self.assertTrue(saved.get_json()['success'])

        payload = client.get(f'/api/settings/telegram-bots?bot_id={bot.id}').get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['copy_overrides'], {
            'menu_wallet': {'fa': 'موجودی من', 'hidden': True},
            'menu_services': {'en': 'My Stuff'},
        })
        self.assertIn('menu_services', payload['copy_defaults']['fa'])
        self.assertIn('menu_wallet', payload['hideable_keys'])
        # Persisted to the model column as cleaned JSON.
        db.session.expire_all()
        stored = json.loads(
            db.session.get(TelegramBotInstance, bot.id).copy_overrides_json)
        self.assertEqual(stored['menu_wallet'], {'fa': 'موجودی من', 'hidden': True})

    def test_save_copy_overrides_invalid_key_rejected(self):
        reseller = self._admin('bad')
        bot = self._bot(owner=reseller, suffix='bad')
        db.session.commit()
        client = self._client(reseller)
        response = client.post(
            f'/api/settings/telegram-bots?bot_id={bot.id}',
            json={'copy_overrides': {'no_such_key': {'fa': 'x'}}},
        )
        # Error statuses are rewritten to 200; the real code is in X-Eve-Status.
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')
        self.assertFalse(response.get_json()['success'])

    def test_reseller_cannot_edit_another_resellers_bot_copy(self):
        owner = self._admin('owner2')
        outsider = self._admin('outsider')
        bot = self._bot(owner=owner, suffix='forbidden')
        db.session.commit()
        other_client = self._client(outsider)
        response = other_client.post(
            f'/api/settings/telegram-bots?bot_id={bot.id}',
            json={'copy_overrides': {'menu_wallet': {'fa': 'هک'}}},
        )
        self.assertEqual(response.status_code, 403)
        db.session.expire_all()
        self.assertEqual(
            db.session.get(TelegramBotInstance, bot.id).copy_overrides_json, '')


if __name__ == '__main__':
    unittest.main()
