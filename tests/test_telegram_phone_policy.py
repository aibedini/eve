import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    CustomerAccount,
    TelegramBotInstance,
    TelegramBotUserState,
    TelegramIdentity,
    _save_telegram_bot_settings,
    app,
    db,
    normalize_international_phone,
    normalize_iran_mobile,
)
from telegram_bot_runtime import COPY  # noqa: E402
from telegram_bot_worker import _handle_contact  # noqa: E402


class FakeBotApi:
    def __init__(self):
        self.messages = []
        self.markups = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((int(chat_id), text))
        self.markups.append(kwargs.get('reply_markup'))
        return ({'ok': True}, 'direct')

    def call(self, method, payload=None, **kwargs):
        return ({}, 'direct')

    def answer_callback(self, callback_id, text=None):
        pass


class TelegramPhonePolicyTests(unittest.TestCase):
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
        TelegramBotUserState.query.delete()
        TelegramIdentity.query.delete()
        TelegramBotInstance.query.delete()
        CustomerAccount.query.delete()
        db.session.commit()

    def _bot(self, suffix='bot', allow_international=False):
        bot = TelegramBotInstance(
            scope_key=f'phone-test-{suffix}-{id(object())}',
            owner_type='system',
            display_name='Phone Policy Bot',
            phone_allow_international=allow_international,
        )
        db.session.add(bot)
        db.session.flush()
        return bot

    def _state(self, bot, user_id, language='fa'):
        state = TelegramBotUserState(
            bot_instance_id=bot.id, telegram_user_id=user_id, language=language)
        db.session.add(state)
        db.session.flush()
        return state

    def _send_contact(self, bot, user_id, phone_number, language='fa'):
        api = FakeBotApi()
        state = self._state(bot, user_id, language=language)
        message = {
            'chat': {'id': user_id},
            'contact': {'user_id': user_id, 'phone_number': phone_number},
        }
        sender = {'id': user_id, 'first_name': 'Test'}
        _handle_contact(api, bot, message, sender, state)
        return api, state

    # ── normalizer semantics ─────────────────────────────────────────────

    def test_normalize_international_phone_semantics(self):
        self.assertEqual(normalize_international_phone('+995 593 681 048'), '995593681048')
        self.assertEqual(normalize_international_phone('0044 20 7946 0958'), '442079460958')
        self.assertEqual(normalize_international_phone('995593681048'), '995593681048')
        self.assertEqual(normalize_international_phone('+1 (202) 555-0123'), '12025550123')
        # Persian digits are accepted.
        self.assertEqual(normalize_international_phone('۹۹۵۵۹۳۶۸۱۰۴۸'), '995593681048')
        # Too short / too long / garbage are rejected.
        self.assertEqual(normalize_international_phone('12345'), '')
        self.assertEqual(normalize_international_phone('1234567890123456'), '')
        self.assertEqual(normalize_international_phone('not a number'), '')
        self.assertEqual(normalize_international_phone(''), '')

    # ── contact flow: default (Iran-only) ────────────────────────────────

    def test_iranian_number_accepted_by_default(self):
        bot = self._bot(suffix='iran')
        api, state = self._send_contact(bot, 9_100_001, '+98 912 345 6789')
        self.assertEqual(state.step, 'verified')
        identity = TelegramIdentity.query.filter_by(telegram_user_id=9_100_001).one()
        self.assertEqual(identity.phone_normalized, '989123456789')
        customer = db.session.get(CustomerAccount, identity.customer_id)
        self.assertEqual(customer.primary_phone, '989123456789')
        self.assertIn(COPY['fa']['verified'], [text for _, text in api.messages])

    def test_foreign_number_rejected_by_default(self):
        bot = self._bot(suffix='foreign-default')
        api, state = self._send_contact(bot, 9_100_002, '+995 593 681 048')
        self.assertEqual(api.messages[-1][1], COPY['fa']['phone_invalid'])
        self.assertNotEqual(state.step, 'verified')
        self.assertEqual(CustomerAccount.query.count(), 0)
        identity = TelegramIdentity.query.filter_by(telegram_user_id=9_100_002).first()
        self.assertTrue(identity is None or not identity.phone_normalized)

    def test_garbage_rejected_by_default_with_iran_copy(self):
        bot = self._bot(suffix='garbage-default')
        api, _ = self._send_contact(bot, 9_100_003, '12345')
        self.assertEqual(api.messages[-1][1], COPY['fa']['phone_invalid'])

    # ── contact flow: international allowed ──────────────────────────────

    def test_foreign_number_accepted_when_flag_on(self):
        bot = self._bot(suffix='foreign-on', allow_international=True)
        api, state = self._send_contact(bot, 9_100_004, '+995 593 681 048')
        self.assertEqual(state.step, 'verified')
        identity = TelegramIdentity.query.filter_by(telegram_user_id=9_100_004).one()
        self.assertEqual(identity.phone_normalized, '995593681048')
        customer = db.session.get(CustomerAccount, identity.customer_id)
        self.assertEqual(customer.primary_phone, '995593681048')
        self.assertIn(COPY['fa']['verified'], [text for _, text in api.messages])

    def test_iranian_stays_canonical_when_flag_on(self):
        bot = self._bot(suffix='iran-on', allow_international=True)
        _, state = self._send_contact(bot, 9_100_005, '0912 345 6789')
        self.assertEqual(state.step, 'verified')
        identity = TelegramIdentity.query.filter_by(telegram_user_id=9_100_005).one()
        self.assertEqual(identity.phone_normalized, '989123456789')

    def test_garbage_rejected_with_intl_copy_when_flag_on(self):
        bot = self._bot(suffix='garbage-on', allow_international=True)
        api, _ = self._send_contact(bot, 9_100_006, '12345')
        self.assertEqual(api.messages[-1][1], COPY['fa']['phone_invalid_intl'])

    def test_phone_invalid_intl_copy_exists_in_both_languages(self):
        self.assertIn('phone_invalid_intl', COPY['fa'])
        self.assertIn('phone_invalid_intl', COPY['en'])
        self.assertTrue(COPY['fa']['phone_invalid_intl'])
        self.assertTrue(COPY['en']['phone_invalid_intl'])

    # ── ownership claim skipped for international numbers ────────────────

    def test_ownership_claim_skipped_for_international(self):
        bot = self._bot(suffix='claim-skip', allow_international=True)
        api = FakeBotApi()
        state = self._state(bot, 9_100_007)
        message = {
            'chat': {'id': 9_100_007},
            'contact': {'user_id': 9_100_007, 'phone_number': '+995 593 681 048'},
        }
        with mock.patch('telegram_bot_worker.discover_phone_ownership_claim') as discover:
            _handle_contact(api, bot, message, {'id': 9_100_007}, state)
        discover.assert_not_called()
        self.assertEqual(state.step, 'verified')

    def test_ownership_claim_runs_for_iranian(self):
        bot = self._bot(suffix='claim-run')
        api = FakeBotApi()
        state = self._state(bot, 9_100_008)
        message = {
            'chat': {'id': 9_100_008},
            'contact': {'user_id': 9_100_008, 'phone_number': '09123456789'},
        }
        with mock.patch('telegram_bot_worker.discover_phone_ownership_claim') as discover:
            discover.return_value = None
            _handle_contact(api, bot, message, {'id': 9_100_008}, state)
        discover.assert_called_once()
        self.assertEqual(state.step, 'verified')

    # ── storage gates ────────────────────────────────────────────────────

    def test_identity_gate_rejects_foreign_by_default(self):
        identity = TelegramIdentity(telegram_user_id=9_100_009)
        with self.assertRaises(ValueError):
            identity.set_verified_phone('995593681048')
        canonical = identity.set_verified_phone('995593681048', allow_international=True)
        self.assertEqual(canonical, '995593681048')
        self.assertEqual(identity.phone_normalized, '995593681048')

    def test_customer_gate_rejects_foreign_by_default(self):
        customer = CustomerAccount()
        with self.assertRaises(ValueError):
            customer.set_primary_phone('+995 593 681 048')
        canonical = customer.set_primary_phone('+995 593 681 048', allow_international=True)
        self.assertEqual(canonical, '995593681048')
        self.assertEqual(customer.primary_phone, '995593681048')

    def test_gates_keep_iranian_behavior(self):
        identity = TelegramIdentity(telegram_user_id=9_100_010)
        self.assertEqual(identity.set_verified_phone('09123456789'), '989123456789')
        customer = CustomerAccount()
        self.assertEqual(customer.set_primary_phone('+98 912 345 6789'), '989123456789')

    # ── settings round trip ──────────────────────────────────────────────

    def _save_settings(self, bot, payload):
        with app.test_request_context():
            result = _save_telegram_bot_settings(bot, payload)
        response, status = result if isinstance(result, tuple) else (result, 200)
        self.assertEqual(status, 200)
        return response.get_json()

    def test_settings_save_round_trip(self):
        bot = self._bot(suffix='settings')
        self.assertFalse(bot.phone_allow_international)
        payload = {
            'display_name': 'Phone Policy Bot',
            'enabled_languages': ['fa'],
            'default_language': 'fa',
            'phone_allow_international': True,
        }
        body = self._save_settings(bot, payload)
        self.assertTrue(body['success'])
        self.assertTrue(body['bot']['phone_allow_international'])
        db.session.flush()
        reloaded = db.session.get(TelegramBotInstance, bot.id)
        self.assertTrue(reloaded.phone_allow_international)

        payload['phone_allow_international'] = False
        body = self._save_settings(bot, payload)
        self.assertFalse(body['bot']['phone_allow_international'])
        db.session.flush()
        self.assertFalse(db.session.get(TelegramBotInstance, bot.id).phone_allow_international)


if __name__ == '__main__':
    unittest.main()
