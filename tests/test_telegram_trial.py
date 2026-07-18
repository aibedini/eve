import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    Admin,
    CustomerAccount,
    Package,
    Server,
    ServiceOwnership,
    TelegramBotInstance,
    TelegramBotUserState,
    TelegramIdentity,
    TelegramPurchasePolicy,
    TelegramPurchaseRequest,
    TelegramTrialGrant,
    app,
    db,
)
from telegram_bot_runtime import COPY, main_menu_keyboard  # noqa: E402
from telegram_bot_worker import (  # noqa: E402
    _grant_emergency_access,
    _start_trial,
    _trial_grant_for,
)


class FakeBotApi:
    def __init__(self, member=True):
        self.messages = []
        self.answers = []
        self.member = member

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((int(chat_id), text))
        return ({'ok': True}, 'direct')

    def call(self, method, payload=None, **kwargs):
        if method == 'getChatMember':
            return ({'status': 'member' if self.member else 'left'}, 'direct')
        return ({}, 'direct')

    def answer_callback(self, callback_id, text=None):
        self.answers.append((callback_id, text))

    def send_photo(self, *args, **kwargs):
        return ({'ok': True}, 'direct')

    def send_document(self, *args, **kwargs):
        return ({'ok': True}, 'direct')


class TelegramTrialTests(unittest.TestCase):
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
        TelegramTrialGrant.query.delete()
        TelegramPurchaseRequest.query.delete()
        TelegramPurchasePolicy.query.delete()
        TelegramBotUserState.query.delete()
        TelegramIdentity.query.delete()
        TelegramBotInstance.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        Package.query.delete()
        Server.query.delete()
        Admin.query.filter(Admin.username.like('trial-test-%')).delete(
            synchronize_session=False)
        db.session.commit()

    def _admin(self, suffix, role='superadmin', **kwargs):
        kwargs.setdefault('is_superadmin', role == 'superadmin')
        admin = Admin(username=f'trial-test-{suffix}', role=role, enabled=True, **kwargs)
        admin.set_password('StrongTrialPassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _bot(self, owner=None, suffix='bot'):
        bot = TelegramBotInstance(
            scope_key=f'trial-test-{suffix}-{id(object())}',
            owner_type='reseller' if owner else 'system',
            owner_admin_id=owner.id if owner else None,
            display_name='Trial Bot',
        )
        db.session.add(bot)
        db.session.flush()
        return bot

    def _package(self, name='trial', **kwargs):
        kwargs.setdefault('is_trial', True)
        kwargs.setdefault('price', 0)
        package = Package(
            name=name, days=3, volume=1, enabled=True, **kwargs)
        db.session.add(package)
        db.session.flush()
        return package

    def _policy(self, bot, **kwargs):
        policy = TelegramPurchasePolicy(bot_instance_id=bot.id, **kwargs)
        db.session.add(policy)
        db.session.flush()
        return policy

    def _identity(self, user_id, phone, verified=True):
        self._customer_seq = getattr(self, '_customer_seq', 0) + 1
        customer = CustomerAccount(primary_phone=f'98917{self._customer_seq:06d}')
        db.session.add(customer)
        db.session.flush()
        identity = TelegramIdentity(
            telegram_user_id=user_id,
            telegram_chat_id=user_id,
            customer_id=customer.id,
            phone_normalized=phone,
            phone_verified_at=datetime.utcnow() if verified else None,
        )
        db.session.add(identity)
        db.session.flush()
        return customer, identity

    def _state(self, bot, user_id):
        state = TelegramBotUserState(
            bot_instance_id=bot.id, telegram_user_id=user_id, language='fa')
        db.session.add(state)
        db.session.flush()
        return state

    def _trial_call(self, bot, user_id=8_500_001, phone='9120001111',
                    verified=True, member=True):
        api = FakeBotApi(member=member)
        customer, identity = self._identity(user_id, phone, verified=verified)
        state = self._state(bot, user_id)
        with mock.patch('telegram_bot_worker._assign_purchase_server') as assign, \
                mock.patch('telegram_bot_worker._execute_purchase_request') as execute:
            server = Server(
                name='Trial Server', host='https://trial.test',
                username='u', password='p')
            db.session.add(server)
            db.session.flush()
            assign.return_value = server
            execute.return_value = (True, {
                'client': {'dashboard_link': 'https://dash.test/s/1'},
                'ownership_id': None,
            })
            _start_trial(api, bot, user_id, user_id, state)
        return api, execute

    # ── policy gating ────────────────────────────────────────────────────

    def test_trial_button_follows_policy(self):
        keyboard = main_menu_keyboard('fa', show_trial=True)
        buttons = {btn['text'] for row in keyboard['keyboard'] for btn in row}
        self.assertIn(COPY['fa']['menu_trial'], buttons)
        keyboard = main_menu_keyboard('fa')
        buttons = {btn['text'] for row in keyboard['keyboard'] for btn in row}
        self.assertNotIn(COPY['fa']['menu_trial'], buttons)

    def test_trial_rejected_when_policy_disabled(self):
        self._admin('super')
        bot = self._bot(suffix='disabled')
        package = self._package()
        self._policy(bot, trial_enabled=False, trial_package_id=package.id)
        api, execute = self._trial_call(bot)
        self.assertEqual(api.messages[-1][1], COPY['fa']['trial_unavailable'])
        execute.assert_not_called()
        self.assertEqual(TelegramTrialGrant.query.count(), 0)

    def test_trial_rejected_for_non_trial_package(self):
        self._admin('super')
        bot = self._bot(suffix='nontrial')
        package = self._package(is_trial=False, price=100_000)
        self._policy(bot, trial_enabled=True, trial_package_id=package.id)
        api, execute = self._trial_call(bot)
        self.assertEqual(api.messages[-1][1], COPY['fa']['trial_unavailable'])
        execute.assert_not_called()

    def test_trial_requires_verified_phone(self):
        self._admin('super')
        bot = self._bot(suffix='unverified')
        package = self._package()
        self._policy(bot, trial_enabled=True, trial_package_id=package.id)
        api, execute = self._trial_call(bot, verified=False)
        self.assertEqual(api.messages[-1][1], COPY['fa']['share_phone'])
        execute.assert_not_called()

    def test_trial_can_require_telegram_channel_membership(self):
        self._admin('super')
        bot = self._bot(suffix='channel')
        package = self._package()
        self._policy(
            bot, trial_enabled=True, trial_package_id=package.id,
            trial_requires_channel_membership=True,
            trial_channel_chat_id=-1001234567890,
        )
        api, execute = self._trial_call(bot, member=False)
        self.assertIn('کانال تلگرام', api.messages[-1][1])
        execute.assert_not_called()
        self.assertEqual(TelegramTrialGrant.query.count(), 0)

        api2, execute2 = self._trial_call(
            bot, user_id=8_500_002, phone='9120002222', member=True)
        self.assertEqual(execute2.call_count, 1)
        self.assertIn(COPY['fa']['trial_success'].split('{')[0], api2.messages[-1][1])

    # ── one trial per phone ──────────────────────────────────────────────

    def test_trial_success_then_same_phone_blocked_across_accounts(self):
        self._admin('super')
        bot = self._bot(suffix='once')
        package = self._package()
        self._policy(bot, trial_enabled=True, trial_package_id=package.id)

        api, execute = self._trial_call(bot, user_id=8_500_001, phone='9120001111')
        self.assertEqual(execute.call_count, 1)
        self.assertIn(COPY['fa']['trial_success'].split('{')[0], api.messages[-1][1])
        self.assertEqual(TelegramTrialGrant.query.filter_by(kind='trial').count(), 1)
        grant = TelegramTrialGrant.query.filter_by(kind='trial').one()
        self.assertEqual(grant.phone_normalized, '9120001111')
        self.assertEqual(grant.bot_instance_id, bot.id)

        # A different telegram account with the same phone is rejected.
        api2, execute2 = self._trial_call(bot, user_id=8_500_002, phone='9120001111')
        self.assertEqual(api2.messages[-1][1], COPY['fa']['trial_already_used'])
        execute2.assert_not_called()
        # The same telegram account is rejected even with another phone.
        identity = TelegramIdentity.query.filter_by(telegram_user_id=8_500_001).one()
        identity.phone_normalized = '9120002222'
        db.session.flush()
        state = TelegramBotUserState.query.filter_by(
            bot_instance_id=bot.id, telegram_user_id=8_500_001).one()
        api3 = FakeBotApi()
        with mock.patch('telegram_bot_worker._execute_purchase_request') as execute3:
            _start_trial(api3, bot, 8_500_001, 8_500_001, state)
        self.assertEqual(api3.messages[-1][1], COPY['fa']['trial_already_used'])
        execute3.assert_not_called()
        self.assertEqual(TelegramTrialGrant.query.filter_by(kind='trial').count(), 1)

    def test_trial_grant_lookup_scoped_per_bot(self):
        bot = self._bot(suffix='ledger')
        other = self._bot(suffix='ledger-other')
        db.session.add(TelegramTrialGrant(
            bot_instance_id=other.id, telegram_user_id=8_500_010,
            phone_normalized='9120003333', kind='trial'))
        db.session.flush()
        self.assertIsNone(_trial_grant_for(bot, 8_500_010, '9120003333'))
        self.assertIsNotNone(_trial_grant_for(other, 8_500_010, '9120003333'))

    # ── emergency access ─────────────────────────────────────────────────

    def _emergency_setup(self, cooldown_days=30):
        self._admin('super')
        bot = self._bot(suffix='emg')
        policy = self._policy(
            bot, emergency_enabled=True, emergency_days=1,
            emergency_volume_gb=1, emergency_cooldown_days=cooldown_days)
        customer, identity = self._identity(8_500_020, '9120004444')
        server = Server(
            name='Emg Server', host='https://emg.test', username='u', password='p')
        db.session.add(server)
        db.session.flush()
        ownership = ServiceOwnership(
            customer_id=customer.id, server_id=server.id,
            client_uuid='trial-test-emg-uuid',
            client_email_snapshot='emg@example.com')
        db.session.add(ownership)
        db.session.flush()
        return bot, policy, ownership

    def _emergency_call(self, bot, ownership):
        api = FakeBotApi()
        callback = {
            'id': 'cb1',
            'from': {'id': 8_500_020},
            'message': {'chat': {'id': 8_500_020}},
        }
        with mock.patch('telegram_bot_worker._execute_emergency_renewal') as renew:
            renew.return_value = (True, {'success': True})
            _grant_emergency_access(api, bot, callback, ownership.id)
        return api, renew

    def test_emergency_grant_then_cooldown_block(self):
        bot, policy, ownership = self._emergency_setup()
        api, renew = self._emergency_call(bot, ownership)
        self.assertEqual(renew.call_count, 1)
        self.assertIn(COPY['fa']['emergency_success'].split('{')[0], api.messages[-2][1])
        grant = TelegramTrialGrant.query.filter_by(kind='emergency').one()
        self.assertEqual(grant.ownership_id, ownership.id)

        # Second attempt inside the cooldown window is blocked.
        api2, renew2 = self._emergency_call(bot, ownership)
        self.assertEqual(api2.messages[-1][1], COPY['fa']['emergency_cooldown'])
        renew2.assert_not_called()

        # An old grant outside the window no longer blocks.
        grant.created_at = datetime.utcnow() - timedelta(days=31)
        db.session.flush()
        api3, renew3 = self._emergency_call(bot, ownership)
        self.assertEqual(renew3.call_count, 1)

    def test_emergency_rejected_when_policy_disabled(self):
        bot, policy, ownership = self._emergency_setup()
        policy.emergency_enabled = False
        db.session.flush()
        api, renew = self._emergency_call(bot, ownership)
        self.assertEqual(api.messages[-1][1], COPY['fa']['emergency_unavailable'])
        renew.assert_not_called()
        self.assertEqual(TelegramTrialGrant.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
