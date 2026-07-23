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

from app import (  # noqa: E402
    Admin,
    CustomerAccount,
    FAQ,
    Package,
    Server,
    ServiceOwnership,
    TelegramBotInstance,
    TelegramBotUserState,
    TelegramIdentity,
    TelegramPurchasePolicy,
    TelegramPurchaseRequest,
    TelegramPurchaseRequestDetail,
    TelegramServiceRequest,
    TelegramTrialGrant,
    app,
    db,
)
from telegram_bot_runtime import (  # noqa: E402
    COPY,
    HIDEABLE_MENU_KEYS,
    main_menu_keyboard,
)
from telegram_bot_worker import (  # noqa: E402
    _deliver_or_request_membership,
    _faq_html_to_telegram,
    _handle_admin_service_callback,
    _handle_callback,
    _send_faq_item,
    _send_faq_list,
    _send_faq_menu,
    _send_link_with_qr,
    _send_service_delivery,
    _start_trial,
)


class FakeBotApi:
    def __init__(self, fail_upload=False, fail_photo_url=False):
        self.messages = []
        self.markups = []
        self.answers = []
        self.uploads = []
        self.photo_urls = []
        self.fail_upload = fail_upload
        self.fail_photo_url = fail_photo_url

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

    def send_photo_url(self, chat_id, url, caption='', reply_markup=None):
        if self.fail_photo_url:
            raise RuntimeError('photo url rejected')
        self.photo_urls.append({
            'chat_id': int(chat_id), 'url': url,
            'caption': caption, 'reply_markup': reply_markup,
        })
        return ({'ok': True}, 'direct')

    def send_upload(self, chat_id, content, filename, content_type,
                    *, as_photo=False, caption='', reply_markup=None):
        if self.fail_upload:
            raise RuntimeError('upload failed')
        self.uploads.append({
            'chat_id': int(chat_id), 'content': bytes(content),
            'filename': filename, 'content_type': content_type,
            'as_photo': as_photo, 'caption': caption,
            'reply_markup': reply_markup,
        })
        return ({'ok': True}, 'direct')


def _callback_data(markup):
    data = []
    for row in (markup or {}).get('inline_keyboard', []):
        for button in row:
            data.append(button.get('callback_data'))
    return data


def _delivery_callbacks(ownership_id):
    return {
        f'service-link:{ownership_id}',
        f'service-rotate:{ownership_id}',
        f'service-renew:{ownership_id}',
        f'service-support:{ownership_id}',
        'service-list',
        'tutorial-devices',
        'faq-menu',
    }


class DeliveryFaqBase(unittest.TestCase):
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
        FAQ.query.delete()
        TelegramServiceRequest.query.delete()
        TelegramTrialGrant.query.delete()
        TelegramPurchaseRequestDetail.query.delete()
        TelegramPurchaseRequest.query.delete()
        TelegramPurchasePolicy.query.delete()
        TelegramBotUserState.query.delete()
        TelegramIdentity.query.delete()
        TelegramBotInstance.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        Package.query.delete()
        Server.query.delete()
        Admin.query.filter(Admin.username.like('delivery-test-%')).delete(
            synchronize_session=False)
        db.session.commit()

    def _admin(self, suffix, **kwargs):
        kwargs.setdefault('is_superadmin', True)
        admin = Admin(
            username=f'delivery-test-{suffix}', role='superadmin', enabled=True, **kwargs)
        admin.set_password('StrongDeliveryPassword123!')
        db.session.add(admin)
        db.session.flush()
        return admin

    def _bot(self, suffix='bot'):
        bot = TelegramBotInstance(
            scope_key=f'delivery-test-{suffix}-{id(object())}',
            owner_type='system',
            display_name='Delivery Bot',
        )
        bot.test_mode = False
        db.session.add(bot)
        db.session.flush()
        return bot

    def _server(self, name='Delivery Server'):
        server = Server(name=name, host='https://delivery.test', username='u', password='p')
        db.session.add(server)
        db.session.flush()
        return server

    def _identity(self, user_id, phone='989120001111'):
        customer = CustomerAccount(primary_phone=phone)
        db.session.add(customer)
        db.session.flush()
        identity = TelegramIdentity(
            telegram_user_id=user_id,
            telegram_chat_id=user_id,
            customer_id=customer.id,
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
            client_uuid=f'delivery-test-uuid-{id(object())}',
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


class LinkQrKeyboardTests(DeliveryFaqBase):
    def test_qr_upload_carries_reply_markup(self):
        api = FakeBotApi()
        keyboard = {'inline_keyboard': [[{'text': 'x', 'callback_data': 'noop'}]]}
        _send_link_with_qr(
            api, 12345, 'https://delivery.test/s/1/abc',
            caption='cap', reply_markup=keyboard)
        self.assertEqual(len(api.uploads), 1)
        self.assertEqual(api.uploads[0]['reply_markup'], keyboard)
        self.assertEqual(api.messages, [])

    def test_qr_fallback_message_keeps_reply_markup(self):
        api = FakeBotApi(fail_upload=True)
        keyboard = {'inline_keyboard': [[{'text': 'x', 'callback_data': 'noop'}]]}
        link = 'https://delivery.test/s/1/abc'
        _send_link_with_qr(api, 12345, link, caption=f'Link:\n{link}',
                           reply_markup=keyboard)
        self.assertEqual(api.uploads, [])
        self.assertEqual(len(api.messages), 1)
        self.assertIn(link, api.messages[0][1])
        self.assertEqual(api.markups[0], keyboard)

    def test_send_upload_form_field_includes_reply_markup(self):
        # Telegram accepts reply_markup as a JSON-encoded multipart form field.
        from telegram_bot_runtime import TelegramBotApi
        import inspect
        signature = inspect.signature(TelegramBotApi.send_upload)
        self.assertIn('reply_markup', signature.parameters)


class ServiceDeliveryHelperTests(DeliveryFaqBase):
    def test_short_template_becomes_qr_caption(self):
        api = FakeBotApi()
        link = 'https://delivery.test/s/1/xyz'
        _send_service_delivery(api, 12345, 'fa', 77, f'متن تحویل\n{link}', link)
        self.assertEqual(len(api.uploads), 1)
        upload = api.uploads[0]
        self.assertIn('متن تحویل', upload['caption'])
        self.assertIn(link, upload['caption'])
        self.assertEqual(api.messages, [])
        self.assertEqual(
            set(_callback_data(upload['reply_markup'])), _delivery_callbacks(77))

    def test_long_template_moves_to_follow_up_message(self):
        api = FakeBotApi()
        link = 'https://delivery.test/s/1/xyz'
        template = f'{"طولانی " * 300}\n{link}'
        self.assertGreater(len(template), 1024)
        _send_service_delivery(api, 12345, 'fa', 78, template, link)
        self.assertEqual(len(api.uploads), 1)
        # Photo keeps the short raw-link caption; full text is a follow-up.
        self.assertEqual(api.uploads[0]['caption'], link)
        self.assertEqual(len(api.messages), 1)
        self.assertIn('طولانی', api.messages[0][1])
        self.assertEqual(
            set(_callback_data(api.markups[-1])), _delivery_callbacks(78))

    def test_missing_link_sends_text_only(self):
        api = FakeBotApi()
        _send_service_delivery(api, 12345, 'fa', 79, 'متن بدون لینک', '')
        self.assertEqual(api.uploads, [])
        self.assertEqual(len(api.messages), 1)
        self.assertEqual(
            set(_callback_data(api.markups[-1])), _delivery_callbacks(79))


class PurchaseDeliveryTests(DeliveryFaqBase):
    def _request_row(self, user_id=9_200_001):
        bot = self._bot(suffix='purchase')
        server = self._server()
        package = Package(name='gold', days=30, volume=100, price=100_000, enabled=True)
        db.session.add(package)
        customer, _identity = self._identity(user_id)
        db.session.flush()
        request_row = TelegramPurchaseRequest(
            bot_instance_id=bot.id,
            telegram_user_id=user_id,
            customer_id=customer.id,
            server_id=server.id,
            package_id=package.id,
            amount=100_000,
            receipt_file_id='photo-file-1',
            receipt_kind='photo',
            source_chat_id=user_id,
            source_message_id=1,
            status='completed',
            payment_method='card',
        )
        db.session.add(request_row)
        db.session.flush()
        detail = TelegramPurchaseRequestDetail(
            request_id=request_row.id, account_name='acc1',
            allocation_strategy='auto')
        db.session.add(detail)
        ownership = self._ownership(customer, server, email='acc1')
        db.session.flush()
        return bot, request_row, ownership

    def test_purchase_delivery_sends_rich_qr_card(self):
        bot, request_row, ownership = self._request_row()
        api = FakeBotApi()
        link = 'https://delivery.test/s/1/purchase'
        result = {'ownership_id': ownership.id, 'client': {'dashboard_link': link}}
        delivered = _deliver_or_request_membership(
            api, bot, request_row, 9_200_001, 'fa', 9_200_001, result=result)
        self.assertTrue(delivered)
        self.assertEqual(len(api.uploads), 1)
        upload = api.uploads[0]
        self.assertIn('acc1', upload['caption'])
        self.assertIn(link, upload['caption'])
        self.assertEqual(
            set(_callback_data(upload['reply_markup'])),
            _delivery_callbacks(ownership.id))
        # No separate plain-text completion message is needed anymore.
        self.assertEqual(api.messages, [])


class RenewalDeliveryTests(DeliveryFaqBase):
    def _setup(self, admin_telegram_id=7_800_001, user_id=9_200_010):
        admin = self._admin('renewal', telegram_id=str(admin_telegram_id))
        bot = self._bot(suffix='renewal')
        server = self._server()
        package = Package(name='silver', days=30, volume=50, price=50_000, enabled=True)
        db.session.add(package)
        customer, _identity = self._identity(user_id)
        ownership = self._ownership(customer, server, email='renew@example.com')
        db.session.flush()
        request_row = TelegramServiceRequest(
            bot_instance_id=bot.id,
            telegram_user_id=user_id,
            customer_id=customer.id,
            service_ownership_id=ownership.id,
            request_type='renewal',
            package_id=package.id,
            amount=50_000,
            payment_method='card',
            status='pending',
        )
        db.session.add(request_row)
        db.session.flush()
        return admin, request_row, ownership

    def test_renewal_completion_delivers_qr_card(self):
        admin, request_row, ownership = self._setup()
        api = FakeBotApi()
        link = 'https://delivery.test/s/1/renewed'
        callback = {
            'id': 'cb-renew',
            'from': {'id': 7_800_001},
            'message': {'chat': {'id': -100123}},
            'data': f'admin-service:{request_row.id}:complete',
        }
        with mock.patch('telegram_bot_worker._execute_renewal_request',
                        return_value=(True, {'success': True})), \
                mock.patch('telegram_bot_worker._cached_owned_service_location',
                           return_value=({'dashboard_link': link,
                                          'email': 'renew@example.com'}, 3)):
            handled = _handle_admin_service_callback(
                api, callback, f'admin-service:{request_row.id}:complete')
        self.assertTrue(handled)
        self.assertEqual(request_row.status, 'completed')
        self.assertEqual(len(api.uploads), 1)
        upload = api.uploads[0]
        # The QR card goes to the customer's chat, not the admin review chat.
        self.assertEqual(upload['chat_id'], 9_200_010)
        self.assertIn('renew@example.com', upload['caption'])
        self.assertIn(link, upload['caption'])
        self.assertIn(
            COPY['fa']['renewal_completed'].split('{')[0].strip(),
            upload['caption'])
        self.assertEqual(
            set(_callback_data(upload['reply_markup'])),
            _delivery_callbacks(ownership.id))
        # The generic request_completed text must not be used for renewals.
        customer_texts = [text for chat_id, text in api.messages
                          if chat_id == 9_200_010]
        self.assertNotIn(COPY['fa']['request_completed'], customer_texts)


class TrialDeliveryTests(DeliveryFaqBase):
    def test_trial_success_sends_rich_qr_card(self):
        self._admin('super')
        bot = self._bot(suffix='trial')
        package = Package(
            name='trial', days=3, volume=1, price=0, enabled=True, is_trial=True)
        db.session.add(package)
        db.session.flush()
        policy = TelegramPurchasePolicy(
            bot_instance_id=bot.id, trial_enabled=True, trial_package_id=package.id)
        db.session.add(policy)
        user_id = 9_200_020
        customer, _identity = self._identity(user_id, phone='9120099999')
        state = self._state(bot, user_id)
        server = self._server('Trial Server')
        ownership = self._ownership(customer, server, email='trial@example.com')
        db.session.flush()
        api = FakeBotApi()
        link = 'https://delivery.test/s/1/trial'
        with mock.patch('telegram_bot_worker._assign_purchase_server',
                        return_value=server), \
                mock.patch('telegram_bot_worker._execute_purchase_request',
                           return_value=(True, {
                               'client': {'dashboard_link': link},
                               'ownership_id': ownership.id,
                           })):
            _start_trial(api, bot, user_id, user_id, state)
        self.assertEqual(len(api.uploads), 1)
        upload = api.uploads[0]
        self.assertIn(COPY['fa']['trial_success'].split('{')[0].strip(),
                      upload['caption'])
        self.assertIn(link, upload['caption'])
        self.assertEqual(
            set(_callback_data(upload['reply_markup'])),
            _delivery_callbacks(ownership.id))


class FaqFlowTests(DeliveryFaqBase):
    def _faq(self, title, platform='android', **kwargs):
        kwargs.setdefault('content', '<p>متن پاسخ</p>')
        kwargs.setdefault('is_enabled', True)
        row = FAQ(title=title, platform=platform, **kwargs)
        db.session.add(row)
        db.session.flush()
        return row

    def test_menu_keyboard_contains_faq_button(self):
        keyboard = main_menu_keyboard('fa')
        labels = {btn['text'] for row in keyboard['keyboard'] for btn in row}
        self.assertIn(COPY['fa']['menu_faq'], labels)
        self.assertIn('menu_faq', HIDEABLE_MENU_KEYS)

    def test_faq_menu_empty(self):
        api = FakeBotApi()
        _send_faq_menu(api, 12345, 'fa')
        self.assertEqual(api.messages[-1][1], COPY['fa']['faq_empty'])

    def test_faq_menu_lists_platform_tabs(self):
        self._faq('سوال عمومی', platform='general')
        api = FakeBotApi()
        _send_faq_menu(api, 12345, 'fa')
        self.assertEqual(api.messages[-1][1], COPY['fa']['faq_choose_device'])
        self.assertEqual(
            _callback_data(api.markups[-1]),
            ['faq-os:android', 'faq-os:ios', 'faq-os:windows', 'faq-os:general'])

    def test_faq_list_includes_general_and_platform_only(self):
        android = self._faq('اندرویدی')
        general = self._faq('عمومی', platform='general')
        self._faq('آیفونی', platform='ios')
        self._faq('غیرفعال', is_enabled=False)
        api = FakeBotApi()
        _send_faq_list(api, 12345, 'fa', 'android')
        data = _callback_data(api.markups[-1])
        self.assertIn(f'faq-item:{android.id}', data)
        self.assertIn(f'faq-item:{general.id}', data)
        self.assertEqual(
            [item for item in data if item and item.startswith('faq-item:')],
            [f'faq-item:{android.id}', f'faq-item:{general.id}'])
        # Back button returns to the platform menu.
        self.assertEqual(data[-1], 'faq-menu')

    def test_faq_item_sends_content_and_image_by_url(self):
        row = self._faq(
            'سوال تصویر', content='<p>پاسخ <b>مهم</b> است</p>',
            image_url='https://delivery.test/faq.png',
            video_url='https://delivery.test/faq.mp4')
        api = FakeBotApi()
        _send_faq_item(api, 12345, 'fa', row.id)
        self.assertEqual(len(api.photo_urls), 1)
        self.assertEqual(api.photo_urls[0]['url'], 'https://delivery.test/faq.png')
        text = api.messages[-1][1]
        self.assertIn('<b>سوال تصویر</b>', text)
        self.assertIn('پاسخ <b>مهم</b> است', text)
        self.assertNotIn('<p>', text)
        markup = api.markups[-1]
        urls = [btn.get('url') for r in markup['inline_keyboard'] for btn in r]
        self.assertIn('https://delivery.test/faq.mp4', urls)
        # Image was sent successfully, so no fallback image button.
        self.assertNotIn('https://delivery.test/faq.png', urls)
        self.assertEqual(
            _callback_data(markup)[-1], f'faq-os:{row.platform}')

    def test_faq_item_image_error_falls_back_to_url_button(self):
        row = self._faq('سوال تصویر', image_url='https://delivery.test/faq.png')
        api = FakeBotApi(fail_photo_url=True)
        _send_faq_item(api, 12345, 'fa', row.id)
        self.assertEqual(api.photo_urls, [])
        buttons = [btn for r in api.markups[-1]['inline_keyboard'] for btn in r]
        image_buttons = [b for b in buttons
                         if b.get('url') == 'https://delivery.test/faq.png']
        self.assertEqual(len(image_buttons), 1)
        self.assertEqual(image_buttons[0]['text'], COPY['fa']['faq_view_image'])

    def test_faq_item_unavailable_shows_empty(self):
        row = self._faq('مخفی', is_enabled=False)
        api = FakeBotApi()
        _send_faq_item(api, 12345, 'fa', row.id)
        self.assertEqual(api.messages[-1][1], COPY['fa']['faq_empty'])

    def test_faq_html_whitelists_telegram_tags(self):
        html_in = ('<p>سلام <strong>دنیا</strong> <script>alert(1)</script><br>'
                   '<a href="https://example.com/x?y=1&amp;z=2">لینک</a>'
                   '<span>حذف</span></p>')
        out = _faq_html_to_telegram(html_in)
        self.assertIn('<b>دنیا</b>', out)
        self.assertIn('\n', out)
        self.assertIn('<a href="https://example.com/x?y=1&amp;z=2">لینک</a>', out)
        self.assertNotIn('<script', out)
        self.assertNotIn('alert', out)
        self.assertNotIn('<span>', out)
        self.assertIn('حذف', out)

    def test_faq_callbacks_dispatch(self):
        self._faq('عمومی', platform='general')
        bot = self._bot(suffix='faq-cb')
        user_id = 9_200_030
        self._identity(user_id)
        self._state(bot, user_id)
        api = FakeBotApi()
        _handle_callback(api, bot, self._callback(user_id, 'faq-menu'))
        self.assertEqual(api.messages[-1][1], COPY['fa']['faq_choose_device'])
        _handle_callback(api, bot, self._callback(user_id, 'faq-os:general'))
        data = _callback_data(api.markups[-1])
        self.assertTrue(any(item and item.startswith('faq-item:') for item in data))
        self.assertEqual(api.answers[-1][0], 'cb-faq-os:general')

    def test_delivery_keyboard_reaches_faq(self):
        # The delivery card FAQ button is dispatched by _handle_callback.
        api = FakeBotApi()
        keyboard = {'inline_keyboard': [[{
            'text': COPY['fa']['menu_faq'], 'callback_data': 'faq-menu'}]]}
        _send_link_with_qr(
            api, 12345, 'https://delivery.test/s/1/faq', reply_markup=keyboard)
        self.assertEqual(api.uploads[0]['reply_markup'], keyboard)


if __name__ == '__main__':
    unittest.main()
