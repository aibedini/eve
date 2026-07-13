"""Dedicated durable long-polling worker for Eve Telegram bots."""

from __future__ import annotations

import html
import json
import os
import random
import re
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

os.environ.setdefault("DISABLE_BACKGROUND_THREADS", "true")
os.environ.setdefault("EVE_PROCESS_ROLE", "telegram-bot")

from app import (  # noqa: E402
    Admin,
    BankCard,
    CustomerAccount,
    GLOBAL_SERVER_DATA,
    OwnershipClaim,
    OwnershipClaimItem,
    Package,
    Server,
    ServiceOwnership,
    TelegramBotInstance,
    TelegramBotRuntime,
    TelegramBotTestUser,
    TelegramBotUserState,
    TelegramIdentity,
    TelegramOwnershipSession,
    TelegramPurchaseRequest,
    TelegramPurchaseRequestDetail,
    TelegramPurchaseNameDraft,
    TelegramPurchasePolicy,
    TelegramPurchaseServerRule,
    TelegramPurchaseSession,
    TelegramServiceRequest,
    TelegramServiceSession,
    _telegram_bot_api_client,
    _decrypt_telegram_secret,
    _public_base_url,
    app,
    db,
    discover_phone_ownership_claim,
    load_snapshot_from_redis,
    normalize_iran_mobile,
    parse_allowed_servers,
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
    keyboard = []
    current_server_id = None
    for index, ownership in enumerate(ownerships[:50], 1):
        if ownership.server_id != current_server_id:
            current_server_id = ownership.server_id
            server_name = getattr(ownership.server, 'name', '') or f'#{ownership.server_id}'
            keyboard.append([{
                "text": f'{COPY[lang]["server_button"]}: {server_name}'[:60],
                "callback_data": "noop",
            }])
        label = ownership.client_email_snapshot or f'{COPY[lang]["service_button"]} {index}'
        keyboard.append([{
            "text": f'{COPY[lang]["account_button"]}: {label}'[:60],
            "callback_data": f"service:{ownership.id}",
        }])
    api.send_message(
        chat_id, COPY[lang]["owned_services"],
        reply_markup={"inline_keyboard": keyboard},
    )


def _owned_service(user_id: int, ownership_id: int):
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not identity or not identity.customer_id:
        return identity, None
    ownership = ServiceOwnership.query.filter_by(
        id=int(ownership_id), customer_id=identity.customer_id, revoked_at=None,
    ).first()
    return identity, ownership


def _cached_owned_service(ownership: ServiceOwnership):
    email = str(ownership.client_email_snapshot or '').strip().lower()
    client_uuid = str(ownership.client_uuid or '').strip().lower()
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(inbound.get('server_id') or 0) != int(ownership.server_id):
                continue
        except (TypeError, ValueError):
            continue
        for client in (inbound.get('clients') or []):
            candidate_email = str(client.get('email') or '').strip().lower()
            raw = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
            candidate_uuid = str(client.get('id') or client.get('uuid') or raw.get('id') or '').strip().lower()
            if (client_uuid and candidate_uuid == client_uuid) or (email and candidate_email == email):
                return client
    return None


def _format_traffic(value) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        size = 0
    if size >= 1024 ** 3:
        return f"{size / (1024 ** 3):.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / (1024 ** 2):.1f} MB"
    return f"{size / 1024:.1f} KB"


def _service_expiry(client: dict | None, language: str) -> str:
    if not client:
        return COPY[language]["service_unavailable"]
    raw = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
    try:
        expiry_ts = int(client.get('expiryTimestamp') or raw.get('expiryTime') or 0)
    except (TypeError, ValueError):
        expiry_ts = 0
    if expiry_ts == 0:
        return COPY[language]["unlimited"]
    if expiry_ts < 0:
        days = max(1, int(round(abs(expiry_ts) / 86400000)))
        return f"{days} " + ("روز پس از اولین اتصال" if language == 'fa' else "days after first connection")
    expiry = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
    remaining_days = int((expiry - datetime.now(timezone.utc)).total_seconds() // 86400)
    date_text = expiry.astimezone(ZoneInfo('Asia/Tehran')).strftime('%Y-%m-%d')
    if remaining_days < 0:
        return f"{date_text} ({COPY[language]['status_expired']})"
    suffix = f"{remaining_days} روز" if language == 'fa' else f"{remaining_days} days"
    return f"{date_text} ({suffix})"


def _service_status(client: dict | None, language: str) -> str:
    if not client:
        return f"⚪ {COPY[language]['status_unknown']}"
    key = str(client.get('service_state') or '').strip().lower()
    if not key:
        key = 'active' if client.get('enable', True) else 'inactive'
    labels = {
        'active': ('🟢', 'status_active'),
        'inactive': ('⚪', 'status_inactive'),
        'expired': ('🔴', 'status_expired'),
        'volume_ended': ('🔴', 'status_volume_ended'),
        'volume_low': ('🟠', 'status_volume_low'),
        'expiring_soon': ('🟠', 'status_expiring_soon'),
    }
    emoji, copy_key = labels.get(key, ('⚪', 'status_unknown'))
    return f"{emoji} {COPY[language][copy_key]}"


def _service_keyboard(ownership: ServiceOwnership, language: str):
    return {"inline_keyboard": [
        [{"text": COPY[language]["get_link_button"], "callback_data": f"service-link:{ownership.id}"}],
        [
            {"text": COPY[language]["renew_button"], "callback_data": f"service-renew:{ownership.id}"},
            {"text": COPY[language]["support_button"], "callback_data": f"service-support:{ownership.id}"},
        ],
        [{"text": COPY[language]["back_services_button"], "callback_data": "service-list"}],
    ]}


def _send_service_details(api: TelegramBotApi, chat_id: int, language: str,
                          ownership: ServiceOwnership):
    lang = language if language in COPY else 'fa'
    client = _cached_owned_service(ownership)
    server_name = getattr(ownership.server, 'name', '') or f'#{ownership.server_id}'
    account = ownership.client_email_snapshot or f'#{ownership.id}'
    if client:
        used = max(0, int(client.get('up') or 0)) + max(0, int(client.get('down') or 0))
        remaining = client.get('remaining_bytes')
        remaining_text = COPY[lang]['unlimited'] if remaining in (None, -1) else _format_traffic(remaining)
        freshness = COPY[lang]['service_live']
    else:
        used = None
        remaining_text = COPY[lang]['service_unavailable']
        freshness = COPY[lang]['service_unavailable']
    text = "\n".join([
        f"<b>{COPY[lang]['service_details']}</b>",
        f"{COPY[lang]['service_server']}: <b>{html.escape(str(server_name))}</b>",
        f"{COPY[lang]['service_account']}: <code>{html.escape(str(account))}</code>",
        f"{COPY[lang]['service_status']}: {_service_status(client, lang)}",
        f"{COPY[lang]['service_expiry']}: {_service_expiry(client, lang)}",
        f"{COPY[lang]['service_usage']}: {_format_traffic(used) if used is not None else COPY[lang]['service_unavailable']}",
        f"{COPY[lang]['service_remaining']}: {remaining_text}",
        f"{COPY[lang]['service_updated']}: {freshness}",
    ])
    api.send_message(
        chat_id, text, parse_mode='HTML',
        reply_markup=_service_keyboard(ownership, lang),
    )


def _service_session(bot_id: int, user_id: int) -> TelegramServiceSession:
    row = TelegramServiceSession.query.filter_by(
        bot_instance_id=bot_id, telegram_user_id=user_id,
    ).first()
    if row is None:
        row = TelegramServiceSession(bot_instance_id=bot_id, telegram_user_id=user_id)
        db.session.add(row)
    return row


def _available_packages(ownership: ServiceOwnership):
    packages = Package.query.filter_by(enabled=True).order_by(
        Package.display_order.asc(), Package.id.asc(),
    ).all()
    visible = []
    for package in packages:
        scope = str(package.scope or 'global').lower()
        if scope == 'global':
            visible.append(package)
            continue
        try:
            assigned = {int(value) for value in json.loads(package.assigned_reseller_ids or '[]')}
        except (TypeError, ValueError):
            assigned = set()
        if ownership.reseller_id and int(ownership.reseller_id) in assigned:
            visible.append(package)
    return visible[:20]


def _send_renew_packages(api: TelegramBotApi, bot: TelegramBotInstance,
                         chat_id: int, user_id: int, language: str,
                         ownership: ServiceOwnership):
    packages = _available_packages(ownership)
    if not packages:
        request_row, duplicate = _create_service_request(
            bot.id, user_id, ownership, 'renewal', package=None, note=None,
        )
        api.send_message(chat_id, COPY[language]['renew_duplicate' if duplicate else 'renew_pending'])
        if not duplicate:
            _notify_service_request_admins(api, request_row)
        return
    keyboard = []
    for package in packages:
        price = f"{int(package.price or 0):,} T"
        keyboard.append([{
            "text": f"{package.name} • {price}"[:60],
            "callback_data": f"renew-package:{ownership.id}:{package.id}",
        }])
    keyboard.append([{"text": COPY[language]['back_services_button'],
                      "callback_data": f"service:{ownership.id}"}])
    api.send_message(
        chat_id, COPY[language]['choose_package'],
        reply_markup={"inline_keyboard": keyboard},
    )


def _purchase_servers(bot: TelegramBotInstance):
    query = Server.query.filter_by(enabled=True, hidden=False)
    servers = query.order_by(Server.name.asc(), Server.id.asc()).all()
    if not bot.owner_admin_id:
        return servers[:30]
    owner = db.session.get(Admin, bot.owner_admin_id)
    raw_allowed = parse_allowed_servers(getattr(owner, 'allowed_servers', None)) if owner else []
    if raw_allowed == '*':
        return servers[:30]
    allowed = set()
    for value in raw_allowed if isinstance(raw_allowed, list) else []:
        try:
            allowed.add(int(value.get('server_id') if isinstance(value, dict) else value))
        except (TypeError, ValueError):
            continue
    return [server for server in servers if server.id in allowed][:30]


def _purchase_policy_values(bot: TelegramBotInstance):
    policy = db.session.get(TelegramPurchasePolicy, bot.id)
    return {
        'customer_selects_server': bool(policy.customer_selects_server) if policy else False,
        'assignment_strategy': (policy.assignment_strategy if policy else None) or 'least_clients',
        'account_name_mode': (policy.account_name_mode if policy else None) or 'generated',
        'account_name_template': (
            policy.account_name_template if policy else None
        ) or 'tg{order_id}-{phone_last4}',
    }


def _purchase_server_rules(bot: TelegramBotInstance):
    return {
        row.server_id: row for row in TelegramPurchaseServerRule.query.filter_by(
            bot_instance_id=bot.id,
        ).all()
    }


def _eligible_purchase_servers(bot: TelegramBotInstance):
    servers = _purchase_servers(bot)
    rules = _purchase_server_rules(bot)
    if not rules:
        return servers
    return [server for server in servers if rules.get(server.id) and rules[server.id].eligible]


def _server_active_client_counts():
    counts = {}
    healthy = set()
    for status in GLOBAL_SERVER_DATA.get('servers_status') or []:
        try:
            server_id = int(status.get('server_id'))
        except (TypeError, ValueError, AttributeError):
            continue
        stats = status.get('stats') if isinstance(status.get('stats'), dict) else {}
        counts[server_id] = int(stats.get('active_clients', stats.get('total_clients', 0)) or 0)
        if status.get('success'):
            healthy.add(server_id)
    return counts, healthy


def _assign_purchase_server(bot: TelegramBotInstance):
    servers = _eligible_purchase_servers(bot)
    if not servers:
        return None
    counts, healthy_ids = _server_active_client_counts()
    healthy_servers = [server for server in servers if server.id in healthy_ids]
    if healthy_servers:
        servers = healthy_servers
    rules = _purchase_server_rules(bot)
    strategy = _purchase_policy_values(bot)['assignment_strategy']
    priority = lambda server: int(getattr(rules.get(server.id), 'priority', 100) or 100)
    if strategy == 'priority':
        return min(servers, key=lambda server: (priority(server), counts.get(server.id, 0), server.id))
    if strategy == 'weighted_random':
        weights = [max(1, int(getattr(rules.get(server.id), 'weight', 1) or 1)) for server in servers]
        return random.choices(servers, weights=weights, k=1)[0]
    if strategy == 'random':
        return random.choice(servers)
    return min(servers, key=lambda server: (counts.get(server.id, 0), priority(server), server.id))


def _purchase_packages(bot: TelegramBotInstance):
    packages = Package.query.filter_by(enabled=True).order_by(
        Package.display_order.asc(), Package.id.asc(),
    ).all()
    visible = []
    for package in packages:
        scope = str(package.scope or 'global').lower()
        if scope == 'global':
            visible.append(package)
            continue
        try:
            assigned = {int(value) for value in json.loads(package.assigned_reseller_ids or '[]')}
        except (TypeError, ValueError):
            assigned = set()
        if bot.owner_admin_id and (
            int(bot.owner_admin_id) in assigned or
            (scope == 'personal' and int(package.created_by or 0) == int(bot.owner_admin_id))
        ):
            visible.append(package)
    return visible[:30]


def _purchase_session(bot_id: int, user_id: int) -> TelegramPurchaseSession:
    row = TelegramPurchaseSession.query.filter_by(
        bot_instance_id=bot_id, telegram_user_id=user_id,
    ).first()
    if row is None:
        row = TelegramPurchaseSession(bot_instance_id=bot_id, telegram_user_id=user_id)
        db.session.add(row)
    return row


def _send_purchase_servers(api: TelegramBotApi, bot: TelegramBotInstance,
                           chat_id: int, language: str):
    rules = _purchase_server_rules(bot)
    servers = [
        server for server in _eligible_purchase_servers(bot)
        if rules.get(server.id) and rules[server.id].customer_visible
    ]
    if not servers:
        api.send_message(chat_id, COPY[language]['payment_unavailable'])
        return
    keyboard = [[{
        'text': f"{COPY[language]['server_button']}: {(rules[server.id].display_name or server.name)}"[:60],
        'callback_data': f'buy-server:{server.id}',
    }] for server in servers]
    api.send_message(
        chat_id, COPY[language]['choose_purchase_server'],
        reply_markup={'inline_keyboard': keyboard},
    )


def _send_purchase_packages(api: TelegramBotApi, bot: TelegramBotInstance,
                            chat_id: int, language: str, server: Server | None):
    packages = _purchase_packages(bot)
    if not packages:
        api.send_message(chat_id, COPY[language]['payment_unavailable'])
        return
    keyboard = [[{
        'text': f"{package.name} • {int(package.price or 0):,} T"[:60],
        'callback_data': f'buy-package:{server.id if server else 0}:{package.id}',
    }] for package in packages]
    api.send_message(
        chat_id, COPY[language]['choose_purchase_package'],
        reply_markup={'inline_keyboard': keyboard},
    )


def _format_bank_card(card: BankCard) -> str:
    lines = []
    if card.label:
        lines.append(f"<b>{html.escape(card.label)}</b>")
    if card.bank_name:
        lines.append(html.escape(card.bank_name))
    if card.card_number:
        number = ''.join(filter(str.isdigit, card.card_number)) or card.card_number
        grouped = ' '.join(number[index:index + 4] for index in range(0, len(number), 4))
        lines.append(f"<code>{html.escape(grouped)}</code>")
    if card.owner_name:
        lines.append(html.escape(card.owner_name))
    if card.iban:
        lines.append(f"IBAN: <code>{html.escape(card.iban)}</code>")
    return '\n'.join(lines)


def _purchase_name_draft(bot_id: int, user_id: int):
    return TelegramPurchaseNameDraft.query.filter_by(
        bot_instance_id=bot_id, telegram_user_id=user_id,
    ).first()


def _purchase_account_name_exists(server_id: int, account_name: str) -> bool:
    wanted = str(account_name or '').strip().casefold()
    if not wanted:
        return False
    for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
        try:
            if int(inbound.get('server_id') or 0) != int(server_id):
                continue
        except (TypeError, ValueError):
            continue
        for client in inbound.get('clients') or []:
            if str(client.get('email') or '').strip().casefold() == wanted:
                return True
    return False


def _continue_purchase_selection(api: TelegramBotApi, bot: TelegramBotInstance,
                                 chat_id: int, user_id: int, language: str,
                                 server: Server, package: Package,
                                 state: TelegramBotUserState):
    policy = _purchase_policy_values(bot)
    old_draft = _purchase_name_draft(bot.id, user_id)
    if old_draft:
        db.session.delete(old_draft)
    if policy['account_name_mode'] == 'customer':
        session_row = _purchase_session(bot.id, user_id)
        session_row.server_id = server.id
        session_row.package_id = package.id
        session_row.bank_card_id = None
        session_row.action = 'awaiting_account_name'
        state.step = 'awaiting_purchase_account_name'
        db.session.flush()
        api.send_message(chat_id, COPY[language]['purchase_account_name_prompt'])
        return
    _begin_purchase_payment(api, bot, chat_id, user_id, language, server, package, state)


def _render_purchase_account_name(bot: TelegramBotInstance,
                                  request_row: TelegramPurchaseRequest,
                                  customer: CustomerAccount,
                                  requested_name: str | None):
    if requested_name:
        return requested_name
    template = _purchase_policy_values(bot)['account_name_template']
    phone = ''.join(filter(str.isdigit, str(customer.primary_phone or '')))
    try:
        rendered = template.format(
            order_id=request_row.id,
            phone_last4=(phone[-4:] if phone else '0000'),
            random4=uuid.uuid4().hex[:4],
        )
    except (KeyError, ValueError):
        rendered = f'tg{request_row.id}-{phone[-4:] if phone else "0000"}'
    rendered = re.sub(r'[^A-Za-z0-9_-]+', '', rendered)[:64]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{2,63}', rendered or ''):
        rendered = f'tg{request_row.id}-{uuid.uuid4().hex[:4]}'
    if _purchase_account_name_exists(request_row.server_id, rendered):
        rendered = f'{rendered[:58]}-{uuid.uuid4().hex[:4]}'
    return rendered


def _begin_purchase_payment(api: TelegramBotApi, bot: TelegramBotInstance,
                            chat_id: int, user_id: int, language: str,
                            server: Server, package: Package,
                            state: TelegramBotUserState):
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not identity or not identity.customer_id or not identity.phone_verified_at:
        _send_contact_prompt(api, chat_id, language)
        return
    duplicate = TelegramPurchaseRequest.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, status='pending',
    ).first()
    if duplicate:
        api.send_message(chat_id, COPY[language]['purchase_duplicate'])
        return
    card = BankCard.query.filter_by(is_active=True).order_by(BankCard.id.asc()).first()
    if card is None:
        api.send_message(chat_id, COPY[language]['payment_unavailable'])
        return
    session_row = _purchase_session(bot.id, user_id)
    session_row.server_id = server.id
    session_row.package_id = package.id
    session_row.bank_card_id = card.id
    session_row.action = 'awaiting_receipt'
    state.step = 'awaiting_purchase_receipt'
    db.session.flush()
    api.send_message(
        chat_id,
        COPY[language]['purchase_payment'].format(
            amount=f"{int(package.price or 0):,}", card=_format_bank_card(card),
        ),
        parse_mode='HTML',
    )


def _receipt_from_message(message: dict):
    photos = message.get('photo')
    if isinstance(photos, list) and photos:
        photo = photos[-1] if isinstance(photos[-1], dict) else {}
        if int(photo.get('file_size') or 0) <= 10 * 1024 * 1024 and photo.get('file_id'):
            return 'photo', str(photo['file_id']), str(photo.get('file_unique_id') or '')
    document = message.get('document') if isinstance(message.get('document'), dict) else {}
    mime = str(document.get('mime_type') or '').lower()
    allowed = {'image/jpeg', 'image/png', 'image/webp', 'application/pdf'}
    if (document.get('file_id') and mime in allowed and
            int(document.get('file_size') or 0) <= 10 * 1024 * 1024):
        return 'document', str(document['file_id']), str(document.get('file_unique_id') or '')
    return None


def _send_claim_candidates(api: TelegramBotApi, chat_id: int, language: str,
                           claim: OwnershipClaim | None):
    if claim is None:
        api.send_message(chat_id, COPY[language]["no_candidates"])
        return
    items = [item for item in claim.items if item.status in ("pending", "conflict")][:20]
    if not items:
        return
    keyboard = []
    current_server_id = None
    for index, item in enumerate(items, 1):
        if item.server_id != current_server_id:
            current_server_id = item.server_id
            server_name = getattr(item.server, 'name', '') or f'#{item.server_id}'
            keyboard.append([{
                "text": f'{COPY[language]["server_button"]}: {server_name}'[:60],
                "callback_data": "noop",
            }])
        label = str(item.client_email_snapshot or f'{COPY[language]["service_button"]} {index}')
        keyboard.append([{
            "text": f'{COPY[language]["account_button"]}: {label}'[:60],
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
            if admin_chat_id > 0:
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


def _create_service_request(bot_id: int, user_id: int, ownership: ServiceOwnership,
                            request_type: str, *, package: Package | None, note: str | None):
    if request_type == 'renewal':
        existing = TelegramServiceRequest.query.filter_by(
            service_ownership_id=ownership.id,
            request_type='renewal', status='pending',
        ).first()
        if existing is not None:
            return existing, True
    row = TelegramServiceRequest(
        bot_instance_id=bot_id,
        telegram_user_id=user_id,
        customer_id=ownership.customer_id,
        service_ownership_id=ownership.id,
        request_type=request_type,
        package_id=(package.id if package else None),
        amount=(int(package.price or 0) if package else None),
        note=(str(note or '').strip()[:4000] or None),
        status='pending',
    )
    db.session.add(row)
    db.session.flush()
    return row, False


def _notify_service_request_admins(api: TelegramBotApi, request_row: TelegramServiceRequest):
    ownership = request_row.ownership
    server_name = getattr(ownership.server, 'name', '') or f'#{ownership.server_id}'
    account = ownership.client_email_snapshot or f'#{ownership.id}'
    request_label = 'Renewal' if request_row.request_type == 'renewal' else 'Support'
    lines = [
        f"Telegram {request_label} request #{request_row.id}",
        f"Server: {server_name}",
        f"Account: {account}",
        f"Telegram user: {request_row.telegram_user_id}",
    ]
    if request_row.package:
        lines.append(f"Package: {request_row.package.name}")
        lines.append(f"Amount: {int(request_row.amount or 0):,} T")
    if request_row.note:
        lines.append(f"Message: {request_row.note[:1000]}")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Complete", "callback_data": f"admin-service:{request_row.id}:complete"},
        {"text": "❌ Reject", "callback_data": f"admin-service:{request_row.id}:reject"},
    ]]}
    for admin in Admin.query.filter_by(enabled=True).all():
        role = str(admin.role or '').lower()
        is_global_admin = bool(admin.is_superadmin or role in ('admin', 'superadmin'))
        is_owner_reseller = bool(role == 'reseller' and ownership.reseller_id == admin.id)
        if not (is_global_admin or is_owner_reseller):
            continue
        try:
            admin_chat_id = int(str(admin.telegram_id or '').strip())
            if admin_chat_id > 0:
                api.send_message(admin_chat_id, "\n".join(lines), reply_markup=keyboard)
        except (TypeError, ValueError, TelegramApiError):
            continue


def _service_request_reviewer(user_id: int, request_row: TelegramServiceRequest):
    try:
        wanted = int(user_id)
    except (TypeError, ValueError):
        return None
    for admin in Admin.query.filter_by(enabled=True).all():
        try:
            if int(str(admin.telegram_id or '').strip()) != wanted:
                continue
        except (TypeError, ValueError):
            continue
        role = str(admin.role or '').lower()
        if admin.is_superadmin or role in ('admin', 'superadmin'):
            return admin
        if role == 'reseller' and request_row.ownership.reseller_id == admin.id:
            return admin
    return None


def _handle_admin_service_callback(api: TelegramBotApi, callback: dict, data: str) -> bool:
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'admin-service' or parts[2] not in ('complete', 'reject'):
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    try:
        request_row = db.session.get(TelegramServiceRequest, int(parts[1]))
    except (TypeError, ValueError):
        request_row = None
    reviewer = _service_request_reviewer(int(sender.get('id') or 0), request_row) if request_row else None
    if not request_row or not reviewer:
        if callback_id:
            api.answer_callback(callback_id, 'Access denied')
        return True
    if request_row.status != 'pending':
        api.answer_callback(callback_id, 'Already reviewed')
        return True
    request_row.status = 'completed' if parts[2] == 'complete' else 'rejected'
    request_row.reviewed_by_admin_id = reviewer.id
    request_row.reviewed_at = datetime.utcnow()
    db.session.flush()
    api.answer_callback(callback_id, 'Saved')
    if chat_id:
        api.send_message(chat_id, f"Request #{request_row.id}: {request_row.status}")
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=request_row.telegram_user_id,
        customer_id=request_row.customer_id,
    ).first()
    if identity and identity.telegram_chat_id:
        customer = db.session.get(CustomerAccount, request_row.customer_id)
        language = str(getattr(customer, 'preferred_language', '') or 'fa')
        if language not in COPY:
            language = 'fa'
        key = 'request_completed' if request_row.status == 'completed' else 'request_rejected'
        api.send_message(identity.telegram_chat_id, COPY[language][key])
    return True


def _purchase_reviewer(user_id: int, request_row: TelegramPurchaseRequest):
    admin = _telegram_admin(user_id)
    if admin:
        return admin
    bot = db.session.get(TelegramBotInstance, request_row.bot_instance_id)
    owner = db.session.get(Admin, bot.owner_admin_id) if bot and bot.owner_admin_id else None
    try:
        owner_telegram_id = int(str(getattr(owner, 'telegram_id', '') or '').strip())
    except (TypeError, ValueError):
        owner_telegram_id = 0
    if owner and owner.enabled and owner_telegram_id == int(user_id):
        return owner
    return None


def _purchase_admins(request_row: TelegramPurchaseRequest):
    bot = db.session.get(TelegramBotInstance, request_row.bot_instance_id)
    admins = []
    for admin in Admin.query.filter_by(enabled=True).all():
        role = str(admin.role or '').lower()
        is_global = bool(admin.is_superadmin or role in ('admin', 'superadmin'))
        is_owner = bool(bot and bot.owner_admin_id and admin.id == bot.owner_admin_id)
        if is_global or is_owner:
            admins.append(admin)
    return admins


def _notify_purchase_admins(api: TelegramBotApi, request_row: TelegramPurchaseRequest):
    lines = [
        f"Telegram purchase request #{request_row.id}",
        f"Server: {request_row.server.name}",
        f"Package: {request_row.package.name}",
        f"Amount: {int(request_row.amount or 0):,} T",
        f"Telegram user: {request_row.telegram_user_id}",
    ]
    if request_row.detail:
        lines.append(f"Account name: {request_row.detail.account_name}")
    keyboard = {'inline_keyboard': [[
        {'text': '✅ Approve payment', 'callback_data': f'admin-purchase:{request_row.id}:approve'},
        {'text': '❌ Reject', 'callback_data': f'admin-purchase:{request_row.id}:reject'},
    ]]}
    for admin in _purchase_admins(request_row):
        try:
            admin_chat_id = int(str(admin.telegram_id or '').strip())
            if admin_chat_id <= 0:
                continue
        except (TypeError, ValueError):
            continue
        admin_lines = list(lines)
        try:
            if request_row.receipt_kind == 'document':
                api.send_document(admin_chat_id, request_row.receipt_file_id)
            else:
                api.send_photo(admin_chat_id, request_row.receipt_file_id)
        except TelegramApiError:
            admin_lines.append('Receipt media delivery failed; open the order from the panel.')
        try:
            api.send_message(admin_chat_id, '\n'.join(admin_lines), reply_markup=keyboard)
        except TelegramApiError:
            continue


