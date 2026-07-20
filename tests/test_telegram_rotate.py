import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from flask import jsonify  # noqa: E402

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
    TelegramPurchaseRequest,
    TelegramPurchaseRequestDetail,
    app,
    db,
)
from telegram_bot_runtime import COPY  # noqa: E402
from telegram_bot_worker import (  # noqa: E402
    _execute_purchase_request,
    _handle_callback,
    _notify_purchase_admins,
    _purchase_account_comment,
    _send_link_with_qr,
)


class FakeBotApi:
    def __init__(self, fail_upload=False):
        self.messages = []
        self.markups = []
        self.answers = []
        self.uploads = []
        self.fail_upload = fail_upload

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((int(chat_id), text))
        self.markups.append(kwargs.get('reply_markup'))
        return ({'ok': True}, 'direct')

    def call(self, method, payload=None, **kwargs):
        return ({}, 'direct')

    def answer_callback(self, callback_id, text=None):
        self.answers.append((callback_id, text))

    def send_photo(self, *args, **kwargs):
        return ({'ok': True}, 'direct')

    def send_document(self, *args, **kwargs):
        return ({'ok': True}, 'direct')

    def send_upload(self, chat_id, content, filename, content_type,
                    *, as_photo=False, caption=''):
        if self.fail_upload:
            raise RuntimeError('upload failed')
        self.uploads.append({
            'chat_id': int(chat_id), 'content': bytes(content),
            'filename': filename, 'content_type': content_type,
            'as_photo': as_photo, 'caption': caption,
        })
        return ({'ok': True}, 'direct')


