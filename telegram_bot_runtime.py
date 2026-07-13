"""Small, token-safe Telegram Bot API client and localized onboarding UI."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from telegram_diagnostics import redact_connection_error


API_ROOT = "https://api.telegram.org"


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

    def call(self, method: str, payload: dict[str, Any] | None = None,
             *, long_poll_timeout: int = 0) -> tuple[Any, str]:
        errors: list[str] = []
        connect_timeout = 10
        read_timeout = max(15, int(long_poll_timeout) + 10)
        url = f"{API_ROOT}/bot{self._token}/{method}"
        for route in self._routes:
            started = time.perf_counter()
            try:
                response = requests.post(
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
                errors.append(f"{route.name}: {safe}")
            except TelegramApiError as exc:
                if not exc.retryable:
                    raise
                errors.append(f"{route.name}: {exc}")
            except requests.RequestException as exc:
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

    def copy_message(self, chat_id: int, from_chat_id: int, message_id: int):
        return self.call("copyMessage", {
            "chat_id": int(chat_id),
            "from_chat_id": int(from_chat_id),
            "message_id": int(message_id),
        })

    def send_photo(self, chat_id: int, file_id: str):
        return self.call("sendPhoto", {
            "chat_id": int(chat_id),
            "photo": str(file_id),
        })

    def send_document(self, chat_id: int, file_id: str):
        return self.call("sendDocument", {
            "chat_id": int(chat_id),
            "document": str(file_id),
        })

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
        "menu_add_service": "➕ افزودن سرویس موجود",
        "menu_language": "🌐 تغییر زبان",
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
        "support_prompt": "پیام پشتیبانی خود را در یک پیام ارسال کنید.",
        "support_pending": "✅ پیام شما برای پشتیبانی ثبت شد.",
        "request_completed": "✅ درخواست شما توسط مدیر تکمیل شد.",
        "request_rejected": "❌ درخواست شما توسط مدیر رد شد.",
        "link_unavailable": "لینک اتصال این سرویس فعلاً در دسترس نیست؛ درخواست شما به مدیر اطلاع داده شد.",
        "invalid_service": "این سرویس در حساب شما وجود ندارد یا دسترسی آن لغو شده است.",
        "choose_purchase_server": "سرور سرویس جدید را انتخاب کنید:",
        "choose_purchase_package": "پکیج خرید را انتخاب کنید:",
        "purchase_payment": "برای ثبت سفارش، مبلغ <b>{amount}</b> تومان را به کارت زیر واریز کنید:\n\n{card}\n\nسپس تصویر رسید را همین‌جا ارسال کنید.",
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
        "menu_add_service": "➕ Add existing service",
        "menu_language": "🌐 Change language",
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
        "support_prompt": "Send your support message in one message.",
        "support_pending": "✅ Your support message was recorded.",
        "request_completed": "✅ Your request was completed by an admin.",
        "request_rejected": "❌ Your request was rejected by an admin.",
        "link_unavailable": "The connection link is not available right now; an admin was notified.",
        "invalid_service": "This service is not in your account or access was revoked.",
        "choose_purchase_server": "Choose a server for the new service:",
        "choose_purchase_package": "Choose a purchase package:",
        "purchase_payment": "Transfer <b>{amount}</b> Toman to the card below:\n\n{card}\n\nThen send the receipt image here.",
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


def main_menu_keyboard(language: str):
    lang = language if language in COPY else "fa"
    return {
        "keyboard": [
            [{"text": COPY[lang]["menu_services"]}, {"text": COPY[lang]["menu_buy_service"]}],
            [{"text": COPY[lang]["menu_add_service"]}, {"text": COPY[lang]["menu_language"]}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }
