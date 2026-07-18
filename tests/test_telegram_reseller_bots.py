import os
import tempfile
import unittest
from datetime import datetime

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
import base64  # noqa: E402
os.environ['SERVER_PASSWORD_KEY'] = base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode()

from app import (  # noqa: E402
    Admin,
    BankCard,
    CustomerAccount,
    Package,
    Server,
    ServiceOwnership,
    TelegramBotInstance,
    TelegramBotRuntime,
    TelegramIdentity,
    TelegramPurchaseRequest,
    _telegram_bot_health,
    _telegram_bot_manageable_by,
    app,
    db,
)
from datetime import timedelta  # noqa: E402
from telegram_bot_worker import (  # noqa: E402
    _brand_text,
    _effective_owner_id,
    _purchase_card,
    _purchase_packages,
    _resolve_purchase_price,
)


class ResellerBotTests(unittest.TestCase):
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
        TelegramIdentity.query.delete()
        TelegramBotRuntime.query.delete()
        TelegramBotInstance.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        Package.query.delete()
        BankCard.query.delete()
        Server.query.delete()
        Admin.query.filter(Admin.username.like('bot-test-%')).delete(
            synchronize_session=False)
        db.session.commit()

    def _admin(self, suffix, role='reseller', **kwargs):
        admin = Admin(username=f'bot-test-{suffix}', role=role, enabled=True, **kwargs)
        admin.set_password('StrongBotPassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _bot(self, owner=None, suffix='bot', **kwargs):
        bot = TelegramBotInstance(
            scope_key=f'bot-test-{suffix}-{id(object())}',
            owner_type='reseller' if owner else 'system',
            owner_admin_id=owner.id if owner else None,
            display_name=kwargs.pop('display_name', 'Reseller Shop Bot'),
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

    # ── permission helper ────────────────────────────────────────────────

    def test_manageable_by(self):
        superadmin = self._admin('super', role='superadmin', is_superadmin=True)
        reseller = self._admin('owner')
        outsider = self._admin('outsider')
        bot = self._bot(owner=reseller, suffix='perm')
        central = self._bot(suffix='central')
        self.assertTrue(_telegram_bot_manageable_by(superadmin, bot))
        self.assertTrue(_telegram_bot_manageable_by(reseller, bot))
        self.assertFalse(_telegram_bot_manageable_by(outsider, bot))
        self.assertFalse(_telegram_bot_manageable_by(reseller, central))
        self.assertFalse(_telegram_bot_manageable_by(None, bot))
        self.assertFalse(_telegram_bot_manageable_by(reseller, None))

    def test_settings_routes_bot_id_access(self):
        superadmin = self._admin('super2', role='superadmin', is_superadmin=True)
        reseller = self._admin('owner2')
        outsider = self._admin('outsider2')
        bot = self._bot(owner=reseller, suffix='access')
        db.session.commit()

        # Reseller can read and save only their own bot.
        client = self._client(reseller)
        payload = client.get(f'/api/settings/telegram-bots?bot_id={bot.id}').get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['bot']['id'], bot.id)
        saved = client.post(
            f'/api/settings/telegram-bots?bot_id={bot.id}',
            json={'display_name': 'My Shop'},
        ).get_json()
        self.assertTrue(saved['success'])
        self.assertEqual(saved['bot']['display_name'], 'My Shop')
        # Without bot_id the central bot is targeted -> 403 for a reseller.
        self.assertEqual(client.get('/api/settings/telegram-bots').status_code, 403)

        # Another reseller is rejected with 403 on GET and POST.
        other_client = self._client(outsider)
        self.assertEqual(
            other_client.get(f'/api/settings/telegram-bots?bot_id={bot.id}').status_code, 403)
        self.assertEqual(
            other_client.post(
                f'/api/settings/telegram-bots?bot_id={bot.id}',
                json={'display_name': 'Hijack'}).status_code, 403)

        # Superadmin can manage any bot, with or without bot_id.
        super_client = self._client(superadmin)
        self.assertTrue(super_client.get(
            f'/api/settings/telegram-bots?bot_id={bot.id}').get_json()['success'])
        central = super_client.get('/api/settings/telegram-bots').get_json()
        self.assertTrue(central['success'])
        self.assertEqual(central['bot']['scope_key'], 'system')

    # ── CRUD /api/telegram-bots ──────────────────────────────────────────

    def test_create_bot_self_and_duplicate_scope_key(self):
        reseller = self._admin('self')
        db.session.commit()
        client = self._client(reseller)
        created = client.post('/api/telegram-bots', json={
            'display_name': 'Self Bot',
            'bot_token': '77001122:ABCDEFGHIJKLMNOPQRSTUVWX',
        })
        self.assertEqual(created.status_code, 201)
        bot = created.get_json()['bot']
        self.assertEqual(bot['scope_key'], f'reseller:{reseller.id}')
        self.assertEqual(bot['owner_type'], 'reseller')
        self.assertEqual(bot['owner_admin_id'], reseller.id)
        self.assertEqual(bot['transport_mode'], 'polling')
        # Second bot for the same reseller -> 409.
        duplicate = client.post('/api/telegram-bots', json={'display_name': 'Again'})
        self.assertFalse(duplicate.get_json()['success'])
        self.assertEqual(duplicate.headers.get('X-Eve-Status'), '409')
        # The reseller sees only their own bot in the list.
        listing = client.get('/api/telegram-bots').get_json()
        self.assertEqual([row['id'] for row in listing['bots']], [bot['id']])

    def test_create_bot_enabled_requires_token(self):
        reseller = self._admin('notoken')
        db.session.commit()
        client = self._client(reseller)
        response = client.post('/api/telegram-bots', json={'enabled': True})
        self.assertFalse(response.get_json()['success'])
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')

    def test_create_bot_superadmin_for_reseller(self):
        superadmin = self._admin('super3', role='superadmin', is_superadmin=True)
        reseller = self._admin('target')
        db.session.commit()
        client = self._client(superadmin)
        created = client.post('/api/telegram-bots', json={'owner_admin_id': reseller.id})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()['bot']['display_name'], reseller.username)
        # Superadmin listing includes runtime info.
        listing = client.get('/api/telegram-bots').get_json()
        self.assertTrue(all('runtime' in row for row in listing['bots']))

    def test_duplicate_token_rejected(self):
        reseller = self._admin('tok1')
        other = self._admin('tok2')
        existing = self._bot(owner=other, suffix='tok-existing', bot_user_id=77001122)
        db.session.commit()
        # Create: token prefix collides with another instance's bot_user_id.
        client = self._client(reseller)
        response = client.post('/api/telegram-bots', json={
            'bot_token': '77001122:ZYXWVUTSRQPONMLKJIHGFEDCBA',
        })
        self.assertFalse(response.get_json()['success'])
        self.assertEqual(response.headers.get('X-Eve-Status'), '409')
        # Save on an existing bot: same collision is rejected.
        bot = self._bot(owner=reseller, suffix='tok-save')
        db.session.commit()
        saved = client.post(
            f'/api/settings/telegram-bots?bot_id={bot.id}',
            json={'bot_token': '77001122:ZYXWVUTSRQPONMLKJIHGFEDCBA'},
        )
        self.assertFalse(saved.get_json()['success'])
        self.assertEqual(saved.headers.get('X-Eve-Status'), '409')
        self.assertIsNone(db.session.get(TelegramBotInstance, bot.id).token_encrypted)

    # ── central-bot ownership fallback ───────────────────────────────────

    def _customer_with_ownership(self, reseller, telegram_user_id):
        customer = CustomerAccount(primary_phone='989130001111')
        server = Server(
            name='Bot Test Server', host='https://bot.test',
            username='u', password='p')
        db.session.add_all([customer, server])
        db.session.flush()
        identity = TelegramIdentity(
            telegram_user_id=telegram_user_id, customer_id=customer.id)
        ownership = ServiceOwnership(
            customer_id=customer.id,
            server_id=server.id,
            client_uuid=f'bot-test-uuid-{telegram_user_id}',
            reseller_id=reseller.id,
        )
        db.session.add_all([identity, ownership])
        db.session.flush()
        return customer, identity, ownership

    def test_effective_owner_id_fallback(self):
        reseller = self._admin('fallback')
        central = self._bot(suffix='fallback-central')
        _customer, _identity, ownership = self._customer_with_ownership(reseller, 8_200_001)
        # Central bot: falls back to the reseller of the user's ownership.
        self.assertEqual(_effective_owner_id(central, 8_200_001), reseller.id)
        # Reseller-owned bot: the bot owner always wins.
        own = self._bot(owner=reseller, suffix='fallback-own')
        self.assertEqual(_effective_owner_id(own, 8_200_001), reseller.id)
        # Unknown telegram user, and revoked ownership -> global (None).
        self.assertIsNone(_effective_owner_id(central, 8_200_999))
        ownership.revoked_at = datetime.utcnow()
        db.session.flush()
        self.assertIsNone(_effective_owner_id(central, 8_200_001))

    def test_fallback_scopes_packages_card_and_price(self):
        reseller = self._admin('scoped', discount_percent=10)
        central = self._bot(suffix='scoped-central')
        self._customer_with_ownership(reseller, 8_200_002)
        global_pkg = Package(
            name='global', days=30, volume=10, price=100_000,
            reseller_price=80_000, enabled=True)
        assigned_pkg = Package(
            name='assigned', days=30, volume=10, price=100_000, enabled=True,
            scope='assigned', assigned_reseller_ids=f'[{reseller.id}]')
        central_card = BankCard(label='central', is_active=True)
        owned_card = BankCard(label='owned', is_active=True, reseller_id=reseller.id)
        db.session.add_all([global_pkg, assigned_pkg, central_card, owned_card])
        db.session.flush()

        owner_id = _effective_owner_id(central, 8_200_002)
        self.assertEqual(owner_id, reseller.id)
        visible = {pkg.id for pkg in _purchase_packages(central, owner_id=owner_id)}
        self.assertEqual(visible, {global_pkg.id, assigned_pkg.id})
        self.assertEqual(_purchase_card(central, owner_id=owner_id).id, owned_card.id)
        self.assertEqual(_resolve_purchase_price(owner_id, global_pkg), 80_000)
        self.assertEqual(_resolve_purchase_price(owner_id, assigned_pkg), 90_000)

        # Without reseller ownership the central bot keeps global behavior.
        self.assertIsNone(_effective_owner_id(central, 8_200_003))
        global_visible = {pkg.id for pkg in _purchase_packages(central, owner_id=None)}
        self.assertEqual(global_visible, {global_pkg.id})
        self.assertEqual(_purchase_card(central, owner_id=None).id, central_card.id)

    # ── branding ─────────────────────────────────────────────────────────

    def test_brand_text(self):
        reseller = self._admin('brand')
        bot = self._bot(owner=reseller, suffix='brand', display_name='Shop Bot')
        central = self._bot(suffix='brand-central')
        branded = _brand_text(bot, 'fa')
        self.assertIn('Shop Bot', branded)
        self.assertEqual(_brand_text(central, 'fa'), '')


