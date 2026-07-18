import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    Admin,
    BankCard,
    CustomerAccount,
    Package,
    Server,
    ServiceOwnership,
    TelegramBotInstance,
    TelegramBotUserState,
    TelegramIdentity,
    TelegramPromo,
    TelegramPromoUse,
    TelegramPurchaseRequest,
    TelegramPurchaseSession,
    TelegramReferral,
    TelegramServiceRequest,
    app,
    db,
)
import telegram_bot_worker as worker  # noqa: E402
from telegram_bot_worker import (  # noqa: E402
    _begin_purchase_payment,
    _evaluate_promos,
    _handle_purchase_receipt,
    _qualify_referral,
    _record_referral,
)


class FakeBotApi:
    def __init__(self, member=True):
        self.messages = []
        self.member = member

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append({'chat_id': int(chat_id), 'text': text})
        return ({'ok': True}, 'direct')

    def call(self, method, payload=None, **kwargs):
        if method == 'getChatMember':
            return ({'status': 'member' if self.member else 'left'}, 'direct')
        return ({}, 'direct')

    def send_photo(self, *args, **kwargs):
        return ({'ok': True}, 'direct')

    def send_document(self, *args, **kwargs):
        return ({'ok': True}, 'direct')

    def answer_callback(self, *args, **kwargs):
        pass