def _handle_admin_purchase_callback(api: TelegramBotApi, callback: dict, data: str) -> bool:
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'admin-purchase' or parts[2] not in ('approve', 'reject'):
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    try:
        request_row = db.session.get(TelegramPurchaseRequest, int(parts[1]))
    except (TypeError, ValueError):
        request_row = None
    reviewer = _purchase_reviewer(int(sender.get('id') or 0), request_row) if request_row else None
    if not request_row or not reviewer:
        if callback_id:
            api.answer_callback(callback_id, 'Access denied')
        return True
    if request_row.status != 'pending':
        api.answer_callback(callback_id, 'Already reviewed')
        return True
    request_row.status = 'approved' if parts[2] == 'approve' else 'rejected'
    request_row.reviewed_by_admin_id = reviewer.id
    request_row.reviewed_at = datetime.utcnow()
    db.session.flush()
    api.answer_callback(callback_id, 'Saved')
    if chat_id:
        api.send_message(chat_id, f"Purchase #{request_row.id}: {request_row.status}")
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=request_row.telegram_user_id,
        customer_id=request_row.customer_id,
    ).first()
    if identity and identity.telegram_chat_id:
        customer = db.session.get(CustomerAccount, request_row.customer_id)
        language = str(getattr(customer, 'preferred_language', '') or 'fa')
        if language not in COPY:
            language = 'fa'
        key = 'purchase_approved' if request_row.status == 'approved' else 'purchase_rejected'
        api.send_message(identity.telegram_chat_id, COPY[language][key])
    return True


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
    if data.startswith('admin-service:'):
        _handle_admin_service_callback(api, callback, data)
        return
    if data.startswith('admin-purchase:'):
        _handle_admin_purchase_callback(api, callback, data)
        return
    if not _is_allowed(bot, user_id):
        api.answer_callback(callback_id)
        return
    state = _state(bot, user_id)
    language = state.language if state.language in COPY else bot.default_language
    if data == 'noop':
        api.answer_callback(callback_id)
        return
    if data == 'service-list':
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        api.answer_callback(callback_id)
        _send_owned_services(api, chat_id, language, identity)
        return
    if data.startswith('buy-server:'):
        try:
            server_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        rules = _purchase_server_rules(bot)
        server = next((
            row for row in _eligible_purchase_servers(bot)
            if row.id == server_id and rules.get(row.id) and rules[row.id].customer_visible
        ), None)
        api.answer_callback(callback_id)
        if server is None:
            api.send_message(chat_id, COPY[language]['payment_unavailable'])
            return
        _send_purchase_packages(api, bot, chat_id, language, server)
        return
    if data.startswith('buy-package:'):
        parts = data.split(':')
        try:
            server_id = int(parts[1])
            package_id = int(parts[2])
        except (IndexError, TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        policy_values = _purchase_policy_values(bot)
        if server_id == 0 and not policy_values['customer_selects_server']:
            server = _assign_purchase_server(bot)
        elif server_id > 0 and policy_values['customer_selects_server']:
            rules = _purchase_server_rules(bot)
            server = next((
                row for row in _eligible_purchase_servers(bot)
                if row.id == server_id and rules.get(row.id) and rules[row.id].customer_visible
            ), None)
        else:
            server = None
        package = next((row for row in _purchase_packages(bot) if row.id == package_id), None)
        api.answer_callback(callback_id)
        if server is None or package is None:
            api.send_message(chat_id, COPY[language]['payment_unavailable'])
            return
        _continue_purchase_selection(
            api, bot, chat_id, user_id, language, server, package, state,
        )
        return
    if data.startswith('service:'):
        try:
            ownership_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        _identity_row, ownership = _owned_service(user_id, ownership_id)
        if not ownership:
            api.answer_callback(callback_id)
            api.send_message(chat_id, COPY[language]['invalid_service'])
            return
        api.answer_callback(callback_id)
        _send_service_details(api, chat_id, language, ownership)
        return
    if data.startswith('service-link:'):
        try:
            ownership_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        _identity_row, ownership = _owned_service(user_id, ownership_id)
        if not ownership:
            api.answer_callback(callback_id)
            api.send_message(chat_id, COPY[language]['invalid_service'])
            return
        client = _cached_owned_service(ownership)
        raw = client.get('raw_client') if client and isinstance(client.get('raw_client'), dict) else {}
        sub_id = str((raw.get('subId') or client.get('subId')) if client else '').strip()
        base_url = _public_base_url().rstrip('/')
        api.answer_callback(callback_id)
        if sub_id and base_url:
            safe_sub_id = quote(sub_id, safe='')
            api.send_message(chat_id, f"{COPY[language]['get_link_button']}:\n{base_url}/s/{ownership.server_id}/{safe_sub_id}")
        else:
            request_row, _duplicate = _create_service_request(
                bot.id, user_id, ownership, 'support', package=None,
                note='Connection link unavailable in Telegram worker snapshot',
            )
            api.send_message(chat_id, COPY[language]['link_unavailable'])
            _notify_service_request_admins(api, request_row)
        return
    if data.startswith('service-renew:'):
        try:
            ownership_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        _identity_row, ownership = _owned_service(user_id, ownership_id)
        if not ownership:
            api.answer_callback(callback_id)
            api.send_message(chat_id, COPY[language]['invalid_service'])
            return
        api.answer_callback(callback_id)
        _send_renew_packages(api, bot, chat_id, user_id, language, ownership)
        return
    if data.startswith('renew-package:'):
        parts = data.split(':')
        try:
            ownership_id = int(parts[1])
            package_id = int(parts[2])
        except (IndexError, TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        _identity_row, ownership = _owned_service(user_id, ownership_id)
        package = db.session.get(Package, package_id)
        allowed_package_ids = {row.id for row in _available_packages(ownership)} if ownership else set()
        if not ownership or not package or package.id not in allowed_package_ids:
            api.answer_callback(callback_id)
            api.send_message(chat_id, COPY[language]['invalid_service'])
            return
        request_row, duplicate = _create_service_request(
            bot.id, user_id, ownership, 'renewal', package=package, note=None,
        )
        api.answer_callback(callback_id)
        api.send_message(chat_id, COPY[language]['renew_duplicate' if duplicate else 'renew_pending'])
        if not duplicate:
            _notify_service_request_admins(api, request_row)
        return
    if data.startswith('service-support:'):
        try:
            ownership_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        _identity_row, ownership = _owned_service(user_id, ownership_id)
        if not ownership:
            api.answer_callback(callback_id)
            api.send_message(chat_id, COPY[language]['invalid_service'])
            return
        session_row = _service_session(bot.id, user_id)
        session_row.service_ownership_id = ownership.id
        session_row.action = 'support'
        state.step = 'awaiting_support_message'
        db.session.flush()
        api.answer_callback(callback_id)
        api.send_message(chat_id, COPY[language]['support_prompt'])
        return
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


def _handle_support_message(api: TelegramBotApi, bot: TelegramBotInstance, message: dict,
                            sender: dict, state: TelegramBotUserState, text: str):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    language = state.language if state.language in COPY else bot.default_language
    session_row = TelegramServiceSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, action='support',
    ).first()
    if not session_row or not session_row.service_ownership_id or not text or text.startswith('/'):
        api.send_message(chat_id, COPY[language]['support_prompt'])
        return
    _identity_row, ownership = _owned_service(user_id, session_row.service_ownership_id)
    if not ownership:
        state.step = 'verified'
        session_row.action = None
        session_row.service_ownership_id = None
        db.session.flush()
        api.send_message(chat_id, COPY[language]['invalid_service'])
        return
    request_row, _duplicate = _create_service_request(
        bot.id, user_id, ownership, 'support', package=None, note=text,
    )
    state.step = 'verified'
    session_row.action = None
    session_row.service_ownership_id = None
    db.session.flush()
    api.send_message(chat_id, COPY[language]['support_pending'])
    _notify_service_request_admins(api, request_row)


def _handle_purchase_account_name(api: TelegramBotApi, bot: TelegramBotInstance,
                                  message: dict, sender: dict,
                                  state: TelegramBotUserState, text: str):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    language = state.language if state.language in COPY else bot.default_language
    value = str(text or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{2,31}', value):
        api.send_message(chat_id, COPY[language]['purchase_account_name_invalid'])
        return
    session_row = TelegramPurchaseSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, action='awaiting_account_name',
    ).first()
    server = db.session.get(Server, session_row.server_id) if session_row else None
    package = db.session.get(Package, session_row.package_id) if session_row else None
    if not session_row or not server or not package:
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, COPY[language]['start_first'])
        return
    if _purchase_account_name_exists(server.id, value):
        api.send_message(chat_id, COPY[language]['purchase_account_name_taken'])
        return
    draft = _purchase_name_draft(bot.id, user_id)
    if draft is None:
        draft = TelegramPurchaseNameDraft(
            bot_instance_id=bot.id, telegram_user_id=user_id, requested_name=value,
        )
        db.session.add(draft)
    draft.requested_name = value
    db.session.flush()
    _begin_purchase_payment(api, bot, chat_id, user_id, language, server, package, state)


