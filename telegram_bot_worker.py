"""Dedicated durable long-polling worker for Eve Telegram bots."""

from __future__ import annotations

import os
import signal
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse

os.environ.setdefault("DISABLE_BACKGROUND_THREADS", "true")
os.environ.setdefault("EVE_PROCESS_ROLE", "telegram-bot")

from app import (  # noqa: E402
    Admin,
    CustomerAccount,
    OwnershipClaim,
    OwnershipClaimItem,
    ServiceOwnership,
    TelegramBotInstance,
    TelegramBotRuntime,
    TelegramBotTestUser,
    TelegramBotUserState,
    TelegramIdentity,
    TelegramOwnershipSession,
    _telegram_bot_api_client,
    _decrypt_telegram_secret,
    app,
    db,
    discover_phone_ownership_claim,
    normalize_iran_mobile,
    review_ownership_claim_item,
    verify_ownership_claim_subscription,
)
from telegram_bot_runtime import (  # noqa: E402
    COPY,
    TelegramApiError,
    TelegramBotApi,
    contact_keyboard,
    language_keyboard,
    main_menu_keyboard,
)
from telegram_diagnostics import redact_connection_error  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402


running = True
worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
webhook_prepared_bot_ids: set[int] = set()


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


def _send_main_menu(api: TelegramBotApi, chat_id: int, language: str):
    lang = language if language in COPY else "fa"
    api.send_message(
        chat_id, COPY[lang]["welcome_menu"],
        reply_markup=main_menu_keyboard(lang),
    )


def _send_owned_services(api: TelegramBotApi, chat_id: int, language: str,
                         identity: TelegramIdentity | None):
    lang = language if language in COPY else "fa"
    if identity is None or not identity.customer_id:
        api.send_message(chat_id, COPY[lang]["no_owned_services"])
        return
    ownerships = ServiceOwnership.query.filter_by(
        customer_id=identity.customer_id, revoked_at=None,
    ).order_by(ServiceOwnership.id.asc()).all()
    if not ownerships:
        api.send_message(chat_id, COPY[lang]["no_owned_services"])
        return
    lines = [COPY[lang]["owned_services"]]
    for index, ownership in enumerate(ownerships, 1):
        label = ownership.client_email_snapshot or f'{COPY[lang]["service_button"]} {index}'
        server_name = getattr(ownership.server, 'name', '') or ''
        lines.append(f"{index}. {label}" + (f" — {server_name}" if server_name else ""))
    api.send_message(chat_id, "\n".join(lines))


def _send_claim_candidates(api: TelegramBotApi, chat_id: int, language: str,
                           claim: OwnershipClaim | None):
    if claim is None:
        api.send_message(chat_id, COPY[language]["no_candidates"])
        return
    items = [item for item in claim.items if item.status in ("pending", "conflict")][:20]
    if not items:
        return
    keyboard = []
    for index, item in enumerate(items, 1):
        label = str(item.client_email_snapshot or f'{COPY[language]["service_button"]} {index}')
        keyboard.append([{
            "text": label[:48],
            "callback_data": f"claim:{item.id}",
        }])
    keyboard.append([{
        "text": COPY[language]["no_link_button"],
        "callback_data": f"claim-none:{claim.id}",
    }])
    api.send_message(
        chat_id, COPY[language]["choose_service"],
        reply_markup={"inline_keyboard": keyboard},
    )


def _ownership_session(bot_id: int, user_id: int, claim_id: int) -> TelegramOwnershipSession:
    row = TelegramOwnershipSession.query.filter_by(
        bot_instance_id=bot_id, telegram_user_id=user_id,
    ).first()
    if row is None:
        row = TelegramOwnershipSession(
            bot_instance_id=bot_id, telegram_user_id=user_id, claim_id=claim_id,
        )
        db.session.add(row)
    row.claim_id = claim_id
    return row


def _extract_subscription_token(value: str) -> str:
    raw = str(value or '').strip()
    if len(raw) > 2048:
        return ''
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ''
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
        return ''
    parts = [unquote(part) for part in parsed.path.split('/') if part]
    token = parts[-1].strip() if parts else ''
    if not token or len(token) > 256 or any(char in token for char in '/\\?#@:'):
        return ''
    return token


