"""Small, token-safe Telegram Bot API client and localized onboarding UI."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from telegram_diagnostics import redact_connection_error


API_ROOT = "https://api.telegram.org"
_TRANSPORT_LOCAL = threading.local()


def _pooled_session() -> requests.Session:
    """One connection pool per worker thread; no settings or customer data is cached."""
    session = getattr(_TRANSPORT_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0, pool_block=False)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": "Eve-Telegram/1"})
        _TRANSPORT_LOCAL.session = session
    return session


def _route_state(client_key: str) -> dict[str, Any]:
    states = getattr(_TRANSPORT_LOCAL, "route_states", None)
    if states is None:
        states = {}
        _TRANSPORT_LOCAL.route_states = states
    return states.setdefault(client_key, {"preferred": None, "cooldowns": {}})


@dataclass(frozen=True)
class TelegramRoute:
    name: str
    proxies: dict[str, str] | None = None


class TelegramApiError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, retry_after: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = max(0, int(retry_after or 0))


class TelegramBotApi:
    """Bot API client with ordered route failover and no token logging."""

    def __init__(self, token: str, routes: list[TelegramRoute]):
        self._token = token
        self._routes = routes or [TelegramRoute("direct")]
        self._session = _pooled_session()
        self._route_state = _route_state(hashlib.sha256(token.encode()).hexdigest()[:20])

    def _ordered_routes(self) -> list[TelegramRoute]:
        now = time.monotonic()
        cooldowns = self._route_state["cooldowns"]
        active = [route for route in self._routes if float(cooldowns.get(route.name, 0)) <= now]
        routes = active or list(self._routes)
        preferred = self._route_state.get("preferred")
        return sorted(routes, key=lambda route: route.name != preferred)

    def _route_succeeded(self, route: TelegramRoute):
        self._route_state["preferred"] = route.name
        self._route_state["cooldowns"].pop(route.name, None)

    def _route_failed(self, route: TelegramRoute):
        # A short circuit breaker avoids paying the same connect/TLS timeout for
        # every reply while still probing the route again automatically.
        if self._route_state.get("preferred") == route.name:
            self._route_state["preferred"] = None
        self._route_state["cooldowns"][route.name] = time.monotonic() + 20

    def call(self, method: str, payload: dict[str, Any] | None = None,
             *, long_poll_timeout: int = 0) -> tuple[Any, str]:
        errors: list[str] = []
        connect_timeout = 4
        read_timeout = max(15, int(long_poll_timeout) + 10)
        url = f"{API_ROOT}/bot{self._token}/{method}"
        for route in self._ordered_routes():
            started = time.perf_counter()
            try:
                response = self._session.post(
                    url, json=payload or {}, proxies=route.proxies,
                    timeout=(connect_timeout, read_timeout),
                )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise TelegramApiError(
                        f"Telegram returned invalid JSON (HTTP {response.status_code})",
                    ) from exc
                if response.status_code == 200 and isinstance(body, dict) and body.get("ok"):
                    self._route_succeeded(route)
                    return body.get("result"), route.name
                description = body.get("description") if isinstance(body, dict) else None
                safe = redact_connection_error(
                    description or f"Telegram returned HTTP {response.status_code}",
                    (self._token,),
                )
                # Auth, validation and webhook conflicts are route-independent.
                if response.status_code in (400, 401, 403, 404, 409, 429):
                    parameters = body.get("parameters") if isinstance(body, dict) else {}
                    raise TelegramApiError(
                        safe,
                        retryable=False,
                        retry_after=(parameters or {}).get("retry_after", 0),
                    )
                self._route_failed(route)
                errors.append(f"{route.name}: {safe}")
            except TelegramApiError as exc:
                if not exc.retryable:
                    raise
                self._route_failed(route)
                errors.append(f"{route.name}: {exc}")
            except requests.RequestException as exc:
                self._route_failed(route)
                elapsed = max(1, int((time.perf_counter() - started) * 1000))
                safe = redact_connection_error(exc, (self._token,))
                errors.append(f"{route.name} ({elapsed} ms): {safe}")
        raise TelegramApiError("; ".join(errors) or "No Telegram route is available")

    def get_updates(self, offset: int, *, timeout: int = 25):
        return self.call("getUpdates", {
            "offset": int(offset or 0),
            "timeout": int(timeout),
            "allowed_updates": ["message", "callback_query"],
        }, long_poll_timeout=timeout)

    def delete_webhook(self):
        return self.call("deleteWebhook", {"drop_pending_updates": False})

    def get_webhook_info(self):
        return self.call("getWebhookInfo", {})

    def send_message(self, chat_id: int, text: str, **extra):
        return self.call("sendMessage", {"chat_id": int(chat_id), "text": text, **extra})

    def copy_message(self, chat_id: int, from_chat_id: int, message_id: int, **extra):
        return self.call("copyMessage", {
            "chat_id": int(chat_id),
            "from_chat_id": int(from_chat_id),
            "message_id": int(message_id),
            **extra,
        })

    def create_forum_topic(self, chat_id: int, name: str):
        return self.call("createForumTopic", {
            "chat_id": int(chat_id),
            "name": str(name)[:128],
        })

    def close_forum_topic(self, chat_id: int, message_thread_id: int):
        return self.call("closeForumTopic", {
            "chat_id": int(chat_id),
            "message_thread_id": int(message_thread_id),
        })

    def send_photo(self, chat_id: int, file_id: str, **extra):
        return self.call("sendPhoto", {
            "chat_id": int(chat_id),
            "photo": str(file_id),
            **extra,
        })

    def send_document(self, chat_id: int, file_id: str, **extra):
        return self.call("sendDocument", {
            "chat_id": int(chat_id),
            "document": str(file_id),
            **extra,
        })

    def send_upload(self, chat_id: int, content: bytes, filename: str,
                    content_type: str, *, as_photo: bool = False, caption: str = ''):
        """Upload a trusted in-memory operator attachment through route failover."""
        if not content:
            raise TelegramApiError("Attachment is empty", retryable=False)
        method = "sendPhoto" if as_photo else "sendDocument"
        field = "photo" if as_photo else "document"
        url = f"{API_ROOT}/bot{self._token}/{method}"
        errors: list[str] = []
        for route in self._ordered_routes():
            try:
                data = {"chat_id": int(chat_id)}
                if caption:
                    data["caption"] = str(caption)[:1024]
                response = self._session.post(
                    url,
                    data=data,
                    files={field: (str(filename), content, str(content_type or 'application/octet-stream'))},
                    proxies=route.proxies,
                    timeout=(15, 120),
                )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise TelegramApiError(
                        f"Telegram returned invalid JSON (HTTP {response.status_code})",
                    ) from exc
                if response.status_code == 200 and isinstance(body, dict) and body.get("ok"):
                    self._route_succeeded(route)
                    return body.get("result"), route.name
                description = body.get("description") if isinstance(body, dict) else None
                safe = redact_connection_error(
                    description or f"Telegram returned HTTP {response.status_code}",
                    (self._token,),
                )
                if response.status_code in (400, 401, 403, 404, 409, 413, 429):
                    raise TelegramApiError(safe, retryable=False)
                self._route_failed(route)
                errors.append(f"{route.name}: {safe}")
            except TelegramApiError as exc:
                if not exc.retryable:
                    raise
                self._route_failed(route)
                errors.append(f"{route.name}: {exc}")
            except requests.RequestException as exc:
                self._route_failed(route)
                errors.append(f"{route.name}: {redact_connection_error(exc, (self._token,))}")
        raise TelegramApiError("; ".join(errors) or "No Telegram route is available")

    def download_file(self, file_id: str, *, max_bytes: int = 20 * 1024 * 1024):
        """Resolve and download a Telegram file through the bot's configured routes."""
        metadata, route_name = self.call("getFile", {"file_id": str(file_id)})
        file_path = str((metadata or {}).get("file_path") or "").strip()
        if not file_path or file_path.startswith('/') or '..' in file_path.split('/'):
            raise TelegramApiError("Telegram returned an invalid file path", retryable=False)
        routes = sorted(self._ordered_routes(), key=lambda route: route.name != route_name)
        url = f"{API_ROOT}/file/bot{self._token}/{file_path}"
        errors: list[str] = []
        for route in routes:
            try:
                response = self._session.get(
                    url, proxies=route.proxies, timeout=(10, 30), stream=True,
                )
                if response.status_code != 200:
                    self._route_failed(route)
                    errors.append(f"{route.name}: HTTP {response.status_code}")
                    continue
                declared = int(response.headers.get('Content-Length') or 0)
                if declared > max_bytes:
                    raise TelegramApiError("Telegram file is too large", retryable=False)
                chunks = []
                size = 0
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramApiError("Telegram file is too large", retryable=False)
                    chunks.append(chunk)
                content_type = str(response.headers.get('Content-Type') or 'application/octet-stream')
                filename = file_path.rsplit('/', 1)[-1] or 'telegram-receipt'
                self._route_succeeded(route)
                return b''.join(chunks), content_type, filename, route.name
            except TelegramApiError:
                raise
            except requests.RequestException as exc:
                self._route_failed(route)
                errors.append(f"{route.name}: {redact_connection_error(exc, (self._token,))}")
        raise TelegramApiError('; '.join(errors) or 'Could not download Telegram file')

    def answer_callback(self, callback_query_id: str, text: str = ""):
        return self.call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
        })