def _handle_purchase_receipt(api: TelegramBotApi, bot: TelegramBotInstance,
                             message: dict, sender: dict,
                             state: TelegramBotUserState):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    language = state.language if state.language in COPY else bot.default_language
    receipt = _receipt_from_message(message)
    if receipt is None:
        api.send_message(chat_id, COPY[language]['receipt_invalid'])
        return
    session_row = TelegramPurchaseSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, action='awaiting_receipt',
    ).first()
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not session_row or not identity or not identity.customer_id:
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, COPY[language]['start_first'])
        return
    server = db.session.get(Server, session_row.server_id)
    package = db.session.get(Package, session_row.package_id)
    card = db.session.get(BankCard, session_row.bank_card_id)
    if not server or not package or not card or not card.is_active:
        session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, COPY[language]['payment_unavailable'])
        return
    duplicate = TelegramPurchaseRequest.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, status='pending',
    ).first()
    if duplicate:
        session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, COPY[language]['purchase_duplicate'])
        return
    kind, file_id, unique_id = receipt
    request_row = TelegramPurchaseRequest(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        customer_id=identity.customer_id,
        server_id=server.id,
        package_id=package.id,
        bank_card_id=card.id,
        amount=int(package.price or 0),
        receipt_file_id=file_id,
        receipt_file_unique_id=unique_id or None,
        receipt_kind=kind,
        source_chat_id=chat_id,
        source_message_id=int(message.get('message_id') or 0),
        status='pending',
    )
    db.session.add(request_row)
    db.session.flush()
    draft = _purchase_name_draft(bot.id, user_id)
    customer = db.session.get(CustomerAccount, identity.customer_id)
    policy_values = _purchase_policy_values(bot)
    account_name = _render_purchase_account_name(
        bot, request_row, customer, draft.requested_name if draft else None,
    )
    db.session.add(TelegramPurchaseRequestDetail(
        request_id=request_row.id,
        account_name=account_name,
        allocation_strategy=policy_values['assignment_strategy'],
    ))
    if draft:
        db.session.delete(draft)
    session_row.action = None
    session_row.server_id = None
    session_row.package_id = None
    session_row.bank_card_id = None
    state.step = 'verified'
    db.session.flush()
    api.send_message(chat_id, COPY[language]['purchase_pending'])
    _notify_purchase_admins(api, request_row)


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
        state.step = 'verified'
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        _send_owned_services(api, chat_id, state.language, identity)
    elif text in {COPY["fa"]["menu_buy_service"], COPY["en"]["menu_buy_service"]}:
        state.step = 'verified'
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        if identity and identity.customer_id and identity.phone_verified_at:
            if _purchase_policy_values(bot)['customer_selects_server']:
                _send_purchase_servers(api, bot, chat_id, state.language)
            else:
                _send_purchase_packages(api, bot, chat_id, state.language, None)
        else:
            _send_contact_prompt(api, chat_id, state.language)
    elif text in {COPY["fa"]["menu_add_service"], COPY["en"]["menu_add_service"]}:
        state.step = 'verified'
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
    elif state.step == "awaiting_support_message":
        _handle_support_message(api, bot, message, sender, state, text)
    elif state.step == "awaiting_purchase_account_name":
        _handle_purchase_account_name(api, bot, message, sender, state, text)
    elif state.step == "awaiting_purchase_receipt":
        _handle_purchase_receipt(api, bot, message, sender, state)
    elif state.step == "share_contact":
        _send_contact_prompt(api, chat_id, state.language)
    else:
        api.send_message(chat_id, COPY[state.language]["start_first"])


def process_update(api: TelegramBotApi, bot: TelegramBotInstance, update: dict):
    # The Telegram worker is a separate process and background snapshot readers
    # are intentionally disabled here. Pull only when Redis' snapshot version
    # changed so service details never depend on stale process-local memory.
    load_snapshot_from_redis()
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