def _notify_claim_admins(api: TelegramBotApi, claim: OwnershipClaim, user_id: int):
    text = (
        f"Telegram ownership review requested\n"
        f"Claim: #{claim.id}\nTelegram user: {user_id}\n"
        f"Phone: {claim.verified_phone}\nCandidates: {len(claim.items)}"
    )
    pending_items = [item for item in claim.items if item.status in ('pending', 'conflict')][:20]
    keyboard = []
    for item in pending_items:
        label = str(item.client_email_snapshot or f'Service #{item.id}')[:32]
        keyboard.append([
            {"text": f"✅ {label}", "callback_data": f"admin-claim:{item.id}:approve"},
            {"text": "❌ Reject", "callback_data": f"admin-claim:{item.id}:reject"},
        ])
    for admin in Admin.query.filter_by(enabled=True).all():
        if not (admin.is_superadmin or admin.role in ('admin', 'superadmin')):
            continue
        try:
            admin_chat_id = int(str(admin.telegram_id or '').strip())
            if admin_chat_id > 0 and admin_chat_id != user_id:
                extra = {"reply_markup": {"inline_keyboard": keyboard}} if keyboard else {}
                api.send_message(admin_chat_id, text, **extra)
        except (TypeError, ValueError, TelegramApiError):
            continue


def _telegram_admin(user_id: int):
    for admin in Admin.query.filter_by(enabled=True).all():
        try:
            if int(str(admin.telegram_id or '').strip()) != int(user_id):
                continue
        except (TypeError, ValueError):
            continue
        if admin.is_superadmin or admin.role in ('admin', 'superadmin'):
            return admin
    return None


def _handle_admin_claim_callback(api: TelegramBotApi, callback: dict, data: str) -> bool:
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'admin-claim' or parts[2] not in ('approve', 'reject'):
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    message = callback.get('message') or {}
    chat_id = int(((message.get('chat') or {}).get('id')) or 0)
    reviewer = _telegram_admin(int(sender.get('id') or 0))
    if not reviewer:
        if callback_id:
            api.answer_callback(callback_id, 'Access denied')
        return True
    try:
        result = review_ownership_claim_item(
            int(parts[1]), reviewer, approve=(parts[2] == 'approve'),
            rejection_reason=('Rejected from Telegram review' if parts[2] == 'reject' else None),
        )
        if callback_id:
            api.answer_callback(callback_id, 'Saved')
        if chat_id:
            api.send_message(chat_id, f"Claim item #{parts[1]}: {result.get('status')}")
        item = db.session.get(OwnershipClaimItem, int(parts[1]))
        if item and item.claim.telegram_identity.telegram_chat_id:
            language = item.claim.customer.preferred_language or 'fa'
            key = ('service_attached' if result.get('status') == 'approved'
                   else 'claim_rejected' if result.get('status') == 'rejected'
                   else 'claim_conflict')
            api.send_message(item.claim.telegram_identity.telegram_chat_id, COPY.get(language, COPY['fa'])[key])
    except (ValueError, PermissionError) as exc:
        if callback_id:
            api.answer_callback(callback_id, str(exc)[:120])
    return True


