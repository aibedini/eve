"""Dedicated durable long-polling worker for Eve Telegram bots."""

from __future__ import annotations

import os
import signal
import time
import uuid
from datetime import datetime, timedelta

os.environ.setdefault("DISABLE_BACKGROUND_THREADS", "true")
os.environ.setdefault("EVE_PROCESS_ROLE", "telegram-bot")

from app import (  # noqa: E402
    CustomerAccount,
    TelegramBotInstance,
    TelegramBotRuntime,
    TelegramBotTestUser,
    TelegramBotUserState,
    TelegramIdentity,
    _telegram_bot_api_client,
    _decrypt_telegram_secret,
    app,
    db,
    normalize_iran_mobile,
)
from telegram_bot_runtime import (  # noqa: E402
    COPY,
    TelegramApiError,
    TelegramBotApi,
    contact_keyboard,
    language_keyboard,
)
from telegram_diagnostics import redact_connection_error  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402


running = True
worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _stop(_signum, _frame):
    global running
    running = False


def _runtime(bot_id: int) -> TelegramBotRuntime:
    row = TelegramBotRuntime.query.filter_by(bot_instance_id=bot_id).first()
    if row is None:
        row = TelegramBotRuntime(bot_instance_id=bot_id)
        db.session.add(row)
        db.session.flush()
    return row


def _claim_lease(bot_id: int) -> TelegramBotRuntime | None:
    now = datetime.utcnow()
    row = TelegramBotRuntime.query.filter_by(bot_instance_id=bot_id).with_for_update().first()
    if row is None:
        row = TelegramBotRuntime(bot_instance_id=bot_id)
        db.session.add(row)
        try:
            db.session.flush()
        except IntegrityError:
            # Another worker created the singleton row concurrently. Re-enter
            # the transaction and lock that row before evaluating its lease.
            db.session.rollback()
            row = TelegramBotRuntime.query.filter_by(
                bot_instance_id=bot_id,
            ).with_for_update().one()
    if row.worker_id and row.worker_id != worker_id and row.lease_expires_at and row.lease_expires_at > now:
        db.session.rollback()
        return None
    row.worker_id = worker_id
    row.lease_expires_at = now + timedelta(seconds=60)
    row.last_heartbeat_at = now
    row.status = "running"
    db.session.commit()
    return row


def _state(bot: TelegramBotInstance, user_id: int) -> TelegramBotUserState:
    row = TelegramBotUserState.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id,
    ).first()
    if row is None:
        row = TelegramBotUserState(
            bot_instance_id=bot.id,
            telegram_user_id=user_id,
            language=bot.default_language,
        )
        db.session.add(row)
        db.session.flush()
    return row


def _identity(sender: dict, chat_id: int) -> TelegramIdentity:
    user_id = int(sender["id"])
    row = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if row is None:
        row = TelegramIdentity(telegram_user_id=user_id)
        db.session.add(row)
    row.telegram_chat_id = int(chat_id)
    row.username = str(sender.get("username") or "")[:64] or None
    row.first_name = str(sender.get("first_name") or "")[:128] or None
    row.last_name = str(sender.get("last_name") or "")[:128] or None
    row.last_seen_at = datetime.utcnow()
    return row


def _is_allowed(bot: TelegramBotInstance, user_id: int) -> bool:
    if not bot.test_mode:
        return True
    return TelegramBotTestUser.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, enabled=True,
    ).first() is not None


def _send_contact_prompt(api: TelegramBotApi, chat_id: int, language: str):
    api.send_message(
        chat_id, COPY[language]["share_phone"],
        reply_markup=contact_keyboard(language),
    )


def _handle_start(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int,
                  user_id: int, state: TelegramBotUserState):
    languages = bot.enabled_languages()
    if len(languages) > 1:
        state.step = "choose_language"
        db.session.flush()
        api.send_message(
            chat_id, COPY[state.language]["choose_language"],
            reply_markup=language_keyboard(languages),
        )
        return
    state.language = languages[0]
    state.step = "share_contact"
    db.session.flush()
    _send_contact_prompt(api, chat_id, state.language)


def _handle_callback(api: TelegramBotApi, bot: TelegramBotInstance, callback: dict):
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    user_id = int(sender.get("id") or 0)
    chat_id = int(chat.get("id") or 0)
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    if not user_id or not chat_id or not callback_id:
        return
    if not _is_allowed(bot, user_id):
        api.answer_callback(callback_id)
        return
    state = _state(bot, user_id)
    if data.startswith("lang:"):
        language = data.partition(":")[2]
        if language in bot.enabled_languages():
            state.language = language
            state.step = "share_contact"
            _identity(sender, chat_id)
            db.session.flush()
            api.answer_callback(callback_id, "✓")
            _send_contact_prompt(api, chat_id, language)
            return
    api.answer_callback(callback_id)


