import os
import tempfile
import unittest
from datetime import datetime

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
    TelegramPurchaseRequest,
    _telegram_purchase_visible_to,
    app,
    db,
)
from telegram_bot_worker import (  # noqa: E402
    _available_packages,
    _purchase_admins,
    _purchase_card,
    _purchase_card_accessible,
    _purchase_packages,
    _purchase_reviewer,
    _resolve_purchase_price,
)


class ResellerScopingTests(unittest.TestCase):
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
        TelegramPurchaseRequest.query.delete()
        TelegramBotInstance.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        Package.query.delete()
        BankCard.query.delete()
        Server.query.delete()
        Admin.query.filter(Admin.username.like('scope-test-%')).delete(
            synchronize_session=False)
        db.session.commit()

    def _admin(self, suffix, role='reseller', **kwargs):
        admin = Admin(username=f'scope-test-{suffix}', role=role, enabled=True, **kwargs)
        admin.set_password('StrongScopePassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _bot(self, owner=None, suffix='bot'):
        bot = TelegramBotInstance(
            scope_key=f'scope-test-{suffix}-{id(object())}',
            owner_type='reseller' if owner else 'system',
            owner_admin_id=owner.id if owner else None,
            display_name='Scope Test Bot',
        )
        db.session.add(bot)
        db.session.flush()
        return bot

    def _card(self, label, **kwargs):
        card = BankCard(label=label, is_active=True, **kwargs)
        db.session.add(card)
        db.session.flush()
        return card

    def _package(self, name, price, **kwargs):
        package = Package(
            name=name, days=30, volume=10, price=price, enabled=True, **kwargs)
        db.session.add(package)
        db.session.flush()
        return package

    # ── _purchase_card priorities ────────────────────────────────────────

    def test_system_bot_prefers_central_card(self):
        central = self._card('central')
        reseller = self._admin('owncard')
        owned = self._card('reseller-owned', reseller_id=reseller.id)
        bot = self._bot()
        self.assertEqual(_purchase_card(bot).id, central.id)
        # Without any central card, fall back to the first active card.
        central.is_active = False
        db.session.flush()
        self.assertEqual(_purchase_card(bot).id, owned.id)

    def test_reseller_bot_prefers_own_card(self):
        reseller = self._admin('own')
        self._card('central')
        owned = self._card('owned', reseller_id=reseller.id)
        bot = self._bot(owner=reseller, suffix='own')
        self.assertEqual(_purchase_card(bot).id, owned.id)

    def test_reseller_bot_uses_assigned_card_before_central(self):
        reseller = self._admin('assigned')
        other = self._admin('other')
        self._card('central')
        assigned = self._card(
            'assigned', reseller_id=other.id,
            assigned_reseller_ids=f'[{reseller.id}]')
        bot = self._bot(owner=reseller, suffix='assigned')
        self.assertEqual(_purchase_card(bot).id, assigned.id)
        self.assertTrue(_purchase_card_accessible(assigned, bot))

    def test_reseller_bot_falls_back_to_central(self):
        reseller = self._admin('fallback')
        other = self._admin('fallback-other')
        central = self._card('central')
        foreign = self._card('foreign', reseller_id=other.id)
        bot = self._bot(owner=reseller, suffix='fallback')
        self.assertEqual(_purchase_card(bot).id, central.id)
        self.assertFalse(_purchase_card_accessible(foreign, bot))

    # ── package visibility + reseller pricing ────────────────────────────

    def test_purchase_packages_visibility_and_price(self):
        reseller = self._admin('pkg', discount_percent=10)
        other = self._admin('pkg-other')
        global_pkg = self._package('global', 100_000, reseller_price=80_000)
        assigned_pkg = self._package(
            'assigned', 100_000, scope='assigned',
            assigned_reseller_ids=f'[{reseller.id}]')
        personal_pkg = self._package(
            'personal', 100_000, scope='personal', created_by=reseller.id)
        hidden_pkg = self._package(
            'hidden', 100_000, scope='assigned', assigned_reseller_ids='[]')
        bot = self._bot(owner=reseller, suffix='pkg')
        visible = {pkg.id for pkg in _purchase_packages(bot)}
        self.assertIn(global_pkg.id, visible)
        self.assertIn(assigned_pkg.id, visible)
        self.assertIn(personal_pkg.id, visible)
        self.assertNotIn(hidden_pkg.id, visible)
        # reseller_price override beats the discount
        self.assertEqual(_resolve_purchase_price(reseller.id, global_pkg), 80_000)
        # discount applies when no reseller_price is set
        self.assertEqual(_resolve_purchase_price(reseller.id, assigned_pkg), 90_000)
        # system bot: base price, global-only visibility
        system_bot = self._bot(suffix='pkg-system')
        self.assertEqual(_resolve_purchase_price(None, global_pkg), 100_000)
        system_visible = {pkg.id for pkg in _purchase_packages(system_bot)}
        self.assertEqual(system_visible, {global_pkg.id})

    def test_available_packages_includes_personal_for_reseller(self):
        reseller = self._admin('renew')
        personal_pkg = self._package(
            'personal', 100_000, scope='personal', created_by=reseller.id)
        global_pkg = self._package('global', 100_000)
        ownership = ServiceOwnership(
            customer=CustomerAccount(primary_phone='989120001111'),
            server=Server(
                name='Scope Test Server', host='https://scope.test',
                username='u', password='p'),
            client_uuid='scope-test-uuid',
            reseller_id=reseller.id,
        )
        db.session.add(ownership)
        db.session.flush()
        visible = {pkg.id for pkg in _available_packages(ownership)}
        self.assertEqual(visible, {global_pkg.id, personal_pkg.id})

    # ── bank card list scoping ───────────────────────────────────────────

    def test_list_bank_cards_scopes_non_superadmin(self):
        superadmin = self._admin(
            'cards-super', role='superadmin', is_superadmin=True)
        reseller = self._admin('cards-reseller')
        other = self._admin('cards-other')
        central = self._card('central')
        owned = self._card('owned', reseller_id=reseller.id)
        assigned = self._card(
            'assigned', reseller_id=other.id,
            assigned_reseller_ids=f'[{reseller.id}]')
        foreign = self._card('foreign', reseller_id=other.id)
        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = reseller.id
        payload = client.get('/api/bank-cards').get_json()
        self.assertTrue(payload['success'])
        visible = {card['label'] for card in payload['cards']}
        self.assertEqual(visible, {central.label, owned.label, assigned.label})
        self.assertNotIn(foreign.label, visible)

        with client.session_transaction() as session_data:
            session_data['admin_id'] = superadmin.id
        payload = client.get('/api/bank-cards').get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(len(payload['cards']), 4)

    def test_create_bank_card_reseller_fields_superadmin_only(self):
        superadmin = self._admin(
            'create-super', role='superadmin', is_superadmin=True)
        reseller = self._admin('create-reseller')
        other = self._admin('create-other', role='admin')
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = superadmin.id
        created = client.post('/api/bank-cards', json={
            'label': 'owned-card',
            'reseller_id': reseller.id,
            'assigned_reseller_ids': [other.id],
        }).get_json()
        self.assertTrue(created['success'])
        self.assertEqual(created['card']['reseller_id'], reseller.id)
        self.assertEqual(created['card']['assigned_reseller_ids'], [other.id])
        # reseller_id must reference a reseller
        rejected = client.post('/api/bank-cards', json={
            'label': 'bad-owner', 'reseller_id': other.id,
        }).get_json()
        self.assertFalse(rejected['success'])
        # Non-superadmin silently cannot set ownership fields.
        with client.session_transaction() as session_data:
            session_data['admin_id'] = other.id
        ignored = client.post('/api/bank-cards', json={
            'label': 'plain-card', 'reseller_id': reseller.id,
        }).get_json()
        self.assertTrue(ignored['success'])
        self.assertIsNone(ignored['card']['reseller_id'])

    # ── operator visibility for bot-owner reseller ───────────────────────

    def test_purchase_visible_to_bot_owner_reseller(self):
        reseller = self._admin('visible', telegram_id='5550001')
        outsider = self._admin('visible-outsider')
        bot = self._bot(owner=reseller, suffix='visible')
        customer = CustomerAccount(primary_phone='989120002222')
        server = Server(
            name='Scope Visible Server', host='https://visible.test',
            username='u', password='p')
        package = self._package('visible', 100_000)
        db.session.add_all([customer, server])
        db.session.flush()
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id,
            telegram_user_id=8_100_001,
            customer_id=customer.id,
            server_id=server.id,
            package_id=package.id,
            amount=100_000,
            receipt_file_id='file-id',
            source_chat_id=8_100_001,
            source_message_id=1,
            status='pending',
        )
        db.session.add(request_row)
        db.session.flush()
        self.assertTrue(_telegram_purchase_visible_to(reseller, request_row))
        self.assertFalse(_telegram_purchase_visible_to(outsider, request_row))
        # The reseller owner can review the receipt inside Telegram and is
        # notified alongside global admins.
        self.assertEqual(
            _purchase_reviewer(5550001, request_row).id, reseller.id)
        self.assertIsNone(_purchase_reviewer(5550002, request_row))
        self.assertIn(reseller.id, {a.id for a in _purchase_admins(request_row)})


if __name__ == '__main__':
    unittest.main()