class ResellerBotLifecycleTests(ResellerBotTests):
    def _runtime(self, bot, **kwargs):
        runtime = TelegramBotRuntime(bot_instance_id=bot.id, **kwargs)
        db.session.add(runtime)
        db.session.flush()
        return runtime

    def _pending_purchase(self, bot):
        customer = CustomerAccount(primary_phone='989140001111')
        server = Server(
            name='Lifecycle Server', host='https://lifecycle.test',
            username='u', password='p')
        package = Package(name='lifecycle', days=30, volume=10, price=100_000, enabled=True)
        db.session.add_all([customer, server, package])
        db.session.flush()
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id,
            telegram_user_id=8_300_001,
            customer_id=customer.id,
            server_id=server.id,
            package_id=package.id,
            amount=100_000,
            receipt_file_id='file-id',
            source_chat_id=8_300_001,
            source_message_id=1,
            status='pending',
        )
        db.session.add(request_row)
        db.session.flush()
        return request_row

    # ── runtime controls ─────────────────────────────────────────────────

    def test_enable_disable_restart_permissions_and_effects(self):
        superadmin = self._admin('lc-super', role='superadmin', is_superadmin=True)
        reseller = self._admin('lc-owner')
        outsider = self._admin('lc-outsider')
        bot = self._bot(owner=reseller, suffix='lc', token_encrypted='enc-token')
        runtime = self._runtime(
            bot, worker_id='worker-1', status='error', failed_update_count=3,
            lease_expires_at=datetime.utcnow() + timedelta(seconds=60))
        db.session.commit()

        # Outsider reseller cannot control the bot.
        other_client = self._client(outsider)
        for action in ('enable', 'disable', 'restart'):
            self.assertEqual(
                other_client.post(
                    f'/api/telegram-bots/{bot.id}/runtime',
                    json={'action': action}).status_code, 403)

        client = self._client(reseller)
        enabled = client.post(
            f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'enable'}).get_json()
        self.assertTrue(enabled['success'])
        self.assertTrue(enabled['bot']['enabled'])
        disabled = client.post(
            f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'disable'}).get_json()
        self.assertTrue(disabled['success'])
        self.assertFalse(disabled['bot']['enabled'])

        # Restart clears the lease and failure state.
        restarted = client.post(
            f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'restart'}).get_json()
        self.assertTrue(restarted['success'])
        db.session.expire_all()
        runtime = db.session.get(TelegramBotRuntime, runtime.id)
        self.assertIsNone(runtime.worker_id)
        self.assertIsNone(runtime.lease_expires_at)
        self.assertEqual(runtime.status, 'stopped')
        self.assertEqual(runtime.failed_update_count, 0)

        # Superadmin may control the reseller's bot too.
        super_client = self._client(superadmin)
        self.assertTrue(super_client.post(
            f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'enable'}).get_json()['success'])

    def test_enable_requires_token(self):
        reseller = self._admin('lc-notoken')
        bot = self._bot(owner=reseller, suffix='lc-notoken')
        db.session.commit()
        client = self._client(reseller)
        response = client.post(f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'enable'})
        self.assertFalse(response.get_json()['success'])
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')

    # ── archive / restore ────────────────────────────────────────────────

    def test_archive_blocked_by_pending_requests(self):
        superadmin = self._admin('ar-super', role='superadmin', is_superadmin=True)
        reseller = self._admin('ar-owner')
        bot = self._bot(owner=reseller, suffix='ar-pending')
        self._pending_purchase(bot)
        db.session.commit()
        client = self._client(superadmin)
        response = client.post(f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'archive'})
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(response.headers.get('X-Eve-Status'), '409')
        self.assertEqual(payload['pending_purchases'], 1)
        db.session.refresh(bot)
        self.assertIsNone(bot.archived_at)

    def test_archive_success_frees_scope_key_and_replacement(self):
        superadmin = self._admin('ar2-super', role='superadmin', is_superadmin=True)
        reseller = self._admin('ar2-owner')
        bot = self._bot(owner=reseller, suffix='ar2')
        bot.scope_key = f'reseller:{reseller.id}'
        db.session.commit()
        client = self._client(superadmin)
        archived = client.post(
            f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'archive'}).get_json()
        self.assertTrue(archived['success'])
        self.assertTrue(archived['bot']['archived'])
        self.assertFalse(archived['bot']['enabled'])
        self.assertEqual(archived['bot']['scope_key'], f'reseller:{reseller.id}:archived:{bot.id}')
        # The reseller can immediately create a replacement bot.
        reseller_client = self._client(reseller)
        replacement = reseller_client.post('/api/telegram-bots', json={'display_name': 'New Bot'})
        self.assertEqual(replacement.status_code, 201)
        # Archived bot is hidden from the default list, visible with include_archived.
        listing = client.get('/api/telegram-bots').get_json()
        self.assertNotIn(bot.id, [row['id'] for row in listing['bots']])
        full = client.get('/api/telegram-bots?include_archived=1').get_json()
        self.assertIn(bot.id, [row['id'] for row in full['bots']])
        # Archived bots cannot be edited or tested through the settings routes.
        blocked = reseller_client.get(f'/api/settings/telegram-bots?bot_id={bot.id}')
        self.assertEqual(blocked.headers.get('X-Eve-Status'), '409')
        # Archived bots fall out of the worker discovery query.
        discovered = {
            row.id for row in TelegramBotInstance.query.filter(
                TelegramBotInstance.transport_mode == 'polling',
                TelegramBotInstance.archived_at.is_(None),
            ).all()
        }
        self.assertNotIn(bot.id, discovered)

    def test_restore_success_and_scope_conflict(self):
        superadmin = self._admin('rs-super', role='superadmin', is_superadmin=True)
        reseller = self._admin('rs-owner')
        bot = self._bot(owner=reseller, suffix='rs')
        bot.scope_key = f'reseller:{reseller.id}'
        db.session.commit()
        client = self._client(superadmin)
        client.post(f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'archive'})
        # Restore works while the scope is free.
        restored = client.post(
            f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'restore'}).get_json()
        self.assertTrue(restored['success'])
        self.assertFalse(restored['bot']['archived'])
        self.assertEqual(restored['bot']['scope_key'], f'reseller:{reseller.id}')
        # Archive again, create a replacement, then restore must conflict.
        client.post(f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'archive'})
        replacement = self._bot(owner=reseller, suffix='rs-replacement')
        replacement.scope_key = f'reseller:{reseller.id}'
        db.session.commit()
        conflict = client.post(f'/api/telegram-bots/{bot.id}/runtime', json={'action': 'restore'})
        self.assertFalse(conflict.get_json()['success'])
        self.assertEqual(conflict.headers.get('X-Eve-Status'), '409')
        db.session.delete(replacement)
        db.session.flush()

    def test_archive_forbidden_for_system_bot_and_resellers(self):
        superadmin = self._admin('af-super', role='superadmin', is_superadmin=True)
        reseller = self._admin('af-owner')
        bot = self._bot(owner=reseller, suffix='af')
        central = self._bot(suffix='af-central')
        db.session.commit()
        # Reseller cannot archive, not even their own bot.
        reseller_client = self._client(reseller)
        self.assertEqual(
            reseller_client.post(
                f'/api/telegram-bots/{bot.id}/runtime',
                json={'action': 'archive'}).status_code, 403)
        self.assertEqual(
            reseller_client.post(
                f'/api/telegram-bots/{bot.id}/runtime',
                json={'action': 'restore'}).status_code, 403)
        # The system bot can never be archived.
        super_client = self._client(superadmin)
        response = super_client.post(
            f'/api/telegram-bots/{central.id}/runtime', json={'action': 'archive'})
        self.assertFalse(response.get_json()['success'])
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')

    # ── health ───────────────────────────────────────────────────────────

    def test_health_states(self):
        reseller = self._admin('hl-owner')
        bot = self._bot(owner=reseller, suffix='hl')
        bot.enabled = False
        db.session.flush()
        self.assertEqual(_telegram_bot_health(bot)['state'], 'disabled')
        bot.enabled = True
        db.session.flush()
        self.assertEqual(_telegram_bot_health(bot)['state'], 'stopped')
        runtime = self._runtime(
            bot, status='running', last_heartbeat_at=datetime.utcnow())
        db.session.expire(bot)
        self.assertEqual(_telegram_bot_health(bot)['state'], 'running')
        runtime.last_heartbeat_at = datetime.utcnow() - timedelta(seconds=300)
        db.session.flush()
        self.assertEqual(_telegram_bot_health(bot)['state'], 'stale')
        runtime.failed_update_count = 2
        db.session.flush()
        health = _telegram_bot_health(bot)
        self.assertEqual(health['state'], 'error')
        self.assertEqual(health['failed_update_count'], 2)
        bot.archived_at = datetime.utcnow()
        db.session.flush()
        self.assertEqual(_telegram_bot_health(bot)['state'], 'archived')


if __name__ == '__main__':
    unittest.main()