class TelegramPromoTests(unittest.TestCase):
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
        worker._CHANNEL_MEMBER_CACHE.clear()
        TelegramPromoUse.query.delete()
        TelegramPromo.query.delete()
        TelegramReferral.query.delete()
        TelegramPurchaseSession.query.delete()
        TelegramPurchaseRequest.query.delete()
        TelegramServiceRequest.query.delete()
        TelegramBotUserState.query.delete()
        TelegramIdentity.query.delete()
        TelegramBotInstance.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        Package.query.delete()
        BankCard.query.delete()
        Server.query.delete()
        Admin.query.filter(Admin.username.like('promo-test-%')).delete(
            synchronize_session=False)
        db.session.commit()
        db.session.remove()

    def _admin(self, suffix, role='superadmin', **kwargs):
        kwargs.setdefault('is_superadmin', role == 'superadmin')
        admin = Admin(username=f'promo-test-{suffix}', role=role, enabled=True, **kwargs)
        admin.set_password('StrongPromoPassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _bot(self, suffix='bot', owner=None):
        bot = TelegramBotInstance(
            scope_key=f'promo-test-{suffix}-{id(object())}',
            owner_type='reseller' if owner else 'system',
            owner_admin_id=owner.id if owner else None,
            display_name='Promo Bot',
        )
        db.session.add(bot)
        db.session.flush()
        return bot

    def _package(self, price=100_000, **kwargs):
        package = Package(name=f'promo-pkg-{id(object())}', days=30, volume=10,
                          price=price, enabled=True, **kwargs)
        db.session.add(package)
        db.session.flush()
        return package

    def _promo(self, **kwargs):
        kwargs.setdefault('name', f'promo-{id(object())}')
        kwargs.setdefault('kind', 'percent')
        kwargs.setdefault('value', 10)
        promo = TelegramPromo(**kwargs)
        db.session.add(promo)
        db.session.flush()
        return promo

    def _evaluate(self, bot, package, **kwargs):
        kwargs.setdefault('user_id', 8_700_001)
        kwargs.setdefault('base_amount', package.price)
        kwargs.setdefault('applies_to', 'purchase')
        return _evaluate_promos(bot, package=package, **kwargs)

    # ── actions ──────────────────────────────────────────────────────────

    def test_percent_fixed_and_cap(self):
        bot = self._bot('actions')
        package = self._package()
        self._promo(value=10)
        final, discount, applied, err = self._evaluate(bot, package)
        self.assertEqual((final, discount), (90_000, 10_000))
        self.assertEqual(len(applied), 1)
        self.assertIsNone(err)
        db.session.rollback()
        TelegramPromo.query.delete()
        self._promo(kind='fixed', value=15_000)
        final, discount, _applied, _err = self._evaluate(bot, package)
        self.assertEqual((final, discount), (85_000, 15_000))
        db.session.rollback()
        TelegramPromo.query.delete()
        self._promo(value=50, max_discount_amount=20_000)
        final, discount, _applied, _err = self._evaluate(bot, package)
        self.assertEqual((final, discount), (80_000, 20_000))

    def test_min_amount_and_time_window(self):
        bot = self._bot('window')
        package = self._package()
        self._promo(min_amount=150_000)
        _final, discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual((discount, applied), (0, []))
        db.session.rollback()
        TelegramPromo.query.delete()
        self._promo(starts_at=datetime.utcnow() + timedelta(days=1))
        self._promo(ends_at=datetime.utcnow() - timedelta(days=1))
        _final, discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual((discount, applied), (0, []))

    def test_scope_bot_package_and_applies_to(self):
        bot = self._bot('scope')
        other_bot = self._bot('scope-other')
        package = self._package()
        other_package = self._package()
        self._promo(bot_instance_id=other_bot.id)
        self._promo(package_id=other_package.id)
        self._promo(applies_to='renewal')
        _final, discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual((discount, applied), (0, []))
        _final, discount, applied, _err = self._evaluate(bot, package, applies_to='renewal')
        self.assertEqual(len(applied), 1)

    def test_code_matching_and_invalid_code(self):
        bot = self._bot('code')
        package = self._package()
        self._promo(code='WELCOME10', value=20)
        self._promo(value=5)  # automatic
        final, discount, applied, err = self._evaluate(bot, package, code='welcome10')
        self.assertIsNone(err)
        # Best non-stackable wins: 20% beats 5%.
        self.assertEqual((final, discount), (80_000, 20_000))
        final, discount, applied, err = self._evaluate(bot, package, code='NOPE')
        self.assertEqual(err, 'invalid_code')
        self.assertEqual((final, discount), (100_000, 0))

    # ── conditions ───────────────────────────────────────────────────────

    def _completed_purchase(self, bot, user_id, days_ago=0):
        customer = CustomerAccount(primary_phone=f'98918{user_id % 10**6:06d}')
        server = Server(name=f'Promo Server {id(object())}', host='https://promo.test',
                        username='u', password='p')
        package = self._package()
        db.session.add_all([customer, server])
        db.session.flush()
        row = TelegramPurchaseRequest(
            bot_instance_id=bot.id, telegram_user_id=user_id, customer_id=customer.id,
            server_id=server.id, package_id=package.id, amount=100_000,
            receipt_file_id='f', source_chat_id=user_id, source_message_id=1,
            status='completed',
            created_at=datetime.utcnow() - timedelta(days=days_ago))
        db.session.add(row)
        db.session.flush()
        return row

    def test_first_purchase_only(self):
        bot = self._bot('first')
        package = self._package()
        self._promo(first_purchase_only=True, value=25)
        _final, discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual(discount, 25_000)
        self._completed_purchase(bot, 8_700_001)
        _final, discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual((discount, applied), (0, []))

    def test_purchase_count_windows(self):
        bot = self._bot('counts')
        package = self._package()
        self._promo(min_purchases_30d=1, value=10)
        self._promo(min_purchases_90d=1, value=20, stackable=True)
        self._completed_purchase(bot, 8_700_001, days_ago=40)
        _final, discount, applied, _err = self._evaluate(bot, package)
        # The 40-day-old purchase satisfies the 90d promo but not the 30d one.
        self.assertEqual([promo.value for promo, _d in applied], [20])
        self.assertEqual(discount, 20_000)

    # ── usage limits ─────────────────────────────────────────────────────

    def test_max_uses_total_and_per_user(self):
        bot = self._bot('limits')
        package = self._package()
        total_capped = self._promo(max_uses_total=1, value=10)
        user_capped = self._promo(max_uses_per_user=1, value=20, stackable=True)
        db.session.add(TelegramPromoUse(promo_id=total_capped.id, telegram_user_id=8_700_999))
        db.session.add(TelegramPromoUse(promo_id=user_capped.id, telegram_user_id=8_700_001))
        db.session.flush()
        _final, discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual((discount, applied), (0, []))

    # ── stacking ─────────────────────────────────────────────────────────

    def test_best_of_non_stackable_and_priority_tiebreak(self):
        bot = self._bot('stack')
        package = self._package()
        self._promo(value=10)
        self._promo(value=20)
        self._promo(value=5, stackable=True)
        final, discount, applied, _err = self._evaluate(bot, package)
        # 20% best-of, then 5% stackable on the remainder.
        self.assertEqual((final, discount), (76_000, 24_000))
        self.assertEqual([promo.value for promo, _d in applied], [20, 5])
        db.session.rollback()
        TelegramPromo.query.delete()
        low = self._promo(value=10, priority=1)
        high = self._promo(value=10, priority=9)
        _final, _discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual(applied[0][0].id, high.id)

    def test_apply_on_reseller_pricing_false(self):
        reseller = self._admin('reseller', role='reseller')
        bot = self._bot('reseller', owner=reseller)
        package = self._package()
        self._promo(value=10, apply_on_reseller_pricing=False)
        _final, discount, applied, _err = self._evaluate(bot, package, owner_id=reseller.id)
        self.assertEqual((discount, applied), (0, []))
        _final, discount, applied, _err = self._evaluate(bot, package, owner_id=None)
        self.assertEqual(discount, 10_000)

    # ── channel membership ───────────────────────────────────────────────

    def test_channel_join_and_once_per_user_ever(self):
        bot = self._bot('channel')
        package = self._package()
        promo = self._promo(value=15, requires_channel_chat_id=-1001234567890)
        api = FakeBotApi(member=True)
        _final, discount, applied, _err = self._evaluate(bot, package, api=api)
        self.assertEqual(discount, 15_000)
        worker._CHANNEL_MEMBER_CACHE.clear()
        non_member = FakeBotApi(member=False)
        _final, discount, applied, _err = self._evaluate(bot, package, api=non_member)
        self.assertEqual((discount, applied), (0, []))
        # After one recorded use, re-joining never grants it again.
        db.session.add(TelegramPromoUse(promo_id=promo.id, telegram_user_id=8_700_001))
        db.session.flush()
        _final, discount, applied, _err = self._evaluate(bot, package, api=api)
        self.assertEqual((discount, applied), (0, []))

    # ── referrals ────────────────────────────────────────────────────────

    def test_referral_record_self_unique_and_qualify(self):
        self.assertFalse(_record_referral(8_700_010, 8_700_010))  # self
        self.assertTrue(_record_referral(8_700_010, 8_700_011))
        # One referrer per referee.
        self.assertFalse(_record_referral(8_700_099, 8_700_011))
        referral = TelegramReferral.query.filter_by(
            referee_telegram_user_id=8_700_011).one()
        self.assertIsNone(referral.qualified_at)
        _qualify_referral(8_700_011)
        self.assertIsNotNone(referral.qualified_at)

    def test_min_referrals_condition(self):
        bot = self._bot('refcond')
        package = self._package()
        self._promo(min_referrals=1, value=30)
        _final, discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual((discount, applied), (0, []))
        _record_referral(8_700_001, 8_700_020)
        _qualify_referral(8_700_020)
        _final, discount, applied, _err = self._evaluate(bot, package)
        self.assertEqual(discount, 30_000)

    # ── amount freeze across payment → receipt ───────────────────────────

    def test_frozen_amount_survives_price_and_promo_changes(self):
        admin = self._admin('ops', telegram_id='555888')
        bot = self._bot('freeze')
        promo = self._promo(code='SAVE20', value=20)
        package = self._package(price=100_000)
        customer = CustomerAccount(primary_phone='989170005555',
                                   phone_verified_at=datetime.utcnow())
        server = Server(name='Freeze Server', host='https://freeze.test',
                        username='u', password='p')
        card = BankCard(label='freeze-card', is_active=True)
        db.session.add_all([customer, server, card])
        db.session.flush()
        identity = TelegramIdentity(
            telegram_user_id=8_700_030, telegram_chat_id=8_700_030,
            customer_id=customer.id, phone_verified_at=datetime.utcnow())
        state = TelegramBotUserState(
            bot_instance_id=bot.id, telegram_user_id=8_700_030, language='fa')
        session_row = TelegramPurchaseSession(
            bot_instance_id=bot.id, telegram_user_id=8_700_030,
            promo_code='SAVE20')
        db.session.add_all([identity, state, session_row])
        db.session.flush()

        api = FakeBotApi()
        _begin_purchase_payment(api, bot, 8_700_030, 8_700_030, 'fa', server, package, state)
        self.assertEqual(session_row.quoted_amount, 80_000)
        self.assertEqual(session_row.promo_id, promo.id)
        payment_text = api.messages[-1]['text']
        self.assertIn('<s>100,000</s>', payment_text)
        self.assertIn('80,000', payment_text)

        # Price and promo change between payment and receipt must not matter.
        package.price = 250_000
        promo.enabled = False
        db.session.flush()
        message = {
            'chat': {'id': 8_700_030}, 'message_id': 3,
            'photo': [{'file_size': 100, 'file_id': 'f-freeze',
                       'file_unique_id': 'uniq-freeze'}],
        }
        _handle_purchase_receipt(api, bot, message, {'id': 8_700_030}, state)
        request_row = TelegramPurchaseRequest.query.filter_by(
            telegram_user_id=8_700_030).one()
        self.assertEqual(request_row.amount, 80_000)
        self.assertEqual(request_row.original_amount, 100_000)
        self.assertEqual(request_row.discount_amount, 20_000)
        self.assertEqual(request_row.promo_code, 'SAVE20')
        use = TelegramPromoUse.query.filter_by(
            promo_id=promo.id, telegram_user_id=8_700_030).one()
        self.assertEqual(use.amount_discounted, 20_000)
        self.assertEqual(use.purchase_request_id, request_row.id)


if __name__ == '__main__':
    unittest.main()