class TelegramRotateBase(unittest.TestCase):
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
        TelegramPurchaseRequestDetail.query.delete()
        TelegramPurchaseRequest.query.delete()
        ServiceOwnership.query.delete()
        TelegramBotUserState.query.delete()
        TelegramIdentity.query.delete()
        TelegramBotInstance.query.delete()
        CustomerAccount.query.delete()
        BankCard.query.delete()
        Package.query.delete()
        Server.query.delete()
        Admin.query.filter(Admin.username.like('rotate-test-%')).delete(
            synchronize_session=False)
        db.session.commit()

    def _admin(self, suffix, **kwargs):
        kwargs.setdefault('is_superadmin', True)
        admin = Admin(
            username=f'rotate-test-{suffix}', role='superadmin', enabled=True, **kwargs)
        admin.set_password('StrongRotatePassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _bot(self, owner=None, suffix='bot'):
        bot = TelegramBotInstance(
            scope_key=f'rotate-test-{suffix}-{id(object())}',
            owner_type='reseller' if owner else 'system',
            owner_admin_id=owner.id if owner else None,
            display_name='Rotate Bot',
        )
        bot.test_mode = False
        db.session.add(bot)
        db.session.flush()
        return bot

    def _server(self, name='Rotate Server'):
        server = Server(name=name, host='https://rotate.test', username='u', password='p')
        db.session.add(server)
        db.session.flush()
        return server

    def _identity(self, user_id, phone='989120001111', username='buyer'):
        customer = CustomerAccount(primary_phone=phone)
        db.session.add(customer)
        db.session.flush()
        identity = TelegramIdentity(
            telegram_user_id=user_id,
            telegram_chat_id=user_id,
            customer_id=customer.id,
            username=username,
            phone_normalized=phone,
            phone_verified_at=datetime.utcnow(),
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

    def _ownership(self, customer, server, email='acc@example.com'):
        ownership = ServiceOwnership(
            customer_id=customer.id, server_id=server.id,
            client_uuid='rotate-test-uuid',
            client_email_snapshot=email)
        db.session.add(ownership)
        db.session.flush()
        return ownership

    def _callback(self, user_id, data):
        return {
            'id': f'cb-{data}',
            'from': {'id': user_id},
            'message': {'chat': {'id': user_id}},
            'data': data,
        }


class LinkQrTests(TelegramRotateBase):
    def test_qr_uploads_png_with_copyable_caption(self):
        api = FakeBotApi()
        link = 'https://rotate.test/s/1/abcdef'
        _send_link_with_qr(api, 12345, link, caption=f'Link:\n{link}')
        self.assertEqual(len(api.uploads), 1)
        upload = api.uploads[0]
        self.assertTrue(upload['content'].startswith(b'\x89PNG'))
        self.assertTrue(upload['as_photo'])
        self.assertEqual(upload['content_type'], 'image/png')
        self.assertIn(link, upload['caption'])
        self.assertEqual(api.messages, [])

    def test_qr_falls_back_to_text_when_upload_fails(self):
        api = FakeBotApi(fail_upload=True)
        link = 'https://rotate.test/s/1/abcdef'
        _send_link_with_qr(api, 12345, link, caption=f'Link:\n{link}')
        self.assertEqual(api.uploads, [])
        self.assertEqual(len(api.messages), 1)
        self.assertIn(link, api.messages[0][1])


class AdminCardEnrichmentTests(TelegramRotateBase):
    def _purchase_request(self, user_id=9_100_001):
        admin = self._admin('super', telegram_id=str(7_700_001))
        bot = self._bot(suffix='notify')
        server = self._server()
        package = Package(name='gold', days=30, volume=100, price=100_000, enabled=True)
        db.session.add(package)
        card = BankCard(
            label='Main', bank_name='Melli', owner_name='Navid Admin',
            card_number='6037991234567890')
        db.session.add(card)
        customer, identity = self._identity(user_id)
        db.session.flush()
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id,
            telegram_user_id=user_id,
            customer_id=customer.id,
            server_id=server.id,
            package_id=package.id,
            bank_card_id=card.id,
            amount=100_000,
            receipt_file_id='photo-file-1',
            receipt_kind='photo',
            source_chat_id=user_id,
            source_message_id=1,
            status='pending',
            payment_method='card',
        )
        db.session.add(request_row)
        db.session.flush()
        return admin, request_row

    def test_admin_card_contains_identity_and_full_card(self):
        _admin, request_row = self._purchase_request()
        api = FakeBotApi()
        _notify_purchase_admins(api, request_row)
        admin_messages = [text for chat_id, text in api.messages if chat_id == 7_700_001]
        self.assertEqual(len(admin_messages), 1)
        text = admin_messages[0]
        self.assertIn('@buyer (https://t.me/buyer)', text)
        self.assertIn(str(request_row.telegram_user_id), text)
        self.assertIn('09120001111', text)
        self.assertIn('6037 9912 3456 7890', text)
        self.assertIn('Navid Admin', text)
        self.assertIn('Main', text)


class PurchaseCommentTests(TelegramRotateBase):
    def _request_row(self, user_id=9_100_010, receipt='wallet:1'):
        bot = self._bot(suffix='comment')
        server = self._server()
        package = Package(name='silver', days=30, volume=50, price=50_000, enabled=True)
        db.session.add(package)
        customer, _identity = self._identity(user_id)
        db.session.flush()
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id,
            telegram_user_id=user_id,
            customer_id=customer.id,
            server_id=server.id,
            package_id=package.id,
            amount=50_000,
            receipt_file_id=receipt,
            receipt_kind='photo',
            source_chat_id=user_id,
            source_message_id=1,
            status='approved',
            payment_method='wallet',
        )
        db.session.add(request_row)
        db.session.flush()
        detail = TelegramPurchaseRequestDetail(
            request_id=request_row.id, account_name='acc1',
            allocation_strategy='auto')
        db.session.add(detail)
        db.session.flush()
        return request_row

    def test_comment_includes_phone_and_username(self):
        request_row = self._request_row()
        comment = _purchase_account_comment(request_row, False, 'telegram_purchase')
        self.assertIn(f'Telegram purchase #{request_row.id}', comment)
        self.assertIn('phone:09120001111', comment)
        self.assertIn('@buyer', comment)
        self.assertLessEqual(len(comment), 200)

    def test_trial_comment_uses_trial_label(self):
        request_row = self._request_row(user_id=9_100_011, receipt='trial:91000011')
        comment = _purchase_account_comment(request_row, True, 'telegram_trial')
        self.assertTrue(comment.startswith('Telegram trial'))
        self.assertNotIn('purchase #', comment)
        self.assertIn('phone:09120001111', comment)

    def test_execute_purchase_passes_identity_comment_to_add_client(self):
        request_row = self._request_row(user_id=9_100_012)
        reviewer = self._admin('reviewer')
        captured = {}

        def fake_add_client(server_id, inbound_id):
            from flask import request as flask_request
            captured.update(flask_request.get_json())
            return jsonify({'success': True, 'client': {'email': 'acc1'}}), 200

        client_snapshot = {'email': 'acc1', 'id': 'uuid-1', 'raw_client': {}}
        with mock.patch('telegram_bot_worker.add_client', side_effect=fake_add_client), \
                mock.patch('telegram_bot_worker._ensure_purchase_inbound_allocation',
                           return_value=([3], None)), \
                mock.patch('telegram_bot_worker._cached_purchase_client',
                           side_effect=[None, client_snapshot]), \
                mock.patch('telegram_bot_worker._ensure_purchase_ownership',
                           return_value=mock.Mock(id=55)):
            success, payload = _execute_purchase_request(request_row, reviewer)
        self.assertTrue(success, payload)
        comment = captured.get('comment', '')
        self.assertIn(f'Telegram purchase #{request_row.id}', comment)
        self.assertIn('phone:09120001111', comment)
        self.assertIn('@buyer', comment)