def _handle_start(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int,
                  user_id: int, state: TelegramBotUserState):
    languages = bot.enabled_languages()
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if identity and identity.customer_id and identity.phone_verified_at:
        customer = db.session.get(CustomerAccount, identity.customer_id)
        preferred = str(getattr(customer, 'preferred_language', '') or '')
        if preferred in languages:
            state.language = preferred
        state.step = "verified"
        db.session.flush()
        _send_main_menu(api, chat_id, state.language)
        return
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
    if data.startswith('admin-claim:'):
        _handle_admin_claim_callback(api, callback, data)
        return
    if not _is_allowed(bot, user_id):
        api.answer_callback(callback_id)
        return
    state = _state(bot, user_id)
    if data.startswith("lang:"):
        language = data.partition(":")[2]
        if language in bot.enabled_languages():
            state.language = language
            identity = _identity(sender, chat_id)
            if identity.customer_id and identity.phone_verified_at:
                customer = db.session.get(CustomerAccount, identity.customer_id)
                if customer is not None:
                    customer.preferred_language = language
                state.step = "verified"
            else:
                state.step = "share_contact"
            db.session.flush()
            api.answer_callback(callback_id, "✓")
            if state.step == "verified":
                _send_main_menu(api, chat_id, language)
            else:
                _send_contact_prompt(api, chat_id, language)
            return
    if data.startswith("claim:"):
        try:
            item_id = int(data.partition(":")[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        item = OwnershipClaimItem.query.filter_by(id=item_id).first()
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        if not item or not identity or item.claim.telegram_identity_id != identity.id:
            api.answer_callback(callback_id)
            return
        session_row = _ownership_session(bot.id, user_id, item.claim_id)
        session_row.selected_item_id = item.id
        state.step = "awaiting_subscription"
        db.session.flush()
        api.answer_callback(callback_id, "✓")
        api.send_message(chat_id, COPY[state.language]["send_subscription"])
        return
    if data.startswith("claim-none:"):
        try:
            claim_id = int(data.partition(":")[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        claim = OwnershipClaim.query.filter_by(id=claim_id).first()
        if not claim or not identity or claim.telegram_identity_id != identity.id:
            api.answer_callback(callback_id)
            return
        claim.claim_method = "admin_review"
        claim.status = "pending"
        state.step = "needs_review"
        session_row = _ownership_session(bot.id, user_id, claim.id)
        session_row.selected_item_id = None
        db.session.flush()
        api.answer_callback(callback_id, "✓")
        api.send_message(chat_id, COPY[state.language]["admin_review"])
        _notify_claim_admins(api, claim, user_id)
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
    _send_main_menu(api, chat_id, language)
    claim = discover_phone_ownership_claim(identity)
    _send_claim_candidates(api, chat_id, language, claim)


def _handle_subscription(api: TelegramBotApi, bot: TelegramBotInstance, message: dict,
                         sender: dict, state: TelegramBotUserState, text: str):
    chat_id = int((message.get("chat") or {}).get("id"))
    user_id = int(sender["id"])
    language = state.language if state.language in COPY else bot.default_language
    session_row = TelegramOwnershipSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id,
    ).first()
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    item = db.session.get(OwnershipClaimItem, session_row.selected_item_id) if session_row else None
    if not identity or not identity.customer_id or not item:
        state.step = "verified"
        db.session.flush()
        api.send_message(chat_id, COPY[language]["start_first"])
        return
    if session_row.locked_until and session_row.locked_until > datetime.utcnow():
        api.send_message(chat_id, COPY[language]["proof_limited"])
        return
    token = _extract_subscription_token(text)
    if not token:
        api.send_message(chat_id, COPY[language]["invalid_subscription"])
        return
    result = verify_ownership_claim_subscription(item, identity.customer_id, token)
    if result.get("status") == "invalid_subscription":
        session_row.failed_attempts = int(session_row.failed_attempts or 0) + 1
        if session_row.failed_attempts >= 5:
            session_row.locked_until = datetime.utcnow() + timedelta(minutes=15)
        db.session.flush()
        api.send_message(chat_id, COPY[language]["invalid_subscription"])
        return
    if result.get("status") == "conflict":
        state.step = "needs_review"
        session_row.selected_item_id = None
        db.session.flush()
        api.send_message(chat_id, COPY[language]["claim_conflict"])
        _notify_claim_admins(api, item.claim, user_id)
        return
    state.step = "verified"
    session_row.selected_item_id = None
    session_row.failed_attempts = 0
    session_row.locked_until = None
    db.session.flush()
    api.send_message(chat_id, COPY[language]["service_attached"])
    _send_main_menu(api, chat_id, language)
    _send_claim_candidates(api, chat_id, language, item.claim)


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
    elif text in {COPY["fa"]["menu_services"], COPY["en"]["menu_services"]}:
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        _send_owned_services(api, chat_id, state.language, identity)
    elif text in {COPY["fa"]["menu_add_service"], COPY["en"]["menu_add_service"]}:
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        if identity and identity.customer_id and identity.phone_verified_at:
            claim = discover_phone_ownership_claim(identity)
            _send_claim_candidates(api, chat_id, state.language, claim)
        else:
            _send_contact_prompt(api, chat_id, state.language)
    elif text in {COPY["fa"]["menu_language"], COPY["en"]["menu_language"]}:
        state.step = "choose_language"
        db.session.flush()
        api.send_message(
            chat_id, COPY[state.language]["choose_language"],
            reply_markup=language_keyboard(bot.enabled_languages()),
        )
    elif message.get("contact"):
        _handle_contact(api, bot, message, sender, state)
    elif state.step == "awaiting_subscription":
        _handle_subscription(api, bot, message, sender, state, text)
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
        time.sleep(5)
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
        if bot.id not in webhook_prepared_bot_ids:
            _result, route_name = api.delete_webhook()
            webhook_prepared_bot_ids.add(bot.id)
            runtime.last_route = route_name
            runtime.last_heartbeat_at = datetime.utcnow()
            db.session.commit()
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
            bots = TelegramBotInstance.query.filter_by(transport_mode="polling").all()
            if not bots:
                time.sleep(3)
                continue
            # The first release has one central bot. Keeping this loop explicit
            # makes adding one worker thread per reseller bot a contained next step.
            for bot in bots:
                if not running:
                    break
                if bot.enabled:
                    _poll_bot(bot)
                    continue
                runtime = _runtime(bot.id)
                runtime.worker_id = worker_id
                runtime.status = "disabled"
                runtime.last_error = "Bot is disabled in settings"
                runtime.last_heartbeat_at = datetime.utcnow()
                runtime.lease_expires_at = datetime.utcnow() + timedelta(seconds=60)
                db.session.commit()
                time.sleep(1)


if __name__ == "__main__":
    main()