def _handle_contact(api: TelegramBotApi, bot: TelegramBotInstance, message: dict,
                    sender: dict, state: TelegramBotUserState):
    chat_id = int((message.get("chat") or {}).get("id"))
    user_id = int(sender["id"])
    language = state.language if state.language in COPY else bot.default_language
    contact = message.get("contact") or {}
    if int(contact.get("user_id") or 0) != user_id:
        api.send_message(chat_id, COPY[language]["phone_mismatch"])
        return
    phone = normalize_iran_mobile(contact.get("phone_number"))
    if not phone:
        api.send_message(chat_id, COPY[language]["phone_invalid"])
        return

    identity = _identity(sender, chat_id)
    if identity.customer_id:
        current = db.session.get(CustomerAccount, identity.customer_id)
        if current and current.primary_phone and current.primary_phone != phone:
            state.step = "needs_review"
            db.session.flush()
            api.send_message(chat_id, COPY[language]["phone_conflict"])
            return
    customer = CustomerAccount.query.filter_by(primary_phone=phone).first()
    if customer is not None:
        other_identity = TelegramIdentity.query.filter(
            TelegramIdentity.customer_id == customer.id,
            TelegramIdentity.telegram_user_id != user_id,
            TelegramIdentity.status == 'active',
        ).first()
        if other_identity is not None:
            state.step = "needs_review"
            db.session.flush()
            api.send_message(chat_id, COPY[language]["phone_conflict"])
            return
    if customer is None:
        customer = CustomerAccount(
            primary_phone=phone,
            phone_verified_at=datetime.utcnow(),
            preferred_language=language,
        )
        db.session.add(customer)
        db.session.flush()
    customer.phone_verified_at = datetime.utcnow()
    customer.preferred_language = language
    if not customer.display_name:
        customer.display_name = " ".join(filter(None, [
            sender.get("first_name"), sender.get("last_name"),
        ]))[:120] or None
    identity.customer_id = customer.id
    identity.set_verified_phone(phone)
    state.step = "verified"
    db.session.flush()
    api.send_message(
        chat_id, COPY[language]["verified"],
        reply_markup={"remove_keyboard": True},
    )


def _handle_message(api: TelegramBotApi, bot: TelegramBotInstance, message: dict):
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or not sender.get("id") or not chat.get("id"):
        return
    user_id = int(sender["id"])
    chat_id = int(chat["id"])
    if not _is_allowed(bot, user_id):
        return
    state = _state(bot, user_id)
    _identity(sender, chat_id)
    db.session.flush()
    text = str(message.get("text") or "").strip()
    if text == "/start" or text.startswith("/start "):
        _handle_start(api, bot, chat_id, user_id, state)
    elif message.get("contact"):
        _handle_contact(api, bot, message, sender, state)
    elif state.step == "share_contact":
        _send_contact_prompt(api, chat_id, state.language)
    else:
        api.send_message(chat_id, COPY[state.language]["start_first"])


def process_update(api: TelegramBotApi, bot: TelegramBotInstance, update: dict):
    if isinstance(update.get("callback_query"), dict):
        _handle_callback(api, bot, update["callback_query"])
    elif isinstance(update.get("message"), dict):
        _handle_message(api, bot, update["message"])


def _poll_bot(bot: TelegramBotInstance):
    runtime = _claim_lease(bot.id)
    if runtime is None:
        return
    token = _decrypt_telegram_secret(bot.token_encrypted)
    if not token:
        runtime.status = "blocked"
        runtime.last_error = "Bot token or Telegram route is not configured"
        runtime.last_heartbeat_at = datetime.utcnow()
        db.session.commit()
        time.sleep(max(5, min(int(getattr(exc, 'retry_after', 0) or 0), 60)))
        return
    try:
        api = _telegram_bot_api_client(bot)
    except ValueError as exc:
        runtime.status = "blocked"
        runtime.last_error = str(exc)
        runtime.last_heartbeat_at = datetime.utcnow()
        db.session.commit()
        time.sleep(5)
        return
    try:
        updates, route_name = api.get_updates(int(runtime.next_update_id or 0), timeout=25)
        runtime = _runtime(bot.id)
        runtime.status = "running"
        runtime.last_route = route_name
        runtime.last_error = None
        runtime.last_heartbeat_at = datetime.utcnow()
        runtime.lease_expires_at = datetime.utcnow() + timedelta(seconds=60)
        db.session.commit()
        for update in updates if isinstance(updates, list) else []:
            update_id = int(update.get("update_id") or 0)
            try:
                process_update(api, bot, update)
                runtime = _runtime(bot.id)
                runtime.next_update_id = max(int(runtime.next_update_id or 0), update_id + 1)
                runtime.last_update_at = datetime.utcnow()
                runtime.failed_update_id = None
                runtime.failed_update_count = 0
                runtime.last_heartbeat_at = datetime.utcnow()
                db.session.commit()
            except Exception as exc:
                db.session.rollback()
                runtime = _runtime(bot.id)
                if int(runtime.failed_update_id or -1) == update_id:
                    runtime.failed_update_count = int(runtime.failed_update_count or 0) + 1
                else:
                    runtime.failed_update_id = update_id
                    runtime.failed_update_count = 1
                runtime.status = "error"
                runtime.last_error = redact_connection_error(exc, (token,))
                runtime.last_heartbeat_at = datetime.utcnow()
                if runtime.failed_update_count >= 3:
                    runtime.next_update_id = update_id + 1
                    runtime.last_error = f"Skipped update {update_id} after 3 failures: {runtime.last_error}"
                    runtime.failed_update_id = None
                    runtime.failed_update_count = 0
                db.session.commit()
                break
    except TelegramApiError as exc:
        db.session.rollback()
        runtime = _runtime(bot.id)
        runtime.status = "error"
        runtime.last_error = redact_connection_error(exc, (token,))
        runtime.last_heartbeat_at = datetime.utcnow()
        runtime.lease_expires_at = datetime.utcnow() + timedelta(seconds=60)
        db.session.commit()
        time.sleep(5)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while running:
        with app.app_context():
            bots = TelegramBotInstance.query.filter_by(enabled=True, transport_mode="polling").all()
            if not bots:
                time.sleep(3)
                continue
            # The first release has one central bot. Keeping this loop explicit
            # makes adding one worker thread per reseller bot a contained next step.
            for bot in bots:
                if not running:
                    break
                _poll_bot(bot)


if __name__ == "__main__":
    main()