COPY = {
    "fa": {
        "choose_language": "زبان ربات را انتخاب کنید:",
        "share_phone": "برای شناسایی حساب‌ها، شماره موبایل خودتان را با دکمه زیر ارسال کنید.",
        "share_button": "📱 ارسال شماره من",
        "phone_invalid": "این شماره معتبر نیست. لطفاً شماره ایران خودتان را با دکمه ارسال کنید.",
        "phone_mismatch": "برای امنیت، فقط شماره‌ای پذیرفته می‌شود که Telegram آن را متعلق به خود شما اعلام کند.",
        "phone_conflict": "این حساب قبلاً به مشتری دیگری متصل شده است. برای ادامه، مدیر باید آن را بررسی کند.",
        "verified": "✅ شماره شما تأیید شد. در مرحله بعد سرویس‌هایتان را با هم پیدا و متصل می‌کنیم.",
        "no_candidates": "فعلاً سرویسی با این شماره پیدا نشد. برای بررسی بیشتر با پشتیبانی تماس بگیرید.",
        "choose_service": "سرویس‌های احتمالی زیر پیدا شدند. برای اثبات مالکیت، یکی را انتخاب کنید:",
        "send_subscription": "لینک Subscription همین سرویس را ارسال کنید. لینک فقط بررسی می‌شود و ذخیره نخواهد شد.",
        "invalid_subscription": "این لینک با سرویس انتخاب‌شده مطابقت ندارد. دوباره بررسی و ارسال کنید.",
        "proof_limited": "تعداد تلاش ناموفق زیاد بود. ۱۵ دقیقه بعد دوباره امتحان کنید.",
        "service_attached": "✅ مالکیت سرویس تأیید و به حساب شما اضافه شد.",
        "admin_review": "درخواست شما برای بررسی دستی مدیر ثبت شد.",
        "claim_conflict": "این سرویس مالک دیگری دارد و باید توسط مدیر بررسی شود.",
        "claim_rejected": "درخواست مالکیت این سرویس توسط مدیر تأیید نشد.",
        "no_link_button": "لینک‌ها را ندارم",
        "service_button": "سرویس",
        "welcome_menu": "به ربات Eve خوش آمدید. یکی از گزینه‌های زیر را انتخاب کنید:",
        "menu_services": "📦 سرویس‌های من",
        "menu_buy_service": "🛒 خرید سرویس جدید",
        "menu_orders": "🧾 سفارش‌های من",
        "menu_add_service": "➕ افزودن سرویس موجود",
        "menu_support_requests": "🎫 درخواست‌های پشتیبانی من",
        "menu_language": "🌐 تغییر زبان",
        "menu_invite": "🎁 لینک دعوت من",
        "menu_wallet": "💰 کیف پول",
        "wallet_balance": "موجودی کیف پول شما: <b>{balance}</b> تومان",
        "wallet_topup_button": "➕ افزایش اعتبار",
        "wallet_history_button": "📜 تاریخچه",
        "wallet_topup_enter_amount": "مبلغ افزایش اعتبار را به تومان ارسال کنید (فقط عدد):",
        "wallet_topup_invalid_amount": "مبلغ معتبر نیست. یک عدد صحیح مثبت به تومان ارسال کنید.",
        "wallet_topup_payment": "برای افزایش اعتبار، مبلغ <b>{amount}</b> تومان را به کارت زیر واریز کنید:\n\n{card}\n\nسپس تصویر رسید را همین‌جا ارسال کنید.",
        "wallet_topup_pending": "✅ رسید افزایش اعتبار ثبت شد. پس از تأیید مدیر، مبلغ به موجودی شما اضافه می‌شود.",
        "wallet_topup_approved": "✅ افزایش اعتبار {amount} تومان تأیید شد. موجودی جدید: {balance} تومان.",
        "wallet_topup_rejected": "❌ درخواست افزایش اعتبار شما تأیید نشد. برای بررسی بیشتر با پشتیبانی تماس بگیرید.",
        "wallet_history_title": "📜 آخرین تراکنش‌های کیف پول:",
        "wallet_history_empty": "هنوز تراکنشی در کیف پول شما ثبت نشده است.",
        "wallet_history_line": "{date} • {type} • {amount} تومان{card}",
        "wallet_type_topup": "افزایش اعتبار",
        "wallet_type_purchase": "خرید سرویس",
        "wallet_type_renewal": "تمدید سرویس",
        "wallet_type_refund": "بازگشت وجه",
        "wallet_type_adjust": "تصحیح دستی",
        "wallet_insufficient": "اعتبار شما {balance} تومان است؛ برای این سفارش {needed} تومان دیگر نیاز دارید.",
        "pay_from_wallet": "💰 پرداخت از کیف پول (موجودی: {balance})",
        "pay_by_card": "💳 کارت به کارت",
        "renewal_payment": "برای تمدید، مبلغ <b>{amount}</b> تومان را به کارت زیر واریز کنید:\n\n{card}\n\nسپس تصویر رسید را همین‌جا ارسال کنید.",
        "admin_edit_card": "✏️ ویرایش کارت",
        "admin_card_updated": "✅ کارت ثبت‌شده روی درخواست #{request_id} به‌روزرسانی شد:\n{card}",
        "request_rejected_refund": "❌ درخواست شما توسط مدیر رد شد و مبلغ {amount} تومان به کیف پول شما بازگردانده شد. موجودی جدید: {balance} تومان.",
        "purchase_rejected_refund": "❌ پرداخت سفارش شما تأیید نشد و مبلغ {amount} تومان به کیف پول شما بازگردانده شد. موجودی جدید: {balance} تومان.",
        "invite_link": "لینک دعوت شما:\n{link}\n\nدوستانتان با این لینک وارد ربات شوند تا دعوت موفق شما ثبت شود.",
        "invite_unavailable": "لینک دعوت هنوز آماده نیست؛ کمی بعد دوباره تلاش کنید.",
        "promo_code_button": "🏷 کد تخفیف دارم",
        "promo_code_prompt": "کد تخفیف خود را ارسال کنید (برای انصراف «-» بفرستید):",
        "promo_code_saved": "✅ کد تخفیف ثبت شد و روی خرید بعدی اعمال می‌شود.",
        "promo_code_cleared": "کد تخفیف حذف شد.",
        "promo_code_invalid": "این کد تخفیف معتبر نیست یا منقضی شده است.",
        "promo_discount_block": "مبلغ اصلی: <s>{original}</s> تومان\nتخفیف: {discount} تومان\n",
        "menu_trial": "🎁 دریافت تست رایگان",
        "trial_unavailable": "در حال حاضر سرویس تست برای این ربات فعال نیست.",
        "trial_already_used": "قبلاً از سرویس تست استفاده کرده‌اید. هر شماره موبایل فقط یک بار می‌تواند تست بگیرد.",
        "trial_success": "✅ سرویس تست شما ساخته شد!\n\n{link}",
        "trial_failed": "ساخت سرویس تست ناموفق بود. کمی بعد دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.",
        "emergency_button": "🆘 دسترسی اضطراری",
        "emergency_unavailable": "دسترسی اضطراری برای این ربات فعال نیست.",
        "emergency_cooldown": "اخیراً برای این سرویس از دسترسی اضطراری استفاده شده است. لطفاً بعداً دوباره تلاش کنید یا سرویس را تمدید کنید.",
        "emergency_success": "✅ دسترسی اضطراری فعال شد: {days} روز و {volume} گیگابایت به سرویس شما اضافه شد.",
        "emergency_failed": "فعال‌سازی دسترسی اضطراری ناموفق بود. لطفاً با پشتیبانی تماس بگیرید.",
        "no_owned_services": "هنوز سرویسی به حساب شما متصل نشده است.",
        "owned_services": "سرویس‌های متصل به حساب شما:",
        "server_button": "🖥 سرور",
        "account_button": "👤 اکانت",
        "service_details": "جزئیات سرویس",
        "service_server": "🖥 سرور",
        "service_account": "👤 نام اکانت",
        "service_status": "وضعیت",
        "service_expiry": "⏳ انقضا",
        "service_usage": "📊 مصرف‌شده",
        "service_remaining": "📦 باقیمانده",
        "service_updated": "🕒 اطلاعات",
        "service_live": "زنده",
        "service_unavailable": "در حال بروزرسانی",
        "status_active": "فعال",
        "status_inactive": "غیرفعال",
        "status_expired": "منقضی‌شده — نیازمند تمدید",
        "status_volume_ended": "حجم تمام‌شده — نیازمند تمدید",
        "status_volume_low": "حجم رو به پایان",
        "status_expiring_soon": "نزدیک انقضا — نیازمند تمدید",
        "status_unknown": "نامشخص",
        "unlimited": "نامحدود",
        "not_started": "پس از اولین اتصال",
        "get_link_button": "🔗 دریافت لینک اتصال",
        "renew_button": "♻️ درخواست تمدید",
        "support_button": "🛟 پشتیبانی",
        "back_services_button": "⬅️ سرویس‌های من",
        "choose_package": "پکیج تمدید را انتخاب کنید:",
        "renew_pending": "✅ درخواست تمدید ثبت شد. پس از بررسی پرداخت، مدیر نتیجه را اطلاع می‌دهد.",
        "renew_duplicate": "درخواست تمدید #{request_id} هنوز در حال بررسی است. برای جلوگیری از ثبت و پرداخت تکراری، درخواست تازه‌ای ساخته نشد. می‌توانید درخواست قبلی را لغو کنید یا منتظر تصمیم مدیر بمانید.",
        "renew_cancel_button": "لغو درخواست تمدید",
        "renew_cancelled": "درخواست تمدید #{request_id} لغو شد. اکنون می‌توانید درخواست تازه‌ای ثبت کنید.",
        "support_prompt": "پیام، تصویر یا فایل پشتیبانی خود را ارسال کنید.",
        "support_pending": "✅ پیام شما در درخواست پشتیبانی #{request_id} ثبت شد.",
        "support_no_tickets": "هنوز درخواست پشتیبانی ندارید.",
        "support_ticket_list": "درخواست‌های پشتیبانی شما:",
        "support_ticket_title": "درخواست پشتیبانی #{request_id}",
        "support_status_waiting_admin": "منتظر پاسخ پشتیبانی",
        "support_status_in_progress": "در حال بررسی توسط پشتیبانی",
        "support_status_waiting_customer": "منتظر پاسخ شما",
        "support_status_closed": "بسته‌شده",
        "support_sender_admin": "پشتیبانی",
        "support_sender_customer": "شما",
        "support_continue_button": "💬 ادامه گفتگو",
        "support_new_button": "➕ درخواست جدید برای این سرویس",
        "support_back_button": "⬅️ درخواست‌های من",
        "support_view_button": "🎫 مشاهده درخواست",
        "support_ticket_missing": "این درخواست پیدا نشد یا دیگر در دسترس نیست.",
        "request_completed": "✅ درخواست شما توسط مدیر تکمیل شد.",
        "request_rejected": "❌ درخواست شما توسط مدیر رد شد.",
        "link_unavailable": "لینک اتصال این سرویس فعلاً در دسترس نیست؛ درخواست شما به مدیر اطلاع داده شد.",
        "invalid_service": "این سرویس در حساب شما وجود ندارد یا دسترسی آن لغو شده است.",
        "choose_purchase_server": "سرور سرویس جدید را انتخاب کنید:",
        "choose_purchase_package": "پکیج خرید را انتخاب کنید:",
        "purchase_payment": "{discount_block}برای ثبت سفارش، مبلغ <b>{amount}</b> تومان را به کارت زیر واریز کنید:\n\n{card}\n\nسپس تصویر رسید را همین‌جا ارسال کنید.",
        "payment_unavailable": "فعلاً خرید برای این انتخاب فعال نیست یا کارت پرداختی تعریف نشده است. کمی بعد دوباره تلاش کنید.",
        "receipt_prompt": "لطفاً تصویر رسید کارت‌به‌کارت را ارسال کنید.",
        "receipt_invalid": "رسید باید عکس یا فایل JPG/PNG/WebP/PDF و حداکثر ۱۰ مگابایت باشد.",
        "purchase_pending": "✅ رسید و سفارش شما ثبت شد. پس از بررسی دستی مدیر، نتیجه همین‌جا اعلام می‌شود.",
        "purchase_duplicate": "یک سفارش پرداخت‌شده‌ی در حال بررسی دارید؛ ابتدا نتیجه همان سفارش مشخص می‌شود.",
        "purchase_account_name_prompt": "نام دلخواه اکانت را با ۳ تا ۳۲ کاراکتر انگلیسی، عدد، خط تیره یا زیرخط ارسال کنید. مثال: navid_01",
        "purchase_account_name_invalid": "نام اکانت معتبر نیست. باید ۳ تا ۳۲ کاراکتر باشد، با حرف یا عدد شروع شود و فقط شامل حروف انگلیسی، عدد، - یا _ باشد.",
        "purchase_account_name_taken": "این نام روی سرور انتخاب‌شده وجود دارد. نام دیگری ارسال کنید.",
        "purchase_approved": "✅ پرداخت سفارش شما تأیید شد، اما ساخت خودکار سرویس هنوز کامل نشده است. مدیر دوباره تلاش می‌کند و نتیجه را همین‌جا می‌فرستد.",
        "purchase_completed": "✅ پرداخت تأیید و سرویس شما ساخته شد.\nنام اکانت: {account_name}\n{delivery_link}",
        "purchase_rejected": "❌ پرداخت سفارش شما تأیید نشد. برای بررسی بیشتر با پشتیبانی تماس بگیرید.",
        "purchase_orders_empty": "هنوز سفارشی ثبت نکرده‌اید.",
        "purchase_orders_list": "سفارش‌های اخیر شما:",
        "purchase_order_title": "سفارش خرید #{order_id}",
        "purchase_order_missing": "این سفارش پیدا نشد یا متعلق به حساب شما نیست.",
        "purchase_status_pending": "در انتظار بررسی پرداخت",
        "purchase_status_approved": "پرداخت تأیید شده؛ در انتظار ساخت سرویس",
        "purchase_status_completed": "تکمیل‌شده",
        "purchase_status_rejected": "ردشده",
        "purchase_status_cancelled": "لغوشده",
        "purchase_order_amount": "مبلغ",
        "purchase_order_package": "پکیج",
        "purchase_order_refresh": "🔄 بروزرسانی وضعیت",
        "purchase_order_buy_again": "🛒 خرید دوباره",
        "start_first": "برای شروع /start را بزنید.",
        "test_restricted": "این ربات فعلاً در حالت تست خصوصی است.",
    },
    "en": {
        "choose_language": "Choose your bot language:",
        "share_phone": "To identify your accounts, share your own phone number using the button below.",
        "share_button": "📱 Share my number",
        "phone_invalid": "That number is not valid. Please share your Iranian mobile number using the button.",
        "phone_mismatch": "For security, only a phone number Telegram confirms belongs to you is accepted.",
        "phone_conflict": "This Telegram account is already linked to another customer. An admin must review it.",
        "verified": "✅ Your phone is verified. Next, we will find and link your services together.",
        "no_candidates": "No service was found for this phone yet. Please contact support for a manual check.",
        "choose_service": "We found these possible services. Select one to prove ownership:",
        "send_subscription": "Send the Subscription link for this service. It is checked only and will not be stored.",
        "invalid_subscription": "That link does not match the selected service. Check it and try again.",
        "proof_limited": "Too many failed attempts. Try again in 15 minutes.",
        "service_attached": "✅ Service ownership verified and added to your account.",
        "admin_review": "Your request was submitted for manual admin review.",
        "claim_conflict": "This service has another owner and requires admin review.",
        "claim_rejected": "An admin did not approve this service ownership request.",
        "no_link_button": "I do not have the links",
        "service_button": "Service",
        "welcome_menu": "Welcome to Eve. Choose an option below:",
        "menu_services": "📦 My services",
        "menu_buy_service": "🛒 Buy new service",
        "menu_orders": "🧾 My orders",
        "menu_add_service": "➕ Add existing service",
        "menu_support_requests": "🎫 My support requests",
        "menu_language": "🌐 Change language",
        "menu_invite": "🎁 My invite link",
        "menu_wallet": "💰 Wallet",
        "wallet_balance": "Your wallet balance: <b>{balance}</b> Toman",
        "wallet_topup_button": "➕ Top up",
        "wallet_history_button": "📜 History",
        "wallet_topup_enter_amount": "Send the top-up amount in Toman (digits only):",
        "wallet_topup_invalid_amount": "Invalid amount. Send a positive whole number in Toman.",
        "wallet_topup_payment": "To top up your wallet, transfer <b>{amount}</b> Toman to the card below:\n\n{card}\n\nThen send the receipt image here.",
        "wallet_topup_pending": "✅ Your top-up receipt was recorded. An admin will approve it and the amount will be added to your balance.",
        "wallet_topup_approved": "✅ Your {amount} Toman top-up was approved. New balance: {balance} Toman.",
        "wallet_topup_rejected": "❌ Your top-up request was not approved. Contact support for more details.",
        "wallet_history_title": "📜 Your latest wallet transactions:",
        "wallet_history_empty": "You have no wallet transactions yet.",
        "wallet_history_line": "{date} • {type} • {amount} Toman{card}",
        "wallet_type_topup": "Top-up",
        "wallet_type_purchase": "Service purchase",
        "wallet_type_renewal": "Service renewal",
        "wallet_type_refund": "Refund",
        "wallet_type_adjust": "Manual adjustment",
        "wallet_insufficient": "Your credit is {balance} Toman; this order needs {needed} Toman more.",
        "pay_from_wallet": "💰 Pay from wallet (balance: {balance})",
        "pay_by_card": "💳 Card to card",
        "renewal_payment": "To renew, transfer <b>{amount}</b> Toman to the card below:\n\n{card}\n\nThen send the receipt image here.",
        "admin_edit_card": "✏️ Edit card",
        "admin_card_updated": "✅ The card recorded on request #{request_id} was updated:\n{card}",
        "request_rejected_refund": "❌ Your request was rejected and {amount} Toman was refunded to your wallet. New balance: {balance} Toman.",
        "purchase_rejected_refund": "❌ Your payment was not approved and {amount} Toman was refunded to your wallet. New balance: {balance} Toman.",
        "invite_link": "Your invite link:\n{link}\n\nWhen your friends join the bot through this link, your referral counts.",
        "invite_unavailable": "The invite link is not ready yet; try again later.",
        "promo_code_button": "🏷 I have a promo code",
        "promo_code_prompt": "Send your promo code (send «-» to cancel):",
        "promo_code_saved": "✅ Promo code saved and will apply to your next purchase.",
        "promo_code_cleared": "Promo code removed.",
        "promo_code_invalid": "This promo code is invalid or expired.",
        "promo_discount_block": "Original: <s>{original}</s> Toman\nDiscount: {discount} Toman\n",
        "menu_trial": "🎁 Get free trial",
        "trial_unavailable": "The free trial is not available on this bot right now.",
        "trial_already_used": "You have already used the free trial. Each phone number can claim it only once.",
        "trial_success": "✅ Your trial service is ready!\n\n{link}",
        "trial_failed": "Could not create the trial service. Try again later or contact support.",
        "emergency_button": "🆘 Emergency access",
        "emergency_unavailable": "Emergency access is not enabled on this bot.",
        "emergency_cooldown": "Emergency access was already used for this service recently. Try again later or renew the service.",
        "emergency_success": "✅ Emergency access activated: {days} day(s) and {volume} GB were added to your service.",
        "emergency_failed": "Could not activate emergency access. Please contact support.",
        "no_owned_services": "No service is linked to your account yet.",
        "owned_services": "Services linked to your account:",
        "server_button": "🖥 Server",
        "account_button": "👤 Account",
        "service_details": "Service details",
        "service_server": "🖥 Server",
        "service_account": "👤 Account name",
        "service_status": "Status",
        "service_expiry": "⏳ Expiry",
        "service_usage": "📊 Used",
        "service_remaining": "📦 Remaining",
        "service_updated": "🕒 Data",
        "service_live": "Live",
        "service_unavailable": "Updating",
        "status_active": "Active",
        "status_inactive": "Inactive",
        "status_expired": "Expired — renewal required",
        "status_volume_ended": "Volume ended — renewal required",
        "status_volume_low": "Low volume",
        "status_expiring_soon": "Expiring soon — renewal required",
        "status_unknown": "Unknown",
        "unlimited": "Unlimited",
        "not_started": "After first connection",
        "get_link_button": "🔗 Get connection link",
        "renew_button": "♻️ Request renewal",
        "support_button": "🛟 Support",
        "back_services_button": "⬅️ My services",
        "choose_package": "Choose a renewal package:",
        "renew_pending": "✅ Renewal request recorded. An admin will review the payment and update you.",
        "renew_duplicate": "Renewal request #{request_id} is still under review. No duplicate request was created. You can cancel the earlier request or wait for an admin decision.",
        "renew_cancel_button": "Cancel renewal request",
        "renew_cancelled": "Renewal request #{request_id} was cancelled. You can now submit a new request.",
        "support_prompt": "Send your support message, image, or file.",
        "support_pending": "✅ Your message was added to support request #{request_id}.",
        "support_no_tickets": "You do not have any support requests yet.",
        "support_ticket_list": "Your support requests:",
        "support_ticket_title": "Support request #{request_id}",
        "support_status_waiting_admin": "Waiting for support",
        "support_status_in_progress": "In progress with support",
        "support_status_waiting_customer": "Waiting for you",
        "support_status_closed": "Closed",
        "support_sender_admin": "Support",
        "support_sender_customer": "You",
        "support_continue_button": "💬 Continue conversation",
        "support_new_button": "➕ New request for this service",
        "support_back_button": "⬅️ My requests",
        "support_view_button": "🎫 View request",
        "support_ticket_missing": "This request was not found or is no longer available.",
        "request_completed": "✅ Your request was completed by an admin.",
        "request_rejected": "❌ Your request was rejected by an admin.",
        "link_unavailable": "The connection link is not available right now; an admin was notified.",
        "invalid_service": "This service is not in your account or access was revoked.",
        "choose_purchase_server": "Choose a server for the new service:",
        "choose_purchase_package": "Choose a purchase package:",
        "purchase_payment": "{discount_block}Transfer <b>{amount}</b> Toman to the card below:\n\n{card}\n\nThen send the receipt image here.",
        "payment_unavailable": "Purchasing is not available for this selection or no active payment card is configured. Try again later.",
        "receipt_prompt": "Please send the card-transfer receipt image.",
        "receipt_invalid": "The receipt must be a JPG/PNG/WebP/PDF up to 10 MB.",
        "purchase_pending": "✅ Your receipt and order were recorded. An admin will review it manually and notify you here.",
        "purchase_duplicate": "You already have a paid order under review. Wait for its result first.",
        "purchase_account_name_prompt": "Send the desired account name using 3-32 ASCII letters, numbers, dash, or underscore. Example: navid_01",
        "purchase_account_name_invalid": "Invalid account name. Use 3-32 characters, start with a letter or number, and use only ASCII letters, numbers, - or _.",
        "purchase_account_name_taken": "That name already exists on the selected server. Send another name.",
        "purchase_approved": "✅ Your payment was approved, but automatic provisioning has not completed yet. An admin will retry and update you here.",
        "purchase_completed": "✅ Payment approved and your service was created.\nAccount: {account_name}\n{delivery_link}",
        "purchase_rejected": "❌ Your payment was not approved. Contact support for more details.",
        "purchase_orders_empty": "You have not placed any orders yet.",
        "purchase_orders_list": "Your recent orders:",
        "purchase_order_title": "Purchase order #{order_id}",
        "purchase_order_missing": "This order was not found or does not belong to your account.",
        "purchase_status_pending": "Waiting for payment review",
        "purchase_status_approved": "Payment approved; waiting for provisioning",
        "purchase_status_completed": "Completed",
        "purchase_status_rejected": "Rejected",
        "purchase_status_cancelled": "Cancelled",
        "purchase_order_amount": "Amount",
        "purchase_order_package": "Package",
        "purchase_order_refresh": "🔄 Refresh status",
        "purchase_order_buy_again": "🛒 Buy again",
        "start_first": "Send /start to begin.",
        "test_restricted": "This bot is currently in private test mode.",
    },
}


def language_keyboard(enabled_languages: list[str]):
    labels = {"fa": "فارسی 🇮🇷", "en": "English 🇬🇧"}
    return {
        "inline_keyboard": [[
            {"text": labels[lang], "callback_data": f"lang:{lang}"}
            for lang in enabled_languages if lang in labels
        ]]
    }


def contact_keyboard(language: str):
    lang = language if language in COPY else "fa"
    return {
        "keyboard": [[{"text": COPY[lang]["share_button"], "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "input_field_placeholder": COPY[lang]["share_button"],
    }


def main_menu_keyboard(language: str, show_trial: bool = False):
    lang = language if language in COPY else "fa"
    rows = []
    if show_trial:
        rows.append([{"text": COPY[lang]["menu_trial"]}])
    rows += [
        [{"text": COPY[lang]["menu_services"]}, {"text": COPY[lang]["menu_buy_service"]}],
        [{"text": COPY[lang]["menu_orders"]}, {"text": COPY[lang]["menu_wallet"]}],
        [{"text": COPY[lang]["menu_add_service"]}, {"text": COPY[lang]["menu_support_requests"]}],
        [{"text": COPY[lang]["menu_invite"]}, {"text": COPY[lang]["menu_language"]}],
    ]
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
    }