class RotateFlowTests(TelegramRotateBase):
    def _setup(self, user_id=9_100_020):
        self._admin('super')
        bot = self._bot(suffix=f'flow-{user_id}')
        server = self._server()
        customer, _identity = self._identity(user_id)
        self._state(bot, user_id)
        ownership = self._ownership(customer, server)
        return bot, ownership

    def _rotate_payload(self):
        return jsonify({
            'ok': True, 'success': True, 'new_email': 'acc-r1@example.com',
            'sub_url': 'https://rotate.test/sub/newsubid',
            'dash_sub_url': 'https://rotate.test/s/1/newsubid',
            'remaining_days': 25, 'remaining_gb': 4.5,
        }), 200

    def test_rotate_button_prompts_for_confirmation(self):
        bot, ownership = self._setup()
        api = FakeBotApi()
        _handle_callback(api, bot, self._callback(9_100_020, f'service-rotate:{ownership.id}'))
        self.assertIn(COPY['fa']['service_rotate_confirm'].split('{')[0], api.messages[-1][1])
        buttons = api.markups[-1]['inline_keyboard'][0]
        self.assertEqual(buttons[0]['callback_data'], f'service-rotate:confirm:{ownership.id}')
        self.assertEqual(buttons[1]['callback_data'], f'service:{ownership.id}')

    def test_rotate_confirm_calls_route_and_delivers_qr(self):
        bot, ownership = self._setup()
        api = FakeBotApi()
        with mock.patch('telegram_bot_worker.rotate_client') as rotate:
            rotate.side_effect = lambda server_id: self._rotate_payload()
            _handle_callback(
                api, bot,
                self._callback(9_100_020, f'service-rotate:confirm:{ownership.id}'))
        self.assertEqual(rotate.call_count, 1)
        self.assertEqual(rotate.call_args[0][0], ownership.server_id)
        self.assertEqual(len(api.uploads), 1)
        upload = api.uploads[0]
        self.assertTrue(upload['content'].startswith(b'\x89PNG'))
        self.assertIn('https://rotate.test/s/1/newsubid', upload['caption'])
        self.assertIn('25', upload['caption'])
        self.assertIn('4.5', upload['caption'])

    def test_rotate_failure_reports_error(self):
        bot, ownership = self._setup()
        api = FakeBotApi()
        with mock.patch('telegram_bot_worker.rotate_client') as rotate:
            rotate.side_effect = lambda server_id: (
                jsonify({'ok': False, 'success': False, 'error': 'panel down'}), 502)
            _handle_callback(
                api, bot,
                self._callback(9_100_020, f'service-rotate:confirm:{ownership.id}'))
        self.assertEqual(api.uploads, [])
        self.assertIn('panel down', api.messages[-1][1])

    def test_rotate_rate_limit_blocks_fourth_attempt(self):
        bot, ownership = self._setup(user_id=9_100_021)
        api = FakeBotApi()
        with mock.patch('telegram_bot_worker.rotate_client') as rotate:
            rotate.side_effect = lambda server_id: self._rotate_payload()
            for _ in range(3):
                _handle_callback(
                    api, bot,
                    self._callback(9_100_021, f'service-rotate:confirm:{ownership.id}'))
            self.assertEqual(rotate.call_count, 3)
            _handle_callback(
                api, bot,
                self._callback(9_100_021, f'service-rotate:confirm:{ownership.id}'))
            self.assertEqual(rotate.call_count, 3)
        self.assertEqual(api.answers[-1][1], COPY['fa']['service_rotate_limited'])
        self.assertEqual(len(api.uploads), 3)


if __name__ == '__main__':
    unittest.main()
