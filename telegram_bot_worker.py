"""Dedicated durable long-polling worker for Eve Telegram bots."""

from __future__ import annotations

import html
import json
import os
import random
import re
import signal
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

from flask import session as flask_session

os.environ.setdefault("DISABLE_BACKGROUND_THREADS", "true")
os.environ.setdefault("EVE_PROCESS_ROLE", "telegram-bot")

from app import (  # noqa: E402
    Admin,
    BankCard,
    CustomerAccount,
    CustomerTransaction,
    GLOBAL_SERVER_DATA,
    OwnershipClaim,
    OwnershipClaimItem,
    Package,
    Server,
    ServiceOwnership,
    SubAppConfig,
    TelegramBotInstance,
    TelegramBotRuntime,
    TelegramBotTestUser,
    TelegramBotUserState,
    TelegramAnnouncement,
    TelegramIdentity,
    TelegramOwnershipSession,
    TelegramPurchaseRequest,
    TelegramPurchaseRequestAllocation,
    TelegramPurchaseRequestDetail,
    TelegramPurchaseInboundRoute,
    TelegramPurchaseNameDraft,
    TelegramPurchasePolicy,
    TelegramPurchaseServerRule,
    TelegramPurchaseSession,
    TelegramPromo,
    TelegramPromoUse,
    TelegramReferral,
    TelegramServiceRequest,
    TelegramServiceRequestMessage,
    TelegramServiceSession,
    TelegramTrialGrant,
    TelegramWalletTopup,
    _log_audit,
    _reseller_can_create_free,
    _telegram_bot_api_client,
    _queue_telegram_announcement,
    _decrypt_telegram_secret,
    _public_base_url,
    _detect_telegram_inbound_profiles,
    _telegram_customer_inbounds,
    add_client,
    app,
    calculate_reseller_price,
    db,
    discover_phone_ownership_claim,
    format_app_datetime,
    get_xui_session,
    load_snapshot_from_redis,
    normalize_iran_mobile,
    parse_allowed_servers,
    renew_client,
    review_ownership_claim_item,
    rotate_client,
    server_is_v3,
    verify_ownership_claim_subscription,
)
from telegram_bot_runtime import (  # noqa: E402
    COPY,
    TelegramApiError,
    TelegramBotApi,
    contact_keyboard,
    language_keyboard,
    main_menu_keyboard,
    menu_label_map,
    parse_copy_overrides,
    resolve_copy,
)
from telegram_diagnostics import redact_connection_error  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402


running = True
worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
webhook_prepared_bot_ids: set[int] = set()
sla_scan_at_by_bot: dict[int, float] = {}
bot_threads: dict[int, tuple[threading.Thread, threading.Event]] = {}

# Per-bot effective copy (COPY + per-bot overrides). Bots run in separate
# threads of this process, so the resolved copy is kept in thread-local state
# and set once per bot in process_update; helpers below fall back to the
# default COPY when no bot context is active.
_copy_context = threading.local()


def _active_copy() -> dict:
    return getattr(_copy_context, "copy", None) or COPY


def _active_overrides() -> dict:
    return getattr(_copy_context, "overrides", None) or {}


def _cc(language: str) -> dict:
    """Effective copy strings for a language, honoring the active bot's overrides."""
    copy = _active_copy()
    return copy.get(language) or COPY.get(language) or COPY["fa"]


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


_RATE_BUCKETS: dict = {}
_RATE_BUCKETS_LOCK = threading.Lock()


def _rate_ok(user_id: int, action: str, limit: int, window_sec: int) -> bool:
    """In-process sliding-window rate limit per telegram user and action."""
    now = time.monotonic()
    key = (int(user_id), action)
    with _RATE_BUCKETS_LOCK:
        hits = [ts for ts in _RATE_BUCKETS.get(key, []) if now - ts < window_sec]
        if len(hits) >= limit:
            _RATE_BUCKETS[key] = hits
            return False
        hits.append(now)
        _RATE_BUCKETS[key] = hits
    return True


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
        chat_id, _cc(language)["share_phone"],
        reply_markup=contact_keyboard(language, copy=_active_copy()),
    )


def _brand_text(bot: TelegramBotInstance, language: str) -> str:
    """Prefix shown before customer-facing copy on reseller-owned bots."""
    if not bot or str(getattr(bot, 'owner_type', 'system') or 'system') == 'system':
        return ''
    name = str(getattr(bot, 'display_name', '') or '').strip()
    return f"{name}\n\n" if name else ''


def _effective_owner_id(bot: TelegramBotInstance, telegram_user_id: int | None):
    """Reseller scope for a purchase on this bot.

    The bot's own owner wins; on the central bot, fall back to the telegram
    user's latest active ownership that is bound to a reseller. None keeps the
    legacy global behavior.
    """
    if bot and bot.owner_admin_id:
        return int(bot.owner_admin_id)
    if not telegram_user_id:
        return None
    identity = TelegramIdentity.query.filter_by(telegram_user_id=int(telegram_user_id)).first()
    if not identity or not identity.customer_id:
        return None
    ownership = ServiceOwnership.query.filter(
        ServiceOwnership.customer_id == identity.customer_id,
        ServiceOwnership.reseller_id.isnot(None),
        ServiceOwnership.revoked_at.is_(None),
    ).order_by(ServiceOwnership.id.desc()).first()
    return int(ownership.reseller_id) if ownership else None


_CHANNEL_MEMBER_CACHE: dict = {}


def _channel_member(api: TelegramBotApi, chat_id: int, user_id: int) -> bool:
    """Live getChatMember check with a short in-memory cache; errors = not a member."""
    key = (int(chat_id), int(user_id))
    now = time.monotonic()
    cached = _CHANNEL_MEMBER_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    ok = False
    try:
        result, _route = api.call('getChatMember', {
            'chat_id': int(chat_id), 'user_id': int(user_id),
        })
        ok = str((result or {}).get('status') or '') in ('member', 'administrator', 'creator')
    except Exception:
        ok = False
    _CHANNEL_MEMBER_CACHE[key] = (now + 300, ok)
    return ok


def _promo_discount(promo: TelegramPromo, amount: int) -> int:
    if promo.kind == 'percent':
        discount = int(round(int(amount) * float(promo.value or 0) / 100.0))
        if promo.max_discount_amount is not None:
            discount = min(discount, int(promo.max_discount_amount))
    else:
        discount = int(round(float(promo.value or 0)))
    return max(0, min(discount, int(amount)))


def _evaluate_promos(bot: TelegramBotInstance, user_id: int, package: Package,
                     base_amount, applies_to: str, code: str | None = None,
                     api: TelegramBotApi | None = None, owner_id=None):
    """Evaluate automatic and code promos for this purchase/renewal.

    Returns (final_amount, total_discount, applied_promos, error_key).
    """
    base = max(0, int(base_amount or 0))
    now = datetime.utcnow()
    code_norm = str(code or '').strip().upper()
    owner = db.session.get(Admin, owner_id) if owner_id else None
    reseller_pricing = bool(owner and str(getattr(owner, 'role', '') or '').lower() == 'reseller')
    candidates = []
    for promo in TelegramPromo.query.filter_by(enabled=True).all():
        if promo.code:
            if not code_norm or str(promo.code).upper() != code_norm:
                continue
        if promo.bot_instance_id and int(promo.bot_instance_id) != int(bot.id):
            continue
        if promo.package_id and int(promo.package_id) != int(package.id):
            continue
        if (promo.applies_to or 'both') not in ('both', applies_to):
            continue
        if promo.starts_at and promo.starts_at > now:
            continue
        if promo.ends_at and promo.ends_at < now:
            continue
        if promo.min_amount and base < int(promo.min_amount):
            continue
        if not promo.apply_on_reseller_pricing and reseller_pricing:
            continue
        candidates.append(promo)
    if code_norm and not any(promo.code for promo in candidates):
        return base, 0, [], 'invalid_code'

    eligible = []
    for promo in candidates:
        if promo.first_purchase_only and TelegramPurchaseRequest.query.filter_by(
                telegram_user_id=int(user_id), status='completed').count():
            continue
        if promo.min_purchases_30d:
            count = TelegramPurchaseRequest.query.filter(
                TelegramPurchaseRequest.telegram_user_id == int(user_id),
                TelegramPurchaseRequest.status == 'completed',
                TelegramPurchaseRequest.created_at >= now - timedelta(days=30),
            ).count()
            if count < int(promo.min_purchases_30d):
                continue
        if promo.min_purchases_90d:
            count = TelegramPurchaseRequest.query.filter(
                TelegramPurchaseRequest.telegram_user_id == int(user_id),
                TelegramPurchaseRequest.status == 'completed',
                TelegramPurchaseRequest.created_at >= now - timedelta(days=90),
            ).count()
            if count < int(promo.min_purchases_90d):
                continue
        if promo.min_referrals:
            count = TelegramReferral.query.filter(
                TelegramReferral.referrer_telegram_user_id == int(user_id),
                TelegramReferral.qualified_at.isnot(None),
            ).count()
            if count < int(promo.min_referrals):
                continue
        if promo.requires_channel_chat_id:
            # Channel promos are honored exactly once per user per promo, even
            # after a leave/re-join cycle.
            if TelegramPromoUse.query.filter_by(
                    promo_id=promo.id, telegram_user_id=int(user_id)).first():
                continue
            if api is None or not _channel_member(
                    api, int(promo.requires_channel_chat_id), int(user_id)):
                continue
        if promo.max_uses_total is not None and TelegramPromoUse.query.filter_by(
                promo_id=promo.id).count() >= int(promo.max_uses_total):
            continue
        if promo.max_uses_per_user is not None and TelegramPromoUse.query.filter_by(
                promo_id=promo.id, telegram_user_id=int(user_id),
        ).count() >= int(promo.max_uses_per_user):
            continue
        eligible.append(promo)

    non_stackable = [promo for promo in eligible if not promo.stackable]
    stackable = sorted(
        (promo for promo in eligible if promo.stackable),
        key=lambda promo: -int(promo.priority or 0),
    )
    ordered = []
    if non_stackable:
        ordered.append(max(
            non_stackable,
            key=lambda promo: (_promo_discount(promo, base), int(promo.priority or 0)),
        ))
    ordered.extend(stackable)

    amount = base
    total_discount = 0
    applied = []
    for promo in ordered:
        discount = _promo_discount(promo, amount)
        if discount <= 0:
            continue
        amount -= discount
        total_discount += discount
        applied.append((promo, discount))
    return max(0, amount), total_discount, applied, None


def _scoped_owner_id(bot: TelegramBotInstance, owner_id=None):
    if owner_id is not None:
        return int(owner_id)
    return int(bot.owner_admin_id) if bot and bot.owner_admin_id else None


def _send_main_menu(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int, language: str):
    lang = language if language in COPY else "fa"
    show_trial = _purchase_policy_values(bot)['trial_enabled']
    api.send_message(
        chat_id, _brand_text(bot, lang) + _cc(lang)["welcome_menu"],
        reply_markup=main_menu_keyboard(
            lang, show_trial=show_trial,
            copy=_active_copy(), overrides=_active_overrides(),
        ),
    )


def _send_owned_services(api: TelegramBotApi, chat_id: int, language: str,
                         identity: TelegramIdentity | None):
    lang = language if language in COPY else "fa"
    if identity is None or not identity.customer_id:
        api.send_message(chat_id, _cc(lang)["no_owned_services"])
        return
    ownerships = ServiceOwnership.query.filter_by(
        customer_id=identity.customer_id, revoked_at=None,
    ).order_by(ServiceOwnership.id.asc()).all()
    if not ownerships:
        api.send_message(chat_id, _cc(lang)["no_owned_services"])
        return
    keyboard = []
    current_server_id = None
    for index, ownership in enumerate(ownerships[:50], 1):
        if ownership.server_id != current_server_id:
            current_server_id = ownership.server_id
            server_name = getattr(ownership.server, 'name', '') or f'#{ownership.server_id}'
            keyboard.append([{
                "text": f'{_cc(lang)["server_button"]}: {server_name}'[:60],
                "callback_data": "noop",
            }])
        label = ownership.client_email_snapshot or f'{_cc(lang)["service_button"]} {index}'
        keyboard.append([{
            "text": f'{_cc(lang)["account_button"]}: {label}'[:60],
            "callback_data": f"service:{ownership.id}",
        }])
    api.send_message(
        chat_id, _cc(lang)["owned_services"],
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


def _cached_owned_service_location(ownership: ServiceOwnership):
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
                try:
                    inbound_id = int(inbound.get('id'))
                except (TypeError, ValueError):
                    inbound_id = None
                return client, inbound_id
    return None, None


def _cached_owned_service(ownership: ServiceOwnership):
    client, _inbound_id = _cached_owned_service_location(ownership)
    return client


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


def _localized_app_date(value: datetime, language: str) -> str:
    formatted = format_app_datetime(value) or ''
    date_text = formatted.split(' ', 1)[0]
    if language == 'fa':
        return date_text.translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))
    return date_text


def _service_expiry(client: dict | None, language: str) -> str:
    if not client:
        return _cc(language)["service_unavailable"]
    raw = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
    try:
        expiry_ts = int(client.get('expiryTimestamp') or raw.get('expiryTime') or 0)
    except (TypeError, ValueError):
        expiry_ts = 0
    if expiry_ts == 0:
        return _cc(language)["unlimited"]
    if expiry_ts < 0:
        days = max(1, int(round(abs(expiry_ts) / 86400000)))
        return f"{days} " + ("روز پس از اولین اتصال" if language == 'fa' else "days after first connection")
    expiry = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
    remaining_days = int((expiry - datetime.now(timezone.utc)).total_seconds() // 86400)
    date_text = _localized_app_date(expiry, language)
    if remaining_days < 0:
        return f"{date_text} ({_cc(language)['status_expired']})"
    days_text = str(remaining_days)
    if language == 'fa':
        days_text = days_text.translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))
    suffix = f"{days_text} روز" if language == 'fa' else f"{days_text} days"
    return f"{date_text} ({suffix})"


_TUTORIAL_OS_TYPES = ('android', 'ios', 'windows')


def _tutorial_device_keyboard(language: str):
    lang = language if language in COPY else 'fa'
    return {'inline_keyboard': [[{
        'text': _cc(lang)[f'tutorial_device_{os_type}'],
        'callback_data': f'tutorial-os:{os_type}',
    }] for os_type in _TUTORIAL_OS_TYPES]}


def _send_tutorial_devices(api: TelegramBotApi, chat_id: int, language: str):
    lang = language if language in COPY else 'fa'
    api.send_message(
        chat_id, _cc(lang)['tutorial_choose_device'],
        reply_markup=_tutorial_device_keyboard(lang),
    )


def _tutorial_apps(os_type: str):
    if os_type not in _TUTORIAL_OS_TYPES:
        return []
    return SubAppConfig.query.filter_by(
        os_type=os_type, is_enabled=True,
    ).order_by(SubAppConfig.display_order.asc(), SubAppConfig.id.asc()).all()


def _send_tutorial_apps(api: TelegramBotApi, chat_id: int, language: str, os_type: str):
    lang = language if language in COPY else 'fa'
    apps = _tutorial_apps(os_type)
    if not apps:
        api.send_message(
            chat_id, _cc(lang)['tutorial_no_apps'],
            reply_markup=_tutorial_device_keyboard(lang),
        )
        return
    keyboard = []
    recommended = next((item for item in apps if item.is_recommended), apps[0])
    for item in apps:
        marker = '⭐ ' if item.id == recommended.id else ''
        keyboard.append([{
            'text': f'{marker}{item.name or item.app_code}'[:60],
            'callback_data': f'tutorial-app:{item.id}',
        }])
    keyboard.append([{
        'text': _cc(lang)['tutorial_back_devices'],
        'callback_data': 'tutorial-devices',
    }])
    api.send_message(
        chat_id, _cc(lang)['tutorial_choose_app'],
        reply_markup={'inline_keyboard': keyboard},
    )


def _tutorial_url(raw_url: str | None) -> str | None:
    value = str(raw_url or '').strip()
    if not value:
        return None
    if value.startswith(('https://', 'http://')):
        return value
    if value.startswith('/'):
        base_url = _public_base_url().rstrip('/')
        return f'{base_url}{value}' if base_url else None
    return None


def _send_tutorial_app(api: TelegramBotApi, chat_id: int, language: str, app_id: int):
    lang = language if language in COPY else 'fa'
    item = db.session.get(SubAppConfig, app_id)
    if not item or not item.is_enabled or item.os_type not in _TUTORIAL_OS_TYPES:
        api.send_message(
            chat_id, _cc(lang)['tutorial_app_unavailable'],
            reply_markup=_tutorial_device_keyboard(lang),
        )
        return
    title = (item.title_fa if lang == 'fa' else item.title_en) or item.name or item.app_code
    description = (item.description_fa if lang == 'fa' else item.description_en) or ''
    lines = [f'<b>{html.escape(str(title))}</b>']
    if item.is_recommended:
        lines.append(_cc(lang)['tutorial_recommended'])
    if description.strip():
        lines.extend(['', html.escape(description.strip())])
    keyboard = []
    download_url = _tutorial_url(item.download_link)
    store_url = _tutorial_url(item.store_link)
    video_url = _tutorial_url(item.tutorial_link)
    if download_url:
        keyboard.append([{'text': _cc(lang)['tutorial_download'], 'url': download_url}])
    if store_url:
        store_key = 'tutorial_google_play' if item.os_type == 'android' else (
            'tutorial_app_store' if item.os_type == 'ios' else 'tutorial_store'
        )
        keyboard.append([{'text': _cc(lang)[store_key], 'url': store_url}])
    if video_url:
        keyboard.append([{'text': _cc(lang)['tutorial_video'], 'url': video_url}])
    keyboard.append([{
        'text': _cc(lang)['tutorial_back_devices'],
        'callback_data': f'tutorial-os:{item.os_type}',
    }])
    api.send_message(
        chat_id, '\n'.join(lines), parse_mode='HTML',
        reply_markup={'inline_keyboard': keyboard},
    )


def _service_status(client: dict | None, language: str) -> str:
    if not client:
        return f"⚪ {_cc(language)['status_unknown']}"
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
    return f"{emoji} {_cc(language)[copy_key]}"


def _service_keyboard(ownership: ServiceOwnership, language: str):
    return {"inline_keyboard": [
        [{"text": _cc(language)["get_link_button"], "callback_data": f"service-link:{ownership.id}"}],
        [
            {"text": _cc(language)["renew_button"], "callback_data": f"service-renew:{ownership.id}"},
            {"text": _cc(language)["support_button"], "callback_data": f"service-support:{ownership.id}"},
        ],
        [{"text": _cc(language)["back_services_button"], "callback_data": "service-list"}],
    ]}


def _send_service_details(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int,
                          language: str, ownership: ServiceOwnership):
    lang = language if language in COPY else 'fa'
    client = _cached_owned_service(ownership)
    server_name = getattr(ownership.server, 'name', '') or f'#{ownership.server_id}'
    account = ownership.client_email_snapshot or f'#{ownership.id}'
    if client:
        used = max(0, int(client.get('up') or 0)) + max(0, int(client.get('down') or 0))
        remaining = client.get('remaining_bytes')
        remaining_text = _cc(lang)['unlimited'] if remaining in (None, -1) else _format_traffic(remaining)
        freshness = _cc(lang)['service_live']
    else:
        used = None
        remaining_text = _cc(lang)['service_unavailable']
        freshness = _cc(lang)['service_unavailable']
    text = "\n".join([
        f"<b>{_cc(lang)['service_details']}</b>",
        f"{_cc(lang)['service_server']}: <b>{html.escape(str(server_name))}</b>",
        f"{_cc(lang)['service_account']}: <code>{html.escape(str(account))}</code>",
        f"{_cc(lang)['service_status']}: {_service_status(client, lang)}",
        f"{_cc(lang)['service_expiry']}: {_service_expiry(client, lang)}",
        f"{_cc(lang)['service_usage']}: {_format_traffic(used) if used is not None else _cc(lang)['service_unavailable']}",
        f"{_cc(lang)['service_remaining']}: {remaining_text}",
        f"{_cc(lang)['service_updated']}: {freshness}",
    ])
    keyboard = _service_keyboard(ownership, lang)
    if client:
        keyboard['inline_keyboard'].insert(1, [{
            'text': _cc(lang)['service_rotate'],
            'callback_data': f'service-rotate:{ownership.id}',
        }])
        status_key = str(client.get('service_state') or '').strip().lower()
        if not status_key:
            status_key = 'active' if client.get('enable', True) else 'inactive'
        if (status_key in ('expired', 'volume_ended')
                and _purchase_policy_values(bot)['emergency_enabled']):
            keyboard['inline_keyboard'].insert(0, [{
                'text': _cc(lang)['emergency_button'],
                'callback_data': f'service-emergency:{ownership.id}',
            }])
    api.send_message(
        chat_id, text, parse_mode='HTML',
        reply_markup=keyboard,
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
        if getattr(package, 'is_trial', False):
            continue
        scope = str(package.scope or 'global').lower()
        if scope == 'global':
            visible.append(package)
            continue
        try:
            assigned = {int(value) for value in json.loads(package.assigned_reseller_ids or '[]')}
        except (TypeError, ValueError):
            assigned = set()
        if ownership.reseller_id and (
            int(ownership.reseller_id) in assigned or
            (scope == 'personal' and int(package.created_by or 0) == int(ownership.reseller_id))
        ):
            visible.append(package)
    return visible[:20]


def _resolve_purchase_price(owner_admin_id, package) -> int:
    owner = db.session.get(Admin, owner_admin_id) if owner_admin_id else None
    if owner and str(getattr(owner, 'role', '') or '').lower() == 'reseller':
        return int(calculate_reseller_price(owner, package=package) or 0)
    return int(package.price or 0)


def _send_renew_packages(api: TelegramBotApi, bot: TelegramBotInstance,
                         chat_id: int, user_id: int, language: str,
                         ownership: ServiceOwnership):
    packages = _available_packages(ownership)
    if not packages:
        request_row, duplicate = _create_service_request(
            bot.id, user_id, ownership, 'renewal', package=None, note=None,
        )
        _send_renewal_request_state(api, chat_id, language, request_row, duplicate=duplicate)
        if not duplicate:
            _notify_service_request_admins(api, request_row)
        return
    keyboard = []
    for package in packages:
        base_price = _resolve_purchase_price(ownership.reseller_id, package)
        final_price, discount, _applied, _err = _evaluate_promos(
            bot, user_id, package, base_price, 'renewal',
            api=api, owner_id=ownership.reseller_id,
        )
        price = (f"~{base_price:,}~ {final_price:,} T" if discount > 0
                 else f"{final_price:,} T")
        keyboard.append([{
            "text": f"{package.name} • {price}"[:60],
            "callback_data": f"renew-package:{ownership.id}:{package.id}",
        }])
    keyboard.append([{"text": _cc(language)['back_services_button'],
                      "callback_data": f"service:{ownership.id}"}])
    api.send_message(
        chat_id, _cc(language)['choose_package'],
        reply_markup={"inline_keyboard": keyboard},
    )


def _purchase_servers(bot: TelegramBotInstance, owner_id=None):
    query = Server.query.filter_by(enabled=True, hidden=False)
    servers = query.order_by(Server.name.asc(), Server.id.asc()).all()
    owner_id = _scoped_owner_id(bot, owner_id)
    if not owner_id:
        return servers[:30]
    owner = db.session.get(Admin, owner_id)
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


def _trial_channels(policy) -> list:
    """Effective trial gate channels: the JSON list, falling back to the legacy single chat ID."""
    if not policy:
        return []
    channels = policy.trial_channels()
    if not channels and policy.trial_channel_chat_id:
        channels = [{'chat_id': int(policy.trial_channel_chat_id), 'title': '', 'invite_url': ''}]
    return channels


def _purchase_policy_values(bot: TelegramBotInstance):
    policy = db.session.get(TelegramPurchasePolicy, bot.id)
    return {
        'customer_selects_server': bool(policy.customer_selects_server) if policy else False,
        'assignment_strategy': (policy.assignment_strategy if policy else None) or 'least_clients',
        'account_name_mode': (policy.account_name_mode if policy else None) or 'generated',
        'account_name_template': (
            policy.account_name_template if policy else None
        ) or 'tg{order_id}-{phone_last4}',
        'trial_enabled': bool(policy.trial_enabled) if policy else False,
        'trial_package_id': policy.trial_package_id if policy else None,
        'trial_requires_channel_membership': (
            bool(policy.trial_requires_channel_membership) if policy else False
        ),
        'trial_channel_chat_id': policy.trial_channel_chat_id if policy else None,
        'trial_channels': _trial_channels(policy),
        'emergency_enabled': bool(policy.emergency_enabled) if policy else False,
        'emergency_days': max(1, int(policy.emergency_days or 1)) if policy else 1,
        'emergency_volume_gb': max(0, int(policy.emergency_volume_gb or 1)) if policy else 1,
        'emergency_cooldown_days': max(1, int(policy.emergency_cooldown_days or 30)) if policy else 30,
    }


def _purchase_server_rules(bot: TelegramBotInstance):
    return {
        row.server_id: row for row in TelegramPurchaseServerRule.query.filter_by(
            bot_instance_id=bot.id,
        ).all()
    }


def _purchase_inbound_routes(bot: TelegramBotInstance, package_id: int | None = None):
    query = TelegramPurchaseInboundRoute.query.filter_by(
        bot_instance_id=bot.id, enabled=True,
    )
    if package_id is not None:
        query = query.filter_by(package_id=int(package_id))
    return query.all()


def _eligible_purchase_servers(bot: TelegramBotInstance, package_id: int | None = None, owner_id=None):
    servers = _purchase_servers(bot, owner_id=owner_id)
    rules = _purchase_server_rules(bot)
    if rules:
        servers = [server for server in servers if rules.get(server.id) and rules[server.id].eligible]
    routes = _purchase_inbound_routes(bot, package_id)
    if not routes:
        return servers
    route_server_ids = {row.server_id for row in routes}
    return [server for server in servers if server.id in route_server_ids]


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


def _assign_purchase_server(bot: TelegramBotInstance, package_id: int | None = None, owner_id=None):
    servers = _eligible_purchase_servers(bot, package_id, owner_id=owner_id)
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


def _purchase_packages(bot: TelegramBotInstance, owner_id=None):
    owner_id = _scoped_owner_id(bot, owner_id)
    packages = Package.query.filter_by(enabled=True).order_by(
        Package.display_order.asc(), Package.id.asc(),
    ).all()
    visible = []
    for package in packages:
        if getattr(package, 'is_trial', False):
            continue
        scope = str(package.scope or 'global').lower()
        if scope == 'global':
            visible.append(package)
            continue
        try:
            assigned = {int(value) for value in json.loads(package.assigned_reseller_ids or '[]')}
        except (TypeError, ValueError):
            assigned = set()
        if owner_id and (
            int(owner_id) in assigned or
            (scope == 'personal' and int(package.created_by or 0) == int(owner_id))
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
                           chat_id: int, language: str, user_id: int | None = None):
    owner_id = _effective_owner_id(bot, user_id)
    rules = _purchase_server_rules(bot)
    servers = [
        server for server in _eligible_purchase_servers(bot, owner_id=owner_id)
        if rules.get(server.id) and rules[server.id].customer_visible
    ]
    if not servers:
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    keyboard = [[{
        'text': f"{_cc(language)['server_button']}: {(rules[server.id].display_name or server.name)}"[:60],
        'callback_data': f'buy-server:{server.id}',
    }] for server in servers]
    api.send_message(
        chat_id, _cc(language)['choose_purchase_server'],
        reply_markup={'inline_keyboard': keyboard},
    )


def _send_purchase_packages(api: TelegramBotApi, bot: TelegramBotInstance,
                            chat_id: int, language: str, server: Server | None,
                            user_id: int | None = None):
    owner_id = _effective_owner_id(bot, user_id)
    packages = _purchase_packages(bot, owner_id=owner_id)
    configured_routes = _purchase_inbound_routes(bot)
    if configured_routes:
        configured_pairs = {(row.package_id, row.server_id) for row in configured_routes}
        if server:
            packages = [row for row in packages if (row.id, server.id) in configured_pairs]
        else:
            eligible_ids = {row.id for row in _eligible_purchase_servers(bot, owner_id=owner_id)}
            packages = [row for row in packages if any(
                package_id == row.id and server_id in eligible_ids
                for package_id, server_id in configured_pairs
            )]
    if not packages:
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    keyboard = [[{
        'text': f"{package.name} • {_resolve_purchase_price(owner_id, package):,} T"[:60],
        'callback_data': f'buy-package:{server.id if server else 0}:{package.id}',
    }] for package in packages]
    keyboard.append([{
        'text': _cc(language)['promo_code_button'],
        'callback_data': 'promo-code',
    }])
    api.send_message(
        chat_id, _cc(language)['choose_purchase_package'],
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


def _send_link_with_qr(api: TelegramBotApi, chat_id: int, link: str, caption: str = ''):
    """Deliver a connection link as a QR photo (caption carries the copyable link).

    Any QR/upload failure falls back to a plain text message with the link.
    """
    link = str(link or '').strip()
    if not link:
        return
    text = str(caption or link).strip() or link
    try:
        import io as _io

        import qrcode as _qrcode

        qr = _qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')
        buffer = _io.BytesIO()
        img.save(buffer, format='PNG')
        api.send_upload(
            chat_id, buffer.getvalue(), 'connection-link.png', 'image/png',
            as_photo=True, caption=text[:1024],
        )
    except Exception:
        api.send_message(chat_id, text)


def _purchase_card_accessible(card: BankCard, bot: TelegramBotInstance, owner_id=None) -> bool:
    owner_id = _scoped_owner_id(bot, owner_id)
    if owner_id is None:
        return True
    if not card.reseller_id or int(card.reseller_id) == owner_id:
        return True
    try:
        assigned = {int(value) for value in json.loads(card.assigned_reseller_ids or '[]')}
    except (TypeError, ValueError):
        assigned = set()
    return owner_id in assigned


def _purchase_card(bot: TelegramBotInstance, owner_id=None):
    cards = BankCard.query.filter_by(is_active=True).order_by(BankCard.id.asc()).all()
    owner_id = _scoped_owner_id(bot, owner_id)
    if owner_id is None:
        central = [card for card in cards if not card.reseller_id]
        return central[0] if central else (cards[0] if cards else None)
    tiers = ([], [], [])  # own cards, assigned-to-owner cards, central cards
    for card in cards:
        if card.reseller_id and int(card.reseller_id) == owner_id:
            tiers[0].append(card)
        elif not card.reseller_id:
            tiers[2].append(card)
        elif _purchase_card_accessible(card, bot, owner_id=owner_id):
            tiers[1].append(card)
    for tier in tiers:
        if tier:
            return tier[0]
    return None


def _wallet_customer(user_id: int):
    """Resolve the verified end customer behind a Telegram user, like the purchase flow."""
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not identity or not identity.customer_id:
        return None, None
    return identity, db.session.get(CustomerAccount, identity.customer_id)


def _receipt_is_duplicate(unique_id: str) -> bool:
    if not unique_id:
        return False
    if TelegramPurchaseRequest.query.filter(
        TelegramPurchaseRequest.receipt_file_unique_id == unique_id,
        TelegramPurchaseRequest.status != 'rejected',
    ).first() is not None:
        return True
    if TelegramServiceRequest.query.filter(
        TelegramServiceRequest.receipt_file_unique_id == unique_id,
        TelegramServiceRequest.status != 'rejected',
    ).first() is not None:
        return True
    return TelegramWalletTopup.query.filter(
        TelegramWalletTopup.receipt_file_unique_id == unique_id,
        TelegramWalletTopup.status != 'rejected',
    ).first() is not None


def _send_wallet_menu(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int,
                      user_id: int, language: str):
    identity, customer = _wallet_customer(user_id)
    if not identity or not customer or not identity.phone_verified_at:
        _send_contact_prompt(api, chat_id, language)
        return
    api.send_message(
        chat_id,
        _cc(language)['wallet_balance'].format(balance=f"{int(customer.credit or 0):,}"),
        parse_mode='HTML',
        reply_markup={'inline_keyboard': [[
            {'text': _cc(language)['wallet_topup_button'], 'callback_data': 'wallet-topup-start'},
            {'text': _cc(language)['wallet_history_button'], 'callback_data': 'wallet-history'},
        ]]},
    )


def _send_wallet_history(api: TelegramBotApi, chat_id: int, user_id: int, language: str):
    _identity_row, customer = _wallet_customer(user_id)
    rows = []
    if customer:
        rows = CustomerTransaction.query.filter_by(customer_id=customer.id).order_by(
            CustomerTransaction.created_at.desc(), CustomerTransaction.id.desc(),
        ).limit(10).all()
    if not rows:
        api.send_message(chat_id, _cc(language)['wallet_history_empty'])
        return
    lines = [_cc(language)['wallet_history_title']]
    for row in rows:
        type_label = _cc(language).get(f'wallet_type_{row.type}', str(row.type or ''))
        card_label = ''
        if row.bank_card:
            card_label = f" • {row.bank_card.label or row.bank_card.card_number or ''}".rstrip()
        lines.append(_cc(language)['wallet_history_line'].format(
            date=row.created_at.strftime('%Y-%m-%d') if row.created_at else '-',
            type=type_label,
            amount=f"{int(row.amount or 0):+,}",
            card=card_label,
        ))
    api.send_message(chat_id, '\n'.join(lines))


def _handle_wallet_topup_amount(api: TelegramBotApi, bot: TelegramBotInstance,
                                message: dict, sender: dict,
                                state: TelegramBotUserState, text: str):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    language = state.language if state.language in COPY else bot.default_language
    raw = str(text or '').strip().replace(',', '').replace('٬', '')
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0 or amount > 100_000_000_000:
        api.send_message(chat_id, _cc(language)['wallet_topup_invalid_amount'])
        return
    identity, customer = _wallet_customer(user_id)
    if not identity or not customer or not identity.phone_verified_at:
        state.step = 'verified'
        db.session.flush()
        _send_contact_prompt(api, chat_id, language)
        return
    owner_id = _effective_owner_id(bot, user_id)
    card = _purchase_card(bot, owner_id=owner_id)
    if card is None:
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    session_row = _service_session(bot.id, user_id)
    session_row.service_ownership_id = None
    session_row.action = f'topup_receipt:{amount}:{card.id}'
    state.step = 'awaiting_topup_receipt'
    db.session.flush()
    api.send_message(
        chat_id,
        _cc(language)['wallet_topup_payment'].format(
            amount=f"{amount:,}", card=_format_bank_card(card)),
        parse_mode='HTML',
    )


def _notify_topup_admins(api: TelegramBotApi, topup: TelegramWalletTopup):
    lines = [
        f"Telegram wallet top-up #{topup.id}",
        f"Amount: {int(topup.amount or 0):,} T",
        f"Telegram user: {topup.telegram_user_id}",
    ]
    if topup.bank_card:
        lines.append(f"Card: {topup.bank_card.label or topup.bank_card.card_number or ''}")
    if topup.duplicate_receipt:
        lines.append('⚠️ FRAUD WARNING: this receipt file was already used on another request!')
    keyboard = {'inline_keyboard': [
        [
            {'text': '✅ Approve top-up', 'callback_data': f'admin-topup:{topup.id}:approve'},
            {'text': '❌ Reject', 'callback_data': f'admin-topup:{topup.id}:reject'},
        ],
        [{'text': '✏️ Edit card', 'callback_data': f'admin-edit-card:topup:{topup.id}'}],
    ]}
    bot = db.session.get(TelegramBotInstance, topup.bot_instance_id)
    owner_id = _effective_owner_id(bot, topup.telegram_user_id) if bot else None
    for admin in Admin.query.filter_by(enabled=True).all():
        role = str(admin.role or '').lower()
        is_global = bool(admin.is_superadmin or role in ('admin', 'superadmin'))
        is_owner = bool(owner_id and admin.id == owner_id)
        if not (is_global or is_owner):
            continue
        try:
            admin_chat_id = int(str(admin.telegram_id or '').strip())
            if admin_chat_id <= 0:
                continue
        except (TypeError, ValueError):
            continue
        try:
            if topup.receipt_file_kind == 'document':
                api.send_document(admin_chat_id, topup.receipt_file_id)
            else:
                api.send_photo(admin_chat_id, topup.receipt_file_id)
        except TelegramApiError:
            pass
        try:
            api.send_message(admin_chat_id, '\n'.join(lines), reply_markup=keyboard)
        except TelegramApiError:
            continue


def _handle_wallet_topup_receipt(api: TelegramBotApi, bot: TelegramBotInstance,
                                 message: dict, sender: dict,
                                 state: TelegramBotUserState):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    if not _rate_ok(user_id, 'receipt', 10, 120):
        return
    language = state.language if state.language in COPY else bot.default_language
    receipt = _receipt_from_message(message)
    if receipt is None:
        api.send_message(chat_id, _cc(language)['receipt_invalid'])
        return
    session_row = TelegramServiceSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id,
    ).first()
    identity, customer = _wallet_customer(user_id)
    parts = str(getattr(session_row, 'action', '') or '').split(':')
    try:
        amount = int(parts[1])
        card_id = int(parts[2])
    except (IndexError, TypeError, ValueError):
        amount, card_id = 0, 0
    card = db.session.get(BankCard, card_id) if card_id else None
    if (not session_row or parts[0] != 'topup_receipt' or amount <= 0
            or not identity or not customer or not card or not card.is_active):
        if session_row:
            session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['start_first'])
        return
    kind, file_id, unique_id = receipt
    topup = TelegramWalletTopup(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        customer_id=customer.id,
        bank_card_id=card.id,
        amount=amount,
        receipt_file_id=file_id,
        receipt_file_kind=kind,
        receipt_file_unique_id=unique_id or None,
        duplicate_receipt=_receipt_is_duplicate(unique_id),
        status='pending',
    )
    db.session.add(topup)
    session_row.action = None
    state.step = 'verified'
    db.session.flush()
    api.send_message(chat_id, _cc(language)['wallet_topup_pending'])
    _notify_topup_admins(api, topup)


def _topup_reviewer(user_id: int, topup: TelegramWalletTopup):
    admin = _telegram_admin(user_id)
    if admin:
        return admin
    bot = db.session.get(TelegramBotInstance, topup.bot_instance_id)
    owner_id = _effective_owner_id(bot, topup.telegram_user_id) if bot else None
    owner = db.session.get(Admin, owner_id) if owner_id else None
    try:
        owner_telegram_id = int(str(getattr(owner, 'telegram_id', '') or '').strip())
    except (TypeError, ValueError):
        owner_telegram_id = 0
    if owner and owner.enabled and owner_telegram_id == int(user_id):
        return owner
    return None


def _handle_admin_topup_callback(api: TelegramBotApi, callback: dict, data: str) -> bool:
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'admin-topup' or parts[2] not in ('approve', 'reject'):
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    try:
        topup = db.session.get(TelegramWalletTopup, int(parts[1]))
    except (TypeError, ValueError):
        topup = None
    reviewer = _topup_reviewer(int(sender.get('id') or 0), topup) if topup else None
    if not topup or not reviewer:
        if callback_id:
            api.answer_callback(callback_id, 'Access denied')
        return True
    if topup.status != 'pending':
        api.answer_callback(callback_id, 'Already reviewed')
        return True
    topup.status = 'approved' if parts[2] == 'approve' else 'rejected'
    topup.reviewer_admin_id = reviewer.id
    topup.reviewed_at = datetime.utcnow()
    customer = db.session.get(CustomerAccount, topup.customer_id)
    if topup.status == 'approved' and customer:
        customer.credit = int(customer.credit or 0) + int(topup.amount or 0)
        db.session.add(CustomerTransaction(
            customer_id=customer.id,
            type='topup',
            amount=int(topup.amount or 0),
            bank_card_id=topup.bank_card_id,
            receipt_file_id=topup.receipt_file_id,
            receipt_file_kind=topup.receipt_file_kind,
            receipt_file_unique_id=topup.receipt_file_unique_id,
            request_ref=f'topup:{topup.id}',
        ))
    _log_audit(f"telegram_topup.{parts[2]}", topup, actor=reviewer)
    db.session.flush()
    api.answer_callback(callback_id, 'Saved')
    if chat_id:
        api.send_message(chat_id, f"Top-up #{topup.id}: {topup.status}")
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=topup.telegram_user_id,
        customer_id=topup.customer_id,
    ).first()
    if identity and identity.telegram_chat_id:
        language = str(getattr(customer, 'preferred_language', '') or 'fa')
        if language not in COPY:
            language = 'fa'
        if topup.status == 'approved':
            text = _cc(language)['wallet_topup_approved'].format(
                amount=f"{int(topup.amount or 0):,}",
                balance=f"{int(customer.credit or 0):,}",
            )
        else:
            text = _cc(language)['wallet_topup_rejected']
        api.send_message(identity.telegram_chat_id, text)
    return True


def _wallet_request_record(kind: str, record_id: int):
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return None
    if kind == 'purchase':
        return db.session.get(TelegramPurchaseRequest, record_id)
    if kind == 'renewal':
        return db.session.get(TelegramServiceRequest, record_id)
    if kind == 'topup':
        return db.session.get(TelegramWalletTopup, record_id)
    return None


def _handle_admin_edit_card_callback(api: TelegramBotApi, callback: dict, data: str) -> bool:
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'admin-edit-card' or parts[1] not in ('purchase', 'renewal', 'topup'):
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    reviewer = _telegram_admin(int(sender.get('id') or 0))
    record = _wallet_request_record(parts[1], parts[2])
    if not reviewer or not record:
        if callback_id:
            api.answer_callback(callback_id, 'Access denied')
        return True
    cards = BankCard.query.filter_by(is_active=True).order_by(BankCard.id.asc()).all()
    if not cards:
        api.answer_callback(callback_id, 'No active cards')
        return True
    keyboard = [[{
        'text': (card.label or card.card_number or f'#{card.id}')[:60],
        'callback_data': f'admin-set-card:{parts[1]}:{record.id}:{card.id}',
    }] for card in cards[:30]]
    api.answer_callback(callback_id)
    if chat_id:
        api.send_message(
            chat_id, f"Select the new card for {parts[1]} #{record.id}:",
            reply_markup={'inline_keyboard': keyboard},
        )
    return True


def _handle_admin_set_card_callback(api: TelegramBotApi, callback: dict, data: str) -> bool:
    parts = data.split(':')
    if len(parts) != 4 or parts[0] != 'admin-set-card' or parts[1] not in ('purchase', 'renewal', 'topup'):
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    reviewer = _telegram_admin(int(sender.get('id') or 0))
    record = _wallet_request_record(parts[1], parts[2])
    try:
        card = db.session.get(BankCard, int(parts[3]))
    except (TypeError, ValueError):
        card = None
    if not reviewer or not record or not card or not card.is_active:
        if callback_id:
            api.answer_callback(callback_id, 'Access denied')
        return True
    record.bank_card_id = card.id
    _log_audit(f'telegram_{parts[1]}.update_card', record, actor=reviewer,
               meta={'bank_card_id': card.id})
    db.session.flush()
    api.answer_callback(callback_id, 'Saved')
    if chat_id:
        api.send_message(
            chat_id,
            f"✅ Card on {parts[1]} #{record.id} updated:\n{_format_bank_card(card)}",
            parse_mode='HTML',
        )
    return True


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
        api.send_message(chat_id, _cc(language)['purchase_account_name_prompt'])
        return
    _begin_purchase_payment(api, bot, chat_id, user_id, language, server, package, state)


def _render_purchase_account_name(bot: TelegramBotInstance,
                                  request_row: TelegramPurchaseRequest,
                                  customer: CustomerAccount,
                                  requested_name: str | None):
    if requested_name:
        return requested_name
    template = _purchase_policy_values(bot)['account_name_template']
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=request_row.telegram_user_id,
    ).first()
    canonical_phone = normalize_iran_mobile(
        (identity.phone_normalized if identity else None) or customer.primary_phone,
    )
    if canonical_phone and canonical_phone.startswith('98') and len(canonical_phone) == 12:
        phone = f'0{canonical_phone[2:]}'
    else:
        phone = ''.join(filter(str.isdigit, str(customer.primary_phone or '')))
    telegram_username = re.sub(
        r'[^A-Za-z0-9_]+', '', str((identity.username if identity else '') or '').lstrip('@'),
    ) or f'user{request_row.telegram_user_id}'
    try:
        rendered = template.format(
            order_id=request_row.id,
            phone=(phone or f'user{request_row.telegram_user_id}'),
            phone_last4=(phone[-4:] if phone else '0000'),
            telegram_username=telegram_username,
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


def _send_purchase_card_payment(api: TelegramBotApi, bot: TelegramBotInstance,
                                chat_id: int, user_id: int, language: str,
                                state: TelegramBotUserState):
    """Card-to-card leg of a purchase: show the card and wait for the receipt."""
    session_row = _purchase_session(bot.id, user_id)
    server = db.session.get(Server, session_row.server_id) if session_row.server_id else None
    package = db.session.get(Package, session_row.package_id) if session_row.package_id else None
    owner_id = _effective_owner_id(bot, user_id)
    card = db.session.get(BankCard, session_row.bank_card_id) if session_row.bank_card_id else None
    if (card is None or not card.is_active
            or not _purchase_card_accessible(card, bot, owner_id=owner_id)):
        card = _purchase_card(bot, owner_id=owner_id)
    if not server or not package or card is None:
        session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    session_row.bank_card_id = card.id
    session_row.action = 'awaiting_receipt'
    state.step = 'awaiting_purchase_receipt'
    db.session.flush()
    discount = int(session_row.discount_amount or 0)
    base_price = int(session_row.quoted_amount or 0) + discount
    discount_block = ''
    if discount > 0:
        discount_block = _cc(language)['promo_discount_block'].format(
            original=f"{base_price:,}", discount=f"{discount:,}")
    api.send_message(
        chat_id,
        _brand_text(bot, language) + _cc(language)['purchase_payment'].format(
            amount=f"{int(session_row.quoted_amount or 0):,}", card=_format_bank_card(card),
            discount_block=discount_block,
        ),
        parse_mode='HTML',
    )


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
        api.send_message(chat_id, _cc(language)['purchase_duplicate'])
        return
    owner_id = _effective_owner_id(bot, user_id)
    card = _purchase_card(bot, owner_id=owner_id)
    session_row = _purchase_session(bot.id, user_id)
    base_price = int(_resolve_purchase_price(owner_id, package))
    final_amount, discount, applied, error_key = _evaluate_promos(
        bot, user_id, package, base_price, 'purchase',
        code=session_row.promo_code, api=api, owner_id=owner_id,
    )
    session_row.server_id = server.id
    session_row.package_id = package.id
    session_row.bank_card_id = card.id if card else None
    session_row.quoted_amount = final_amount
    session_row.promo_id = applied[0][0].id if applied else None
    session_row.discount_amount = discount or None
    session_row.promo_discounts_json = (
        json.dumps({str(promo.id): amount for promo, amount in applied}) if applied else None
    )
    if error_key == 'invalid_code':
        api.send_message(chat_id, _cc(language)['promo_code_invalid'])
    customer = db.session.get(CustomerAccount, identity.customer_id)
    balance = int(getattr(customer, 'credit', 0) or 0)
    if balance >= final_amount:
        session_row.action = 'awaiting_payment_method'
        state.step = 'verified'
        db.session.flush()
        api.send_message(
            chat_id,
            _cc(language)['wallet_balance'].format(balance=f"{balance:,}"),
            parse_mode='HTML',
            reply_markup={'inline_keyboard': [[{
                'text': _cc(language)['pay_from_wallet'].format(balance=f"{balance:,}"),
                'callback_data': 'purchase-pay-wallet',
            }], [{
                'text': _cc(language)['pay_by_card'],
                'callback_data': 'purchase-pay-card',
            }]]},
        )
        return
    if card is None:
        session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    session_row.action = 'awaiting_payment_method'
    state.step = 'verified'
    db.session.flush()
    api.send_message(
        chat_id,
        _cc(language)['wallet_insufficient'].format(
            balance=f"{balance:,}", needed=f"{max(0, final_amount - balance):,}"),
        reply_markup={'inline_keyboard': [[{
            'text': _cc(language)['wallet_topup_button'],
            'callback_data': 'wallet-topup-start',
        }], [{
            'text': _cc(language)['pay_by_card'],
            'callback_data': 'purchase-pay-card',
        }]]},
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


def _support_attachment_from_message(message: dict):
    """Return durable Telegram metadata for a safe support attachment."""
    photos = message.get('photo')
    if isinstance(photos, list) and photos:
        photo = photos[-1] if isinstance(photos[-1], dict) else {}
        size = int(photo.get('file_size') or 0)
        if photo.get('file_id') and size <= 20 * 1024 * 1024:
            return {
                'attachment_kind': 'photo',
                'attachment_file_id': str(photo['file_id']),
                'attachment_file_unique_id': str(photo.get('file_unique_id') or '')[:255] or None,
                'attachment_name': 'support-image.jpg',
                'attachment_mime': 'image/jpeg',
                'attachment_size': size,
            }
    document = message.get('document') if isinstance(message.get('document'), dict) else {}
    mime = str(document.get('mime_type') or 'application/octet-stream').lower()
    allowed = {
        'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'application/pdf',
        'text/plain', 'application/zip', 'application/x-zip-compressed',
    }
    size = int(document.get('file_size') or 0)
    if document.get('file_id') and mime in allowed and size <= 20 * 1024 * 1024:
        return {
            'attachment_kind': 'document',
            'attachment_file_id': str(document['file_id']),
            'attachment_file_unique_id': str(document.get('file_unique_id') or '')[:255] or None,
            'attachment_name': str(document.get('file_name') or 'support-file')[:255],
            'attachment_mime': mime[:127],
            'attachment_size': size,
        }
    return None


def _send_claim_candidates(api: TelegramBotApi, chat_id: int, language: str,
                           claim: OwnershipClaim | None):
    if claim is None:
        api.send_message(chat_id, _cc(language)["no_candidates"])
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
                "text": f'{_cc(language)["server_button"]}: {server_name}'[:60],
                "callback_data": "noop",
            }])
        label = str(item.client_email_snapshot or f'{_cc(language)["service_button"]} {index}')
        keyboard.append([{
            "text": f'{_cc(language)["account_button"]}: {label}'[:60],
            "callback_data": f"claim:{item.id}",
        }])
    keyboard.append([{
        "text": _cc(language)["no_link_button"],
        "callback_data": f"claim-none:{claim.id}",
    }])
    api.send_message(
        chat_id, _cc(language)["choose_service"],
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
                            request_type: str, *, package: Package | None, note: str | None,
                            attachment: dict | None = None, source_chat_id: int | None = None,
                            source_message_id: int | None = None,
                            api: TelegramBotApi | None = None,
                            bank_card_id: int | None = None,
                            receipt: tuple | None = None,
                            duplicate_receipt: bool = False,
                            payment_method: str = 'card'):
    if request_type == 'renewal':
        existing = TelegramServiceRequest.query.filter_by(
            service_ownership_id=ownership.id,
            request_type='renewal', status='pending',
        ).first()
        if existing is not None:
            return existing, True
    if request_type == 'support':
        existing = TelegramServiceRequest.query.filter_by(
            bot_instance_id=bot_id,
            telegram_user_id=user_id,
            service_ownership_id=ownership.id,
            request_type='support',
            status='pending',
        ).order_by(TelegramServiceRequest.id.desc()).first()
        clean_note = str(note or '').strip()[:4000]
        if existing is not None:
            if clean_note or attachment:
                if clean_note:
                    existing.note = clean_note
                existing.updated_at = datetime.utcnow()
                db.session.add(TelegramServiceRequestMessage(
                    request_id=existing.id,
                    sender_type='customer',
                    message=clean_note,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    **(attachment or {}),
                ))
            return existing, True
    amount = None
    original_amount = None
    discount_amount = None
    if package is not None:
        # Renewal amount uses the reseller-aware resolver (not raw package.price),
        # then automatic renewal promos.
        base_price = _resolve_purchase_price(ownership.reseller_id, package)
        if request_type == 'renewal':
            bot = db.session.get(TelegramBotInstance, int(bot_id))
            amount, discount, applied, _err = _evaluate_promos(
                bot, user_id, package, base_price, 'renewal',
                api=api, owner_id=ownership.reseller_id,
            ) if bot else (base_price, 0, [], None)
            original_amount = base_price
            discount_amount = discount or None
            for promo, discounted in applied:
                db.session.add(TelegramPromoUse(
                    promo_id=promo.id,
                    telegram_user_id=user_id,
                    customer_id=ownership.customer_id,
                    purchase_request_id=None,
                    amount_discounted=int(discounted or 0),
                ))
        else:
            amount = base_price
    row = TelegramServiceRequest(
        bot_instance_id=bot_id,
        telegram_user_id=user_id,
        customer_id=ownership.customer_id,
        service_ownership_id=ownership.id,
        request_type=request_type,
        package_id=(package.id if package else None),
        amount=amount,
        original_amount=original_amount,
        discount_amount=discount_amount,
        note=(str(note or '').strip()[:4000] or None),
        bank_card_id=bank_card_id,
        receipt_file_id=(receipt[1] if receipt else None),
        receipt_file_kind=(receipt[0] if receipt else None),
        receipt_file_unique_id=(receipt[2] or None) if receipt else None,
        duplicate_receipt=bool(duplicate_receipt),
        payment_method=payment_method,
        status='pending',
    )
    db.session.add(row)
    db.session.flush()
    if request_type == 'support' and (row.note or attachment):
        db.session.add(TelegramServiceRequestMessage(
            request_id=row.id,
            sender_type='customer',
            message=row.note or '',
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            **(attachment or {}),
        ))
    return row, False


def _renewal_quoted_price(api: TelegramBotApi, bot: TelegramBotInstance, user_id: int,
                          ownership: ServiceOwnership, package: Package) -> int:
    base_price = _resolve_purchase_price(ownership.reseller_id, package)
    final_price, _discount, _applied, _err = _evaluate_promos(
        bot, user_id, package, base_price, 'renewal',
        api=api, owner_id=ownership.reseller_id,
    )
    return int(final_price or 0)


def _send_renewal_payment_choice(api: TelegramBotApi, bot: TelegramBotInstance,
                                 chat_id: int, user_id: int, language: str,
                                 ownership: ServiceOwnership, package: Package,
                                 state: TelegramBotUserState):
    """Step 2 of renewal: choose wallet or card-to-card before any request exists."""
    _identity_row, customer = _wallet_customer(user_id)
    session_row = _service_session(bot.id, user_id)
    session_row.service_ownership_id = ownership.id
    session_row.action = f'renew_package:{package.id}'
    db.session.flush()
    final_price = _renewal_quoted_price(api, bot, user_id, ownership, package)
    balance = int(customer.credit or 0) if customer else 0
    card_button = [{'text': _cc(language)['pay_by_card'],
                    'callback_data': f'renew-pay-card:{ownership.id}:{package.id}'}]
    if customer and balance >= final_price:
        keyboard = [[{
            'text': _cc(language)['pay_from_wallet'].format(balance=f"{balance:,}"),
            'callback_data': f'renew-pay-wallet:{ownership.id}:{package.id}',
        }], card_button]
        api.send_message(
            chat_id,
            _cc(language)['wallet_balance'].format(balance=f"{balance:,}"),
            parse_mode='HTML',
            reply_markup={'inline_keyboard': keyboard},
        )
        return
    needed = max(0, final_price - balance)
    keyboard = [[{'text': _cc(language)['wallet_topup_button'],
                  'callback_data': 'wallet-topup-start'}], card_button]
    api.send_message(
        chat_id,
        _cc(language)['wallet_insufficient'].format(
            balance=f"{balance:,}", needed=f"{needed:,}"),
        reply_markup={'inline_keyboard': keyboard},
    )


def _begin_renewal_card_payment(api: TelegramBotApi, bot: TelegramBotInstance,
                                chat_id: int, user_id: int, language: str,
                                ownership: ServiceOwnership, package: Package,
                                state: TelegramBotUserState):
    card = _purchase_card(bot, owner_id=ownership.reseller_id)
    if card is None:
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    session_row = _service_session(bot.id, user_id)
    session_row.service_ownership_id = ownership.id
    session_row.action = f'renew_receipt:{package.id}:{card.id}'
    state.step = 'awaiting_renewal_receipt'
    db.session.flush()
    final_price = _renewal_quoted_price(api, bot, user_id, ownership, package)
    api.send_message(
        chat_id,
        _cc(language)['renewal_payment'].format(
            amount=f"{final_price:,}", card=_format_bank_card(card)),
        parse_mode='HTML',
    )


def _create_renewal_wallet_request(api: TelegramBotApi, bot: TelegramBotInstance,
                                   chat_id: int, user_id: int, language: str,
                                   ownership: ServiceOwnership, package: Package):
    request_row, duplicate = _create_service_request(
        bot.id, user_id, ownership, 'renewal', package=package, note=None, api=api,
        payment_method='wallet',
    )
    if duplicate:
        _send_renewal_request_state(api, chat_id, language, request_row, duplicate=True)
        return
    amount = int(request_row.amount or 0)
    customer = db.session.get(CustomerAccount, request_row.customer_id)
    if not customer or int(customer.credit or 0) < amount:
        # Balance changed since the choice screen; never create an unpaid wallet request.
        db.session.delete(request_row)
        db.session.flush()
        balance = int(customer.credit or 0) if customer else 0
        api.send_message(
            chat_id,
            _cc(language)['wallet_insufficient'].format(
                balance=f"{balance:,}", needed=f"{max(0, amount - balance):,}"),
        )
        return
    customer.credit = int(customer.credit or 0) - amount
    db.session.add(CustomerTransaction(
        customer_id=customer.id,
        type='renewal',
        amount=-amount,
        request_ref=f'renewal:{request_row.id}',
    ))
    db.session.flush()
    _send_renewal_request_state(api, chat_id, language, request_row, duplicate=False)
    _notify_service_request_admins(api, request_row)


def _handle_renewal_receipt(api: TelegramBotApi, bot: TelegramBotInstance,
                            message: dict, sender: dict,
                            state: TelegramBotUserState):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    if not _rate_ok(user_id, 'receipt', 10, 120):
        return
    language = state.language if state.language in COPY else bot.default_language
    receipt = _receipt_from_message(message)
    if receipt is None:
        api.send_message(chat_id, _cc(language)['receipt_invalid'])
        return
    session_row = TelegramServiceSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id,
    ).first()
    parts = str(getattr(session_row, 'action', '') or '').split(':')
    try:
        package_id = int(parts[1])
        card_id = int(parts[2])
    except (IndexError, TypeError, ValueError):
        package_id, card_id = 0, 0
    _identity_row, ownership = _owned_service(
        user_id, int(session_row.service_ownership_id or 0)) if session_row else (None, None)
    package = db.session.get(Package, package_id) if package_id else None
    card = db.session.get(BankCard, card_id) if card_id else None
    allowed_package_ids = {row.id for row in _available_packages(ownership)} if ownership else set()
    if (not session_row or parts[0] != 'renew_receipt' or not ownership or not package
            or package.id not in allowed_package_ids or not card or not card.is_active):
        if session_row:
            session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    kind, file_id, unique_id = receipt
    request_row, duplicate = _create_service_request(
        bot.id, user_id, ownership, 'renewal', package=package, note=None, api=api,
        bank_card_id=card.id,
        receipt=(kind, file_id, unique_id),
        duplicate_receipt=_receipt_is_duplicate(unique_id),
        payment_method='card',
    )
    session_row.action = None
    session_row.service_ownership_id = None
    state.step = 'verified'
    db.session.flush()
    _send_renewal_request_state(api, chat_id, language, request_row, duplicate=duplicate)
    if not duplicate:
        _notify_service_request_admins(api, request_row)


def _send_renewal_request_state(api: TelegramBotApi, chat_id: int, language: str,
                                request_row: TelegramServiceRequest, *, duplicate: bool):
    if not duplicate:
        api.send_message(chat_id, _cc(language)['renew_pending'])
        return
    api.send_message(
        chat_id,
        _cc(language)['renew_duplicate'].format(request_id=request_row.id),
        reply_markup={"inline_keyboard": [[{
            "text": _cc(language)['renew_cancel_button'],
            "callback_data": f"renew-request:{request_row.id}:cancel",
        }]]},
    )
    # A duplicate means the customer has returned to an unresolved request. Send
    # the existing review card again so an admin can actually finish it instead
    # of leaving the customer trapped behind a generic warning.
    _notify_service_request_admins(api, request_row)


def _notify_service_request_admins(api: TelegramBotApi, request_row: TelegramServiceRequest,
                                   incoming_message: dict | None = None):
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
    if request_row.request_type == 'support':
        lines.append(f"Priority: {str(request_row.support_priority or 'normal').upper()}")
        lines.append(
            f"Assigned: {request_row.assigned_admin.username if request_row.assigned_admin else 'Unassigned'}"
        )
    if request_row.package:
        lines.append(f"Package: {request_row.package.name}")
        lines.append(f"Amount: {int(request_row.amount or 0):,} T")
    if request_row.request_type == 'renewal':
        payment_method = str(getattr(request_row, 'payment_method', '') or 'card')
        lines.append(f"Payment: {'💰 Wallet' if payment_method == 'wallet' else '💳 Card'}")
        if request_row.bank_card:
            lines.append(
                f"Card: {request_row.bank_card.label or request_row.bank_card.card_number or ''}")
        if getattr(request_row, 'duplicate_receipt', False):
            lines.append('⚠️ FRAUD WARNING: this receipt file was already used on another request!')
    if request_row.note:
        lines.append(f"Message: {request_row.note[:1000]}")
    if request_row.request_type == 'support':
        identity = TelegramIdentity.query.filter_by(
            telegram_user_id=request_row.telegram_user_id,
        ).first()
        username = str(getattr(identity, 'username', '') or '').lstrip('@')
        private_url = (f'https://t.me/{username}' if username
                       else f'tg://user?id={request_row.telegram_user_id}')
        rows = [[
            {"text": "💬 Reply", "callback_data": f"admin-support:{request_row.id}:reply"},
            {"text": "👤 Open PV", "url": private_url},
        ]]
        rows.append([{
            "text": "🙋 Claim ticket",
            "callback_data": f"admin-support:{request_row.id}:claim",
        }])
        panel_url = _public_base_url().rstrip('/')
        if panel_url:
            rows.append([{
                "text": "🖥 Open Eve inbox",
                "url": f'{panel_url}/telegram-operations?kind=support&request={request_row.id}',
            }])
        rows.append([{
            "text": "✅ Close ticket",
            "callback_data": f"admin-support:{request_row.id}:close",
        }])
        keyboard = {"inline_keyboard": rows}
    else:
        keyboard = {"inline_keyboard": [
            [
                {"text": "✅ Approve & renew", "callback_data": f"admin-service:{request_row.id}:complete"},
                {"text": "❌ Reject", "callback_data": f"admin-service:{request_row.id}:reject"},
            ],
            [{"text": "✏️ Edit card", "callback_data": f"admin-edit-card:renewal:{request_row.id}"}],
        ]}
    for admin in Admin.query.filter_by(enabled=True).all():
        role = str(admin.role or '').lower()
        is_global_admin = bool(admin.is_superadmin or role in ('admin', 'superadmin'))
        is_owner_reseller = bool(role == 'reseller' and ownership.reseller_id == admin.id)
        if not (is_global_admin or is_owner_reseller):
            continue
        try:
            admin_chat_id = int(str(admin.telegram_id or '').strip())
            if admin_chat_id > 0:
                if request_row.receipt_file_id:
                    try:
                        if request_row.receipt_file_kind == 'document':
                            api.send_document(admin_chat_id, request_row.receipt_file_id)
                        else:
                            api.send_photo(admin_chat_id, request_row.receipt_file_id)
                    except TelegramApiError:
                        pass
                api.send_message(admin_chat_id, "\n".join(lines), reply_markup=keyboard)
                source_chat = int(((incoming_message or {}).get('chat') or {}).get('id') or 0)
                source_message = int((incoming_message or {}).get('message_id') or 0)
                if source_chat and source_message and _support_attachment_from_message(incoming_message or {}):
                    api.copy_message(admin_chat_id, source_chat, source_message)
        except (TypeError, ValueError, TelegramApiError):
            continue
    if request_row.request_type == 'support':
        _notify_support_group(api, request_row, incoming_message=incoming_message)


def _support_group_route(api: TelegramBotApi, request_row: TelegramServiceRequest):
    """Return the configured group and create one forum topic per ticket when enabled."""
    bot = db.session.get(TelegramBotInstance, request_row.bot_instance_id)
    if not bot or not bot.support_group_enabled or not bot.support_group_chat_id:
        return None, {}
    chat_id = int(bot.support_group_chat_id)
    request_row.support_group_chat_id = chat_id
    if bot.support_group_topics and not request_row.support_message_thread_id:
        ownership = request_row.ownership
        server_name = getattr(getattr(ownership, 'server', None), 'name', '') or 'Server'
        account = getattr(ownership, 'client_email_snapshot', '') or f'account-{request_row.id}'
        topic_name = f'#{request_row.id} {server_name} · {account}'[:128]
        try:
            result, _route_name = api.create_forum_topic(chat_id, topic_name)
            request_row.support_message_thread_id = int(
                (result or {}).get('message_thread_id') or 0
            ) or None
        except TelegramApiError:
            # A regular group (or a forum where the bot lacks topic permission)
            # still receives the ticket card; operators reply directly to it.
            request_row.support_message_thread_id = None
    extra = ({'message_thread_id': int(request_row.support_message_thread_id)}
             if request_row.support_message_thread_id else {})
    return chat_id, extra


def _notify_support_group(api: TelegramBotApi, request_row: TelegramServiceRequest,
                          *, incoming_message: dict | None = None):
    chat_id, thread_extra = _support_group_route(api, request_row)
    if not chat_id:
        return
    ownership = request_row.ownership
    server_name = getattr(getattr(ownership, 'server', None), 'name', '') or '-'
    account = getattr(ownership, 'client_email_snapshot', '') or f'#{request_row.id}'
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=request_row.telegram_user_id,
        customer_id=request_row.customer_id,
    ).first()
    username = str(getattr(identity, 'username', '') or '').lstrip('@')
    private_url = (f'https://t.me/{username}' if username
                   else f'tg://user?id={request_row.telegram_user_id}')
    lines = [
        f'🎫 Support ticket #{request_row.id}',
        f'Priority: {str(request_row.support_priority or "normal").upper()}',
        f'Assigned: {request_row.assigned_admin.username if request_row.assigned_admin else "Unassigned"}',
        f'Server: {server_name}',
        f'Account: {account}',
        f'Telegram user: {request_row.telegram_user_id}',
    ]
    if request_row.note:
        lines.extend(['', f'Customer: {request_row.note[:2500]}'])
    bot = db.session.get(TelegramBotInstance, request_row.bot_instance_id)
    sla_minutes = max(0, int(getattr(bot, 'support_sla_minutes', 0) or 0))
    if sla_minutes:
        lines.append(f'SLA: reply within {sla_minutes} minutes')
    keyboard = [[
        {'text': '🙋 Claim', 'callback_data': f'admin-support:{request_row.id}:claim'},
        {'text': '👤 Open PV', 'url': private_url},
        {'text': '✅ Close ticket', 'callback_data': f'admin-support:{request_row.id}:close'},
    ]]
    panel_url = _public_base_url().rstrip('/')
    if panel_url:
        keyboard.append([{
            'text': '🖥 Open Eve inbox',
            'url': f'{panel_url}/telegram-operations?kind=support&request={request_row.id}',
        }])
    try:
        send_extra = dict(thread_extra)
        if not thread_extra and request_row.support_group_message_id:
            send_extra['reply_parameters'] = {
                'message_id': int(request_row.support_group_message_id),
            }
        result, _route_name = api.send_message(
            chat_id, '\n'.join(lines),
            reply_markup={'inline_keyboard': keyboard},
            **send_extra,
        )
        if thread_extra or not request_row.support_group_message_id:
            request_row.support_group_message_id = int(
                (result or {}).get('message_id') or 0
            ) or request_row.support_group_message_id
        source_chat = int(((incoming_message or {}).get('chat') or {}).get('id') or 0)
        source_message = int((incoming_message or {}).get('message_id') or 0)
        if source_chat and source_message and _support_attachment_from_message(incoming_message or {}):
            api.copy_message(chat_id, source_chat, source_message, **send_extra)
    except TelegramApiError:
        return


def _support_sla_alert(api: TelegramBotApi, request_row: TelegramServiceRequest,
                       *, escalated: bool):
    """Notify the right operators once for the current customer message."""
    ownership = request_row.ownership
    server_name = getattr(getattr(ownership, 'server', None), 'name', '') or '-'
    account = getattr(ownership, 'client_email_snapshot', '') or f'#{request_row.id}'
    assigned = request_row.assigned_admin.username if request_row.assigned_admin else 'Unassigned'
    title = '🚨 SLA ESCALATION' if escalated else '⏳ SLA warning'
    lines = [
        f'{title} · support ticket #{request_row.id}',
        f'Priority: {str(request_row.support_priority or "normal").upper()}',
        f'Assigned: {assigned}',
        f'Server: {server_name}',
        f'Account: {account}',
        f'Telegram user: {request_row.telegram_user_id}',
    ]
    panel_url = _public_base_url().rstrip('/')
    keyboard = [[
        {'text': '🙋 Claim', 'callback_data': f'admin-support:{request_row.id}:claim'},
        {'text': '💬 Reply', 'callback_data': f'admin-support:{request_row.id}:reply'},
    ]]
    if panel_url:
        keyboard.append([{
            'text': '🖥 Open Eve inbox',
            'url': f'{panel_url}/telegram-operations?kind=support&request={request_row.id}',
        }])

    eligible_admins = []
    for admin in Admin.query.filter_by(enabled=True).all():
        role = str(admin.role or '').lower()
        is_global_admin = bool(admin.is_superadmin or role in ('admin', 'superadmin'))
        is_owner_reseller = bool(role == 'reseller' and ownership.reseller_id == admin.id)
        if is_global_admin or is_owner_reseller:
            eligible_admins.append(admin)
    targets = eligible_admins
    if not escalated and request_row.assigned_admin_id:
        assigned_targets = [admin for admin in eligible_admins
                            if admin.id == request_row.assigned_admin_id]
        has_assigned_chat = any(
            str(admin.telegram_id or '').strip().isdigit()
            and int(str(admin.telegram_id).strip()) > 0
            for admin in assigned_targets
        )
        if has_assigned_chat:
            targets = assigned_targets
    for admin in targets:
        try:
            admin_chat_id = int(str(admin.telegram_id or '').strip())
            if admin_chat_id > 0:
                api.send_message(
                    admin_chat_id, '\n'.join(lines),
                    reply_markup={'inline_keyboard': keyboard},
                )
        except (TypeError, ValueError, TelegramApiError):
            continue

    if request_row.support_group_chat_id:
        extra = {}
        if request_row.support_message_thread_id:
            extra['message_thread_id'] = int(request_row.support_message_thread_id)
        elif request_row.support_group_message_id:
            extra['reply_parameters'] = {'message_id': int(request_row.support_group_message_id)}
        try:
            api.send_message(
                int(request_row.support_group_chat_id), '\n'.join(lines),
                reply_markup={'inline_keyboard': keyboard}, **extra,
            )
        except TelegramApiError:
            pass


def _scan_support_sla(api: TelegramBotApi, bot: TelegramBotInstance):
    """Send durable, de-duplicated warning/escalation alerts for open tickets."""
    sla_minutes = max(0, int(bot.support_sla_minutes or 0))
    if not sla_minutes:
        return
    warning_percent = max(1, min(99, int(bot.support_sla_warning_percent or 80)))
    escalation_minutes = max(0, int(bot.support_escalation_minutes or 0))
    now = datetime.utcnow()
    rows = TelegramServiceRequest.query.filter_by(
        bot_instance_id=bot.id, request_type='support', status='pending',
    ).all()
    changed = False
    for request_row in rows:
        messages = list(request_row.messages or [])
        latest = messages[-1] if messages else None
        if not latest or latest.sender_type != 'customer' or not latest.created_at:
            continue
        warning_due = latest.created_at + timedelta(
            seconds=(sla_minutes * 60 * warning_percent / 100),
        )
        escalation_due = latest.created_at + timedelta(
            minutes=sla_minutes + escalation_minutes,
        )
        if now >= escalation_due and request_row.sla_escalated_message_id != latest.id:
            if str(request_row.support_priority or 'normal').lower() != 'urgent':
                request_row.support_priority = 'urgent'
            _support_sla_alert(api, request_row, escalated=True)
            request_row.sla_warning_message_id = latest.id
            request_row.sla_escalated_message_id = latest.id
            request_row.sla_warning_at = now
            request_row.sla_escalated_at = now
            request_row.updated_at = now
            changed = True
        elif now >= warning_due and request_row.sla_warning_message_id != latest.id:
            _support_sla_alert(api, request_row, escalated=False)
            request_row.sla_warning_message_id = latest.id
            request_row.sla_warning_at = now
            request_row.updated_at = now
            changed = True
    if changed:
        db.session.commit()


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


def _support_reviewer_can_handle(reviewer: Admin, request_row: TelegramServiceRequest) -> bool:
    if not request_row.assigned_admin_id or request_row.assigned_admin_id == reviewer.id:
        return True
    return bool(reviewer.is_superadmin or str(reviewer.role or '').lower() == 'superadmin')


def _customer_support_request(user_id: int, request_id: int):
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not identity or not identity.customer_id:
        return None
    return TelegramServiceRequest.query.filter_by(
        id=request_id,
        telegram_user_id=user_id,
        customer_id=identity.customer_id,
        request_type='support',
    ).first()


def _support_waiting_state(request_row: TelegramServiceRequest) -> str:
    if request_row.status != 'pending':
        return 'closed'
    messages = list(request_row.messages or [])
    if messages and messages[-1].sender_type == 'admin':
        return 'waiting_customer'
    return 'in_progress' if request_row.assigned_admin_id else 'waiting_admin'


def _support_status_label(request_row: TelegramServiceRequest, language: str) -> str:
    return _cc(language)[f"support_status_{_support_waiting_state(request_row)}"]


def _purchase_status_label(request_row: TelegramPurchaseRequest, language: str) -> str:
    status = str(request_row.status or 'pending').lower()
    return _cc(language).get(f'purchase_status_{status}', status.replace('_', ' ').title())


def _customer_purchase_order(bot_id: int, user_id: int, request_id: int):
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not identity or not identity.customer_id:
        return None
    return TelegramPurchaseRequest.query.filter_by(
        id=request_id,
        bot_instance_id=bot_id,
        telegram_user_id=user_id,
        customer_id=identity.customer_id,
    ).first()


def _send_purchase_orders(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int,
                          user_id: int, language: str):
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not identity or not identity.customer_id:
        api.send_message(chat_id, _cc(language)['purchase_orders_empty'])
        return
    rows = TelegramPurchaseRequest.query.filter_by(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        customer_id=identity.customer_id,
    ).order_by(
        TelegramPurchaseRequest.updated_at.desc(), TelegramPurchaseRequest.id.desc(),
    ).limit(20).all()
    if not rows:
        api.send_message(chat_id, _cc(language)['purchase_orders_empty'])
        return
    keyboard = []
    for row in rows:
        package_name = getattr(row.package, 'name', '') or f'#{row.package_id}'
        label = f"#{row.id} · {_purchase_status_label(row, language)} · {package_name}"
        keyboard.append([{
            'text': label[:64],
            'callback_data': f'purchase-order:{row.id}',
        }])
    api.send_message(
        chat_id, _cc(language)['purchase_orders_list'],
        reply_markup={'inline_keyboard': keyboard},
    )


def _send_purchase_order(api: TelegramBotApi, chat_id: int, language: str,
                         request_row: TelegramPurchaseRequest):
    server_name = getattr(request_row.server, 'name', '') or f'#{request_row.server_id}'
    package_name = getattr(request_row.package, 'name', '') or f'#{request_row.package_id}'
    account_name = str(getattr(request_row.detail, 'account_name', '') or '').strip()
    lines = [
        f"<b>{html.escape(_cc(language)['purchase_order_title'].format(order_id=request_row.id))}</b>",
        f"{_cc(language)['service_status']}: <b>{html.escape(_purchase_status_label(request_row, language))}</b>",
        f"{_cc(language)['service_server']}: <b>{html.escape(str(server_name))}</b>",
        f"{_cc(language)['purchase_order_package']}: <b>{html.escape(str(package_name))}</b>",
        f"{_cc(language)['purchase_order_amount']}: <b>{int(request_row.amount or 0):,} T</b>",
    ]
    if account_name:
        lines.append(
            f"{_cc(language)['service_account']}: <code>{html.escape(account_name)}</code>"
        )
    keyboard = [[{
        'text': _cc(language)['purchase_order_refresh'],
        'callback_data': f'purchase-order:{request_row.id}',
    }]]
    if request_row.status == 'completed' and account_name:
        ownership = next((row for row in ServiceOwnership.query.filter_by(
            customer_id=request_row.customer_id,
            server_id=request_row.server_id,
            revoked_at=None,
        ).all() if str(row.client_email_snapshot or '').strip().lower() == account_name.lower()), None)
        if ownership:
            keyboard.append([{
                'text': _cc(language)['menu_services'],
                'callback_data': f'service:{ownership.id}',
            }])
    if request_row.status in ('completed', 'rejected', 'cancelled'):
        keyboard.append([{
            'text': _cc(language)['purchase_order_buy_again'],
            'callback_data': 'purchase-start',
        }])
    api.send_message(
        chat_id, '\n'.join(lines), parse_mode='HTML',
        reply_markup={'inline_keyboard': keyboard},
    )


def _send_support_requests(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int,
                           user_id: int, language: str):
    rows = TelegramServiceRequest.query.filter_by(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        request_type='support',
    ).order_by(TelegramServiceRequest.updated_at.desc(), TelegramServiceRequest.id.desc()).limit(20).all()
    if not rows:
        api.send_message(chat_id, _cc(language)['support_no_tickets'])
        return
    keyboard = []
    for row in rows:
        account = row.ownership.client_email_snapshot if row.ownership else f'#{row.id}'
        label = f"#{row.id} • {_support_status_label(row, language)} • {account}"
        keyboard.append([{"text": label[:60], "callback_data": f"support-ticket:{row.id}"}])
    api.send_message(
        chat_id, _cc(language)['support_ticket_list'],
        reply_markup={"inline_keyboard": keyboard},
    )


def _send_support_ticket(api: TelegramBotApi, chat_id: int, user_id: int,
                         language: str, request_row: TelegramServiceRequest):
    ownership = request_row.ownership
    account = ownership.client_email_snapshot if ownership else f'#{request_row.id}'
    server_name = getattr(getattr(ownership, 'server', None), 'name', '') or '-'
    lines = [
        f"<b>{html.escape(_cc(language)['support_ticket_title'].format(request_id=request_row.id))}</b>",
        f"{_cc(language)['service_server']}: <b>{html.escape(str(server_name))}</b>",
        f"{_cc(language)['service_account']}: <code>{html.escape(str(account))}</code>",
        f"{_cc(language)['service_status']}: <b>{html.escape(_support_status_label(request_row, language))}</b>",
        '',
    ]
    recent = list(request_row.messages or [])[-10:]
    for message in recent:
        sender_label = (_cc(language)['support_sender_admin']
                        if message.sender_type == 'admin'
                        else _cc(language)['support_sender_customer'])
        body = str(message.message or '').strip()
        attachment = str(message.attachment_name or '').strip()
        rendered = body[:700] if body else ''
        if attachment:
            rendered = f"{rendered}\n📎 {attachment}".strip()
        lines.append(f"<b>{html.escape(sender_label)}:</b> {html.escape(rendered or '—')}")
    keyboard = []
    if request_row.status == 'pending':
        keyboard.append([{
            "text": _cc(language)['support_continue_button'],
            "callback_data": f"support-reply:{request_row.id}",
        }])
    elif ownership:
        keyboard.append([{
            "text": _cc(language)['support_new_button'],
            "callback_data": f"service-support:{ownership.id}",
        }])
    keyboard.append([{
        "text": _cc(language)['support_back_button'],
        "callback_data": "support-list",
    }])
    api.send_message(
        chat_id, '\n'.join(lines), parse_mode='HTML',
        reply_markup={"inline_keyboard": keyboard},
    )
    # Re-send the latest attachments so the customer can open them from history.
    for message in [row for row in recent if row.attachment_file_id][-3:]:
        try:
            if message.attachment_kind == 'photo':
                api.send_photo(chat_id, message.attachment_file_id)
            else:
                api.send_document(chat_id, message.attachment_file_id)
        except TelegramApiError:
            continue


def _execute_renewal_request(request_row: TelegramServiceRequest, reviewer: Admin):
    if not request_row.package_id or not request_row.package:
        return False, 'The renewal request has no package.'
    ownership = request_row.ownership
    client, inbound_id = _cached_owned_service_location(ownership)
    email = str(
        (client or {}).get('email') or ownership.client_email_snapshot or ''
    ).strip()
    if not client or inbound_id is None or not email:
        return False, 'The service is not present in the latest server snapshot. Refresh the server and retry.'
    path = f"/api/client/{ownership.server_id}/{inbound_id}/{quote(email, safe='')}/renew"
    with app.test_request_context(
        path,
        method='POST',
        json={
            'mode': 'package',
            'package_id': request_row.package_id,
            'reset_traffic': False,
            'free': False,
        },
    ):
        flask_session['admin_id'] = reviewer.id
        flask_session['admin_username'] = reviewer.username
        flask_session['role'] = reviewer.role
        flask_session['is_superadmin'] = bool(reviewer.is_superadmin)
        response = renew_client(ownership.server_id, inbound_id, email)
    status_code = 200
    if isinstance(response, tuple):
        response, status_code = response[0], int(response[1])
    payload = response.get_json(silent=True) if hasattr(response, 'get_json') else None
    payload = payload if isinstance(payload, dict) else {}
    if status_code < 400 and payload.get('success'):
        return True, payload
    return False, str(payload.get('error') or f'Renewal failed with HTTP {status_code}')


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
    if parts[2] == 'complete' and request_row.request_type == 'renewal':
        renewed, result = _execute_renewal_request(request_row, reviewer)
        if not renewed:
            api.answer_callback(callback_id, 'Renewal failed')
            if chat_id:
                api.send_message(
                    chat_id,
                    f"Renewal request #{request_row.id} is still pending.\nError: {result}",
                )
            return True
    request_row.status = 'completed' if parts[2] == 'complete' else 'rejected'
    request_row.reviewed_by_admin_id = reviewer.id
    request_row.reviewed_at = datetime.utcnow()
    refunded_amount = 0
    if (request_row.status == 'rejected' and request_row.request_type == 'renewal'
            and str(getattr(request_row, 'payment_method', '') or 'card') == 'wallet'):
        wallet_customer = db.session.get(CustomerAccount, request_row.customer_id)
        refunded_amount = int(request_row.amount or 0)
        if wallet_customer and refunded_amount > 0:
            wallet_customer.credit = int(wallet_customer.credit or 0) + refunded_amount
            db.session.add(CustomerTransaction(
                customer_id=wallet_customer.id,
                type='refund',
                amount=refunded_amount,
                request_ref=f'renewal:{request_row.id}',
            ))
        else:
            refunded_amount = 0
    _log_audit(f"telegram_service.{parts[2]}", request_row, actor=reviewer)
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
        if request_row.status == 'completed':
            text = _cc(language)['request_completed']
        elif refunded_amount:
            text = _cc(language)['request_rejected_refund'].format(
                amount=f"{refunded_amount:,}",
                balance=f"{int(customer.credit or 0):,}",
            )
        else:
            text = _cc(language)['request_rejected']
        api.send_message(identity.telegram_chat_id, text)
    return True


def _handle_admin_support_callback(api: TelegramBotApi, bot: TelegramBotInstance,
                                   callback: dict, data: str) -> bool:
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'admin-support' or parts[2] not in ('reply', 'close', 'claim'):
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    user_id = int(sender.get('id') or 0)
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    try:
        request_row = db.session.get(TelegramServiceRequest, int(parts[1]))
    except (TypeError, ValueError):
        request_row = None
    reviewer = _service_request_reviewer(user_id, request_row) if request_row else None
    if not request_row or request_row.request_type != 'support' or not reviewer:
        if callback_id:
            api.answer_callback(callback_id, 'Access denied')
        return True
    if request_row.status != 'pending':
        api.answer_callback(callback_id, 'Ticket is already closed')
        return True
    if parts[2] == 'claim':
        if request_row.assigned_admin_id and request_row.assigned_admin_id != reviewer.id:
            api.answer_callback(callback_id, 'Already assigned to another operator')
            return True
        request_row.assigned_admin_id = reviewer.id
        request_row.updated_at = datetime.utcnow()
        db.session.flush()
        api.answer_callback(callback_id, 'Ticket assigned to you')
        thread_id = int(((callback.get('message') or {}).get('message_thread_id') or 0))
        if chat_id:
            api.send_message(
                chat_id, f'🙋 Ticket #{request_row.id} assigned to {reviewer.username}.',
                **({'message_thread_id': thread_id} if thread_id else {}),
            )
        return True
    if not _support_reviewer_can_handle(reviewer, request_row):
        api.answer_callback(callback_id, 'Assigned to another operator')
        return True
    if parts[2] == 'close':
        if not request_row.assigned_admin_id:
            request_row.assigned_admin_id = reviewer.id
        request_row.status = 'completed'
        request_row.reviewed_by_admin_id = reviewer.id
        request_row.reviewed_at = datetime.utcnow()
        db.session.flush()
        api.answer_callback(callback_id, 'Ticket closed')
        customer = db.session.get(CustomerAccount, request_row.customer_id)
        language = str(getattr(customer, 'preferred_language', '') or 'fa')
        language = language if language in COPY else 'fa'
        identity = TelegramIdentity.query.filter_by(
            telegram_user_id=request_row.telegram_user_id,
            customer_id=request_row.customer_id,
        ).first()
        if identity and identity.telegram_chat_id:
            api.send_message(
                identity.telegram_chat_id,
                _cc(language)['request_completed'],
                reply_markup={"inline_keyboard": [[{
                    "text": _cc(language)['support_view_button'],
                    "callback_data": f"support-ticket:{request_row.id}",
                }]]},
            )
        if chat_id:
            callback_thread = int(
                ((callback.get('message') or {}).get('message_thread_id') or 0)
            ) or None
            extra = {'message_thread_id': callback_thread} if callback_thread else {}
            api.send_message(chat_id, f'Support ticket #{request_row.id} closed.', **extra)
        if request_row.support_group_chat_id and request_row.support_message_thread_id:
            try:
                api.close_forum_topic(
                    request_row.support_group_chat_id,
                    request_row.support_message_thread_id,
                )
            except TelegramApiError:
                pass
        return True
    state = _state(bot, user_id)
    session_row = _service_session(bot.id, user_id)
    session_row.action = f'admin_support:{request_row.id}'
    session_row.service_ownership_id = request_row.service_ownership_id
    state.step = 'awaiting_admin_support_reply'
    db.session.flush()
    api.answer_callback(callback_id, 'Send your reply')
    api.send_message(
        chat_id,
        f'Reply to support ticket #{request_row.id}. Send text, an image, or a file.',
    )
    return True


def _handle_renewal_request_callback(api: TelegramBotApi, callback: dict, data: str) -> bool:
    parts = data.split(':')
    if len(parts) != 3 or parts[0] != 'renew-request' or parts[2] != 'cancel':
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    user_id = int(sender.get('id') or 0)
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    try:
        request_row = db.session.get(TelegramServiceRequest, int(parts[1]))
    except (TypeError, ValueError):
        request_row = None
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    allowed = bool(
        request_row and identity and identity.customer_id == request_row.customer_id
        and request_row.telegram_user_id == user_id
    )
    if not allowed:
        if callback_id:
            api.answer_callback(callback_id, 'Access denied')
        return True
    customer = db.session.get(CustomerAccount, request_row.customer_id)
    language = str(getattr(customer, 'preferred_language', '') or 'fa')
    if language not in COPY:
        language = 'fa'
    if request_row.status != 'pending':
        api.answer_callback(callback_id, 'Already reviewed')
        return True
    request_row.status = 'cancelled'
    request_row.reviewed_at = datetime.utcnow()
    db.session.flush()
    api.answer_callback(callback_id, 'Cancelled')
    api.send_message(
        chat_id,
        _cc(language)['renew_cancelled'].format(request_id=request_row.id),
    )
    return True


def _purchase_reviewer(user_id: int, request_row: TelegramPurchaseRequest):
    admin = _telegram_admin(user_id)
    if admin:
        return admin
    bot = db.session.get(TelegramBotInstance, request_row.bot_instance_id)
    owner_id = _effective_owner_id(bot, request_row.telegram_user_id) if bot else None
    owner = db.session.get(Admin, owner_id) if owner_id else None
    try:
        owner_telegram_id = int(str(getattr(owner, 'telegram_id', '') or '').strip())
    except (TypeError, ValueError):
        owner_telegram_id = 0
    if owner and owner.enabled and owner_telegram_id == int(user_id):
        return owner
    return None


def _purchase_admins(request_row: TelegramPurchaseRequest):
    bot = db.session.get(TelegramBotInstance, request_row.bot_instance_id)
    owner_id = _effective_owner_id(bot, request_row.telegram_user_id) if bot else None
    admins = []
    for admin in Admin.query.filter_by(enabled=True).all():
        role = str(admin.role or '').lower()
        is_global = bool(admin.is_superadmin or role in ('admin', 'superadmin'))
        is_owner = bool(owner_id and admin.id == owner_id)
        if is_global or is_owner:
            admins.append(admin)
    return admins


def _identity_display_phone(identity: TelegramIdentity | None,
                            customer: CustomerAccount | None = None) -> str:
    canonical = normalize_iran_mobile(
        (identity.phone_normalized if identity else None)
        or (customer.primary_phone if customer else None),
    )
    if canonical and canonical.startswith('98') and len(canonical) == 12:
        return f'0{canonical[2:]}'
    return ''


def _buyer_contact_line(request_row: TelegramPurchaseRequest) -> str:
    """Admin-facing buyer identity: @username (t.me link), numeric id, phone."""
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=request_row.telegram_user_id,
        customer_id=request_row.customer_id,
    ).first()
    if identity is None:
        identity = TelegramIdentity.query.filter_by(
            telegram_user_id=request_row.telegram_user_id,
        ).first()
    username = re.sub(
        r'[^A-Za-z0-9_]+', '', str((identity.username if identity else '') or '').lstrip('@'),
    )
    parts = []
    if username:
        parts.append(f"@{username} (https://t.me/{username})")
    parts.append(str(request_row.telegram_user_id))
    phone = _identity_display_phone(identity)
    if phone:
        parts.append(phone)
    return ' · '.join(parts)


def _notify_purchase_admins(api: TelegramBotApi, request_row: TelegramPurchaseRequest):
    payment_method = str(getattr(request_row, 'payment_method', '') or 'card')
    lines = [
        f"Telegram purchase request #{request_row.id}",
        f"Server: {html.escape(str(request_row.server.name))}",
        f"Package: {html.escape(str(request_row.package.name))}",
        f"Amount: {int(request_row.amount or 0):,} T",
        f"Payment: {'💰 Wallet' if payment_method == 'wallet' else '💳 Card'}",
        f"Telegram user: {_buyer_contact_line(request_row)}",
    ]
    if request_row.bank_card:
        lines.append(f"Card:\n{_format_bank_card(request_row.bank_card)}")
    if getattr(request_row, 'duplicate_receipt', False):
        lines.append('⚠️ FRAUD WARNING: this receipt file was already used on another purchase request!')
    if getattr(request_row, 'discount_amount', None):
        lines.append(
            f"Original: {int(request_row.original_amount or 0):,} T · "
            f"Discount: {int(request_row.discount_amount or 0):,} T"
            + (f" · Promo: {html.escape(str(request_row.promo_code))}" if request_row.promo_code else ''))
    if request_row.detail:
        lines.append(f"Account name: {html.escape(str(request_row.detail.account_name))}")
    approve_label = (
        '🔄 Retry provisioning' if request_row.status == 'approved' else '✅ Approve payment & create service'
    )
    keyboard = {'inline_keyboard': [
        [
            {'text': approve_label, 'callback_data': f'admin-purchase:{request_row.id}:approve'},
            {'text': '❌ Reject', 'callback_data': f'admin-purchase:{request_row.id}:reject'},
        ],
        [{'text': '✏️ Edit card', 'callback_data': f'admin-edit-card:purchase:{request_row.id}'}],
    ]}
    has_receipt_media = bool(request_row.receipt_file_id) and not str(
        request_row.receipt_file_id).startswith(('wallet:', 'trial:'))
    for admin in _purchase_admins(request_row):
        try:
            admin_chat_id = int(str(admin.telegram_id or '').strip())
            if admin_chat_id <= 0:
                continue
        except (TypeError, ValueError):
            continue
        admin_lines = list(lines)
        if has_receipt_media:
            try:
                if request_row.receipt_kind == 'document':
                    api.send_document(admin_chat_id, request_row.receipt_file_id)
                else:
                    api.send_photo(admin_chat_id, request_row.receipt_file_id)
            except TelegramApiError:
                admin_lines.append('Receipt media delivery failed; open the order from the panel.')
        try:
            api.send_message(
                admin_chat_id, '\n'.join(admin_lines),
                parse_mode='HTML', reply_markup=keyboard)
        except TelegramApiError:
            continue


def _provisioning_reviewer(bot: TelegramBotInstance):
    """Admin identity used for free trial/emergency provisioning calls."""
    owner = db.session.get(Admin, bot.owner_admin_id) if bot and bot.owner_admin_id else None
    if owner and owner.enabled and _reseller_can_create_free(owner):
        return owner
    for admin in Admin.query.filter_by(enabled=True).all():
        if admin.is_superadmin or str(admin.role or '').lower() == 'superadmin':
            return admin
    return None


def _trial_grant_for(bot: TelegramBotInstance, telegram_user_id: int, phone_normalized: str):
    query = TelegramTrialGrant.query.filter_by(bot_instance_id=bot.id, kind='trial')
    grants = query.all()
    phone = str(phone_normalized or '')
    for grant in grants:
        if int(grant.telegram_user_id or 0) == int(telegram_user_id):
            return grant
        if phone and grant.phone_normalized == phone:
            return grant
    return None


def _notify_grant_admins(api: TelegramBotApi, request_row: TelegramPurchaseRequest, kind: str):
    lines = [
        f"Telegram {kind} grant on purchase #{request_row.id}",
        f"Server: {request_row.server.name if request_row.server else request_row.server_id}",
        f"Package: {request_row.package.name if request_row.package else request_row.package_id}",
        f"Telegram user: {request_row.telegram_user_id}",
    ]
    for admin in _purchase_admins(request_row):
        try:
            admin_chat_id = int(str(admin.telegram_id or '').strip())
            if admin_chat_id <= 0:
                continue
        except (TypeError, ValueError):
            continue
        try:
            api.send_message(admin_chat_id, '\n'.join(lines))
        except TelegramApiError:
            continue


def _grant_delivery_link(result: dict) -> str:
    client = result.get('client') if isinstance(result, dict) else None
    client = client if isinstance(client, dict) else {}
    return str(
        client.get('dashboard_link') or client.get('dash_sub_url')
        or client.get('sub_link') or client.get('link') or ''
    ).strip()


def _start_trial(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int,
                 user_id: int, state: TelegramBotUserState):
    language = state.language if state.language in COPY else 'fa'
    policy = _purchase_policy_values(bot)
    package = None
    if policy['trial_package_id']:
        package = db.session.get(Package, int(policy['trial_package_id']))
    if (not policy['trial_enabled'] or not package or not package.enabled
            or not getattr(package, 'is_trial', False)):
        api.send_message(chat_id, _cc(language)['trial_unavailable'])
        return
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not identity or not identity.customer_id or not identity.phone_verified_at:
        _send_contact_prompt(api, chat_id, language)
        return
    if policy.get('trial_requires_channel_membership') and policy.get('trial_channels'):
        missing = [
            channel for channel in policy['trial_channels']
            if not _channel_member(api, int(channel['chat_id']), int(user_id))
        ]
        if missing:
            copy = _cc(language)
            titles = [channel.get('title') or str(channel['chat_id']) for channel in missing]
            keyboard = []
            for channel in missing:
                invite_url = channel.get('invite_url') or ''
                if invite_url:
                    keyboard.append([{
                        'text': channel.get('title') or str(channel['chat_id']),
                        'url': invite_url,
                    }])
            keyboard.append([{'text': copy['trial_channel_recheck'], 'callback_data': 'trial_recheck'}])
            api.send_message(
                chat_id,
                copy['trial_channel_required'].format(channels='\n'.join(f'• {t}' for t in titles)),
                reply_markup={'inline_keyboard': keyboard},
            )
            return
    phone = str(identity.phone_normalized or '')
    if _trial_grant_for(bot, user_id, phone):
        api.send_message(chat_id, _cc(language)['trial_already_used'])
        return
    reviewer = _provisioning_reviewer(bot)
    if reviewer is None:
        api.send_message(chat_id, _cc(language)['trial_failed'])
        return
    server = _assign_purchase_server(bot, package.id, owner_id=_effective_owner_id(bot, user_id))
    if server is None:
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    request_row = TelegramPurchaseRequest(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        customer_id=identity.customer_id,
        server_id=server.id,
        package_id=package.id,
        amount=0,
        receipt_file_id=f'trial:{user_id}',
        receipt_kind='photo',
        source_chat_id=chat_id,
        source_message_id=0,
        status='approved',
        reviewed_by_admin_id=reviewer.id,
        reviewed_at=datetime.utcnow(),
    )
    db.session.add(request_row)
    db.session.flush()
    success, result = _execute_purchase_request(
        request_row, reviewer, free=True, verification_method='telegram_trial')
    if not success:
        request_row.status = 'rejected'
        db.session.commit()
        app.logger.warning('[telegram-trial] provisioning failed for bot %s: %s', bot.id, result)
        api.send_message(chat_id, _cc(language)['trial_failed'])
        return
    request_row.status = 'completed'
    db.session.add(TelegramTrialGrant(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        phone_normalized=phone,
        customer_id=identity.customer_id,
        package_id=package.id,
        ownership_id=(result or {}).get('ownership_id') if isinstance(result, dict) else None,
        kind='trial',
    ))
    _log_audit('telegram.trial_grant', request_row, actor='customer',
               meta={'telegram_user_id': user_id, 'phone_normalized': phone})
    db.session.commit()
    api.send_message(
        chat_id,
        _cc(language)['trial_success'].format(link=_grant_delivery_link(result)),
    )
    _notify_grant_admins(api, request_row, 'trial')


def _execute_emergency_renewal(ownership: ServiceOwnership, days: int, volume_gb: int,
                               reviewer: Admin):
    client, inbound_id = _cached_owned_service_location(ownership)
    email = str(
        (client or {}).get('email') or ownership.client_email_snapshot or ''
    ).strip()
    if not client or inbound_id is None or not email:
        return False, 'The service is not present in the latest server snapshot. Refresh the server and retry.'
    path = f"/api/client/{ownership.server_id}/{inbound_id}/{quote(email, safe='')}/renew"
    with app.test_request_context(
        path,
        method='POST',
        json={
            'mode': 'custom',
            'days': int(days),
            'volume': int(volume_gb),
            'reset_traffic': True,
            'free': True,
        },
    ):
        flask_session['admin_id'] = reviewer.id
        flask_session['admin_username'] = reviewer.username
        flask_session['role'] = reviewer.role
        flask_session['is_superadmin'] = bool(reviewer.is_superadmin)
        response = renew_client(ownership.server_id, inbound_id, email)
    status_code = 200
    if isinstance(response, tuple):
        response, status_code = response[0], int(response[1])
    payload = response.get_json(silent=True) if hasattr(response, 'get_json') else None
    payload = payload if isinstance(payload, dict) else {}
    if status_code < 400 and payload.get('success'):
        return True, payload
    return False, str(payload.get('error') or f'Renewal failed with HTTP {status_code}')


def _grant_emergency_access(api: TelegramBotApi, bot: TelegramBotInstance, callback: dict,
                            ownership_id: int):
    sender = callback.get('from') or {}
    callback_id = str(callback.get('id') or '')
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    user_id = int(sender.get('id') or 0)
    identity, ownership = _owned_service(user_id, ownership_id)
    state = _state(bot, user_id)
    language = state.language if state.language in COPY else 'fa'
    api.answer_callback(callback_id)
    if not ownership:
        api.send_message(chat_id, _cc(language)['invalid_service'])
        return
    policy = _purchase_policy_values(bot)
    if not policy['emergency_enabled']:
        api.send_message(chat_id, _cc(language)['emergency_unavailable'])
        return
    cutoff = datetime.utcnow() - timedelta(days=int(policy['emergency_cooldown_days']))
    recent = TelegramTrialGrant.query.filter(
        TelegramTrialGrant.kind == 'emergency',
        TelegramTrialGrant.ownership_id == ownership.id,
        TelegramTrialGrant.created_at >= cutoff,
    ).first()
    if recent:
        api.send_message(chat_id, _cc(language)['emergency_cooldown'])
        return
    reviewer = _provisioning_reviewer(bot)
    if reviewer is None:
        api.send_message(chat_id, _cc(language)['emergency_failed'])
        return
    renewed, result = _execute_emergency_renewal(
        ownership, policy['emergency_days'], policy['emergency_volume_gb'], reviewer)
    if not renewed:
        app.logger.warning('[telegram-emergency] renewal failed for bot %s: %s', bot.id, result)
        api.send_message(chat_id, _cc(language)['emergency_failed'])
        return
    db.session.add(TelegramTrialGrant(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        phone_normalized=str(getattr(identity, 'phone_normalized', '') or '') if identity else '',
        customer_id=ownership.customer_id,
        ownership_id=ownership.id,
        kind='emergency',
    ))
    _log_audit('telegram.emergency_grant', ('ServiceOwnership', ownership.id), actor='customer',
               meta={'telegram_user_id': user_id})
    db.session.commit()
    api.send_message(
        chat_id,
        _cc(language)['emergency_success'].format(
            days=policy['emergency_days'], volume=policy['emergency_volume_gb']),
    )
    _send_service_details(api, bot, chat_id, language, ownership)


def _cached_purchase_client(request_row: TelegramPurchaseRequest):
    email = str(getattr(request_row.detail, 'account_name', '') or '').strip().lower()
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(inbound.get('server_id') or 0) != int(request_row.server_id):
                continue
        except (TypeError, ValueError):
            continue
        for client in (inbound.get('clients') or []):
            if str(client.get('email') or '').strip().lower() == email:
                return client
    return None


def _ensure_purchase_detail(request_row: TelegramPurchaseRequest):
    if request_row.detail:
        return request_row.detail
    bot = db.session.get(TelegramBotInstance, request_row.bot_instance_id)
    customer = db.session.get(CustomerAccount, request_row.customer_id)
    if not bot or not customer:
        return None
    detail = TelegramPurchaseRequestDetail(
        request_id=request_row.id,
        account_name=_render_purchase_account_name(bot, request_row, customer, None),
        allocation_strategy=_purchase_policy_values(bot)['assignment_strategy'],
    )
    request_row.detail = detail
    db.session.add(detail)
    db.session.flush()
    return detail


def _ensure_purchase_ownership(request_row: TelegramPurchaseRequest, reviewer: Admin, client: dict,
                               *, allow_create: bool,
                               verification_method: str = 'telegram_purchase'):
    raw = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
    client_uuid = str(client.get('id') or client.get('uuid') or raw.get('id') or '').strip()
    if not client_uuid:
        raise ValueError('Created client UUID is missing from the server snapshot.')
    ownership = ServiceOwnership.query.filter_by(
        server_id=request_row.server_id, client_uuid=client_uuid,
    ).first()
    if ownership is None:
        if not allow_create:
            raise ValueError(
                'An account with this name already exists but is not linked to this order. '
                'Resolve ownership manually; Eve will not claim it automatically.'
            )
        bot = db.session.get(TelegramBotInstance, request_row.bot_instance_id)
        owner_id = _effective_owner_id(bot, request_row.telegram_user_id) if bot else None
        owner = db.session.get(Admin, owner_id) if owner_id else None
        reseller_id = owner.id if owner and str(owner.role or '').lower() == 'reseller' else None
        ownership = ServiceOwnership(
            customer_id=request_row.customer_id,
            server_id=request_row.server_id,
            client_uuid=client_uuid,
            client_email_snapshot=request_row.detail.account_name,
            reseller_id=reseller_id,
            verification_method=verification_method,
            verified_by_admin_id=reviewer.id,
            verified_at=datetime.utcnow(),
        )
        db.session.add(ownership)
    elif ownership.customer_id != request_row.customer_id:
        raise ValueError('The generated account name already belongs to another customer.')
    return ownership


def _ensure_purchase_inbound_allocation(request_row: TelegramPurchaseRequest):
    """Resolve once and persist so retries can never drift to another inbound pack."""
    if request_row.inbound_allocation:
        inbound_ids = request_row.inbound_allocation.inbound_ids()
        if inbound_ids:
            return inbound_ids, None
        return None, 'The saved inbound allocation for this order is invalid.'
    route = TelegramPurchaseInboundRoute.query.filter_by(
        bot_instance_id=request_row.bot_instance_id,
        package_id=request_row.package_id,
        server_id=request_row.server_id,
        enabled=True,
    ).first()
    if route is None:
        return None, (
            'No inbound route is configured for this package and server. '
            'Configure Telegram Purchase Flow > Inbound routing, then retry.'
        )
    server = db.session.get(Server, request_row.server_id)
    if not server:
        return None, 'The selected server no longer exists.'
    valid_ids = {row['id'] for row in _telegram_customer_inbounds(server.id)}
    panel_session, panel_error = get_xui_session(server)
    if not panel_session or panel_error:
        return None, panel_error or 'Could not connect to the selected server.'
    is_v3 = bool(server_is_v3(server, panel_session))
    signature = None
    if route.mode == 'auto_detect':
        if not is_v3:
            return None, 'Auto Detect requires 3x-ui v3 or newer; select one manual inbound for this legacy server.'
        try:
            profiles = _detect_telegram_inbound_profiles(server)
        except ValueError as exc:
            return None, str(exc)
        selected = random.choices(
            profiles,
            weights=[max(1, int(profile['client_count'])) for profile in profiles],
            k=1,
        )[0]
        inbound_ids = [value for value in selected['inbound_ids'] if value in valid_ids]
        signature = selected.get('signature')
    else:
        inbound_ids = [value for value in route.inbound_ids() if value in valid_ids]
        if not is_v3 and len(inbound_ids) != 1:
            return None, 'Legacy 3x-ui servers require exactly one manual inbound per package.'
    if not inbound_ids:
        return None, 'The configured inbound route has no active client-capable inbound. Refresh and update the route.'
    allocation = TelegramPurchaseRequestAllocation(
        request_id=request_row.id,
        route_id=route.id,
        mode=route.mode,
        inbound_ids_json=json.dumps(sorted(set(inbound_ids)), separators=(',', ':')),
        detected_signature=signature,
    )
    request_row.inbound_allocation = allocation
    db.session.add(allocation)
    db.session.flush()
    return allocation.inbound_ids(), None


def _purchase_provisioning_inbound_ids(server_id: int):
    """Compatibility helper: list valid customer inbounds, not an allocation policy."""
    return [row['id'] for row in _telegram_customer_inbounds(server_id)]


def _purchase_account_comment(request_row: TelegramPurchaseRequest, free: bool,
                              verification_method: str) -> str:
    """Buyer identity baked into the panel comment: label | phone:x | @username."""
    is_trial = (
        verification_method == 'telegram_trial'
        or str(getattr(request_row, 'receipt_file_id', '') or '').startswith('trial:')
    )
    label = 'Telegram trial' if is_trial else f'Telegram purchase #{request_row.id}'
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=request_row.telegram_user_id,
        customer_id=request_row.customer_id,
    ).first()
    parts = [label]
    phone = _identity_display_phone(identity)
    if phone:
        parts.append(f'phone:{phone}')
    username = re.sub(
        r'[^A-Za-z0-9_]+', '', str((identity.username if identity else '') or '').lstrip('@'),
    )
    if username:
        parts.append(f'@{username}')
    return ' | '.join(parts)[:200]


def _execute_purchase_request(request_row: TelegramPurchaseRequest, reviewer: Admin,
                              free: bool = False,
                              verification_method: str = 'telegram_purchase'):
    detail = _ensure_purchase_detail(request_row)
    if not request_row.package_id or not request_row.package or not detail:
        return False, 'Purchase package or provisioning details are missing.'
    existing = _cached_purchase_client(request_row)
    if existing:
        try:
            _ensure_purchase_ownership(
                request_row, reviewer, existing, allow_create=False,
                verification_method=verification_method)
            return True, {'client': existing, 'already_created': True}
        except ValueError as exc:
            return False, str(exc)
    inbound_ids, allocation_error = _ensure_purchase_inbound_allocation(request_row)
    if not inbound_ids:
        return False, allocation_error or 'No inbound allocation is available.'
    path = f"/api/client/{request_row.server_id}/{inbound_ids[0]}/add"
    public_base_url = str(_public_base_url() or '').strip().rstrip('/') or 'http://localhost'
    with app.test_request_context(
        path,
        base_url=public_base_url,
        method='POST',
        json={
            'mode': 'package',
            'package_id': request_row.package_id,
            'email': request_row.detail.account_name,
            'comment': _purchase_account_comment(request_row, free, verification_method),
            'inbound_ids': inbound_ids,
            'free': bool(free),
        },
    ):
        flask_session['admin_id'] = reviewer.id
        flask_session['admin_username'] = reviewer.username
        flask_session['role'] = reviewer.role
        flask_session['is_superadmin'] = bool(reviewer.is_superadmin)
        response = add_client(request_row.server_id, inbound_ids[0])
    status_code = 200
    if isinstance(response, tuple):
        response, status_code = response[0], int(response[1])
    payload = response.get_json(silent=True) if hasattr(response, 'get_json') else None
    payload = payload if isinstance(payload, dict) else {}
    if status_code >= 400 or not payload.get('success'):
        return False, str(payload.get('error') or f'Provisioning failed with HTTP {status_code}')
    client = _cached_purchase_client(request_row)
    if not client:
        return False, 'The panel created the client but the local snapshot was not updated. Refresh and retry.'
    try:
        ownership = _ensure_purchase_ownership(
            request_row, reviewer, client, allow_create=True,
            verification_method=verification_method)
    except ValueError as exc:
        return False, str(exc)
    payload['ownership_id'] = ownership.id
    return True, payload


def _service_action_reviewer(bot: TelegramBotInstance):
    """Admin identity used for customer-initiated panel actions (link rotate)."""
    owner = db.session.get(Admin, bot.owner_admin_id) if bot and bot.owner_admin_id else None
    if owner and owner.enabled:
        return owner
    for admin in Admin.query.filter_by(enabled=True).all():
        if admin.is_superadmin or str(admin.role or '').lower() == 'superadmin':
            return admin
    return None


def _service_remaining_days(client: dict | None):
    if not client:
        return None
    raw = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
    try:
        expiry_ts = int(client.get('expiryTimestamp') or raw.get('expiryTime') or 0)
    except (TypeError, ValueError):
        return None
    if expiry_ts <= 0:
        return None
    return max(0, int((expiry_ts / 1000 - time.time()) // 86400))


def _service_rotate_capacity(client: dict | None, language: str):
    """(days_text, gb_text) for rotate prompts/results; 'unlimited' when unset."""
    days = _service_remaining_days(client)
    days_text = _cc(language)['unlimited'] if days is None else str(days)
    remaining = client.get('remaining_bytes') if client else None
    if remaining in (None, -1):
        gb_text = _cc(language)['unlimited']
    else:
        gb_text = f"{max(0, int(remaining or 0)) / (1024 ** 3):.2f}"
    return days_text, gb_text


def _execute_service_rotate(ownership: ServiceOwnership, reviewer: Admin):
    """Call the panel rotate route in-process, mirroring _execute_purchase_request."""
    email = str(ownership.client_email_snapshot or '').strip()
    if not email:
        return False, 'The service account name is missing from the local snapshot.'
    path = f"/api/client/{ownership.server_id}/rotate"
    public_base_url = str(_public_base_url() or '').strip().rstrip('/') or 'http://localhost'
    with app.test_request_context(
        path,
        base_url=public_base_url,
        method='POST',
        json={'client_email': email},
    ):
        flask_session['admin_id'] = reviewer.id
        flask_session['admin_username'] = reviewer.username
        flask_session['role'] = reviewer.role
        flask_session['is_superadmin'] = bool(reviewer.is_superadmin)
        response = rotate_client(ownership.server_id)
    status_code = 200
    if isinstance(response, tuple):
        response, status_code = response[0], int(response[1])
    payload = response.get_json(silent=True) if hasattr(response, 'get_json') else None
    payload = payload if isinstance(payload, dict) else {}
    if status_code >= 400 or not payload.get('success'):
        return False, str(payload.get('error') or f'Rotate failed with HTTP {status_code}')
    return True, payload


def _handle_service_rotate_callback(api: TelegramBotApi, bot: TelegramBotInstance,
                                    callback: dict, data: str) -> bool:
    parts = data.split(':')
    if parts[0] != 'service-rotate':
        return False
    confirm = len(parts) == 3 and parts[1] == 'confirm'
    if len(parts) != 2 and not confirm:
        return False
    callback_id = str(callback.get('id') or '')
    sender = callback.get('from') or {}
    user_id = int(sender.get('id') or 0)
    chat_id = int((((callback.get('message') or {}).get('chat') or {}).get('id')) or 0)
    try:
        ownership_id = int(parts[-1])
    except (TypeError, ValueError):
        api.answer_callback(callback_id)
        return True
    state = _state(bot, user_id)
    language = state.language if state.language in COPY else bot.default_language
    _identity_row, ownership = _owned_service(user_id, ownership_id)
    if not ownership:
        api.answer_callback(callback_id)
        api.send_message(chat_id, _cc(language)['invalid_service'])
        return True
    if not confirm:
        client = _cached_owned_service(ownership)
        days_text, gb_text = _service_rotate_capacity(client, language)
        api.answer_callback(callback_id)
        api.send_message(
            chat_id,
            _cc(language)['service_rotate_confirm'].format(days=days_text, gb=gb_text),
            reply_markup={'inline_keyboard': [[
                {'text': _cc(language)['service_rotate_yes'],
                 'callback_data': f'service-rotate:confirm:{ownership.id}'},
                {'text': _cc(language)['service_rotate_no'],
                 'callback_data': f'service:{ownership.id}'},
            ]]},
        )
        return True
    if not _rate_ok(user_id, 'service_rotate', 3, 86400):
        api.answer_callback(callback_id, _cc(language)['service_rotate_limited'])
        return True
    reviewer = _service_action_reviewer(bot)
    if reviewer is None:
        api.answer_callback(callback_id)
        api.send_message(chat_id, _cc(language)['service_rotate_failed'].format(
            error='no reviewer available'))
        return True
    api.answer_callback(callback_id)
    success, result = _execute_service_rotate(ownership, reviewer)
    if not success:
        api.send_message(chat_id, _cc(language)['service_rotate_failed'].format(error=result))
        return True
    link = str(result.get('dash_sub_url') or result.get('sub_url') or '').strip()
    days = result.get('remaining_days')
    gb = result.get('remaining_gb')
    days_text = _cc(language)['unlimited'] if days is None else str(days)
    gb_text = _cc(language)['unlimited'] if gb is None else str(gb)
    text = _cc(language)['service_rotate_done'].format(
        days=days_text, gb=gb_text, link=link)
    if link:
        _send_link_with_qr(api, chat_id, link, caption=text)
    else:
        api.send_message(chat_id, text)
    return True


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
    refunded_amount = 0
    if parts[2] == 'reject':
        if request_row.status != 'pending':
            api.answer_callback(callback_id, 'Already reviewed')
            return True
        request_row.status = 'rejected'
        request_row.reviewed_by_admin_id = reviewer.id
        request_row.reviewed_at = datetime.utcnow()
        if str(getattr(request_row, 'payment_method', '') or 'card') == 'wallet':
            wallet_customer = db.session.get(CustomerAccount, request_row.customer_id)
            refunded_amount = int(request_row.amount or 0)
            if wallet_customer and refunded_amount > 0:
                wallet_customer.credit = int(wallet_customer.credit or 0) + refunded_amount
                db.session.add(CustomerTransaction(
                    customer_id=wallet_customer.id,
                    type='refund',
                    amount=refunded_amount,
                    request_ref=f'purchase:{request_row.id}',
                ))
            else:
                refunded_amount = 0
        _log_audit('telegram_purchase.reject', request_row, actor=reviewer)
        db.session.flush()
        api.answer_callback(callback_id, 'Rejected')
    else:
        if request_row.status not in ('pending', 'approved'):
            api.answer_callback(callback_id, 'Already completed')
            return True
        if request_row.status == 'pending':
            request_row.status = 'approved'
            request_row.reviewed_by_admin_id = reviewer.id
            request_row.reviewed_at = datetime.utcnow()
            db.session.flush()
        provisioned, provision_result = _execute_purchase_request(request_row, reviewer)
        if provisioned:
            request_row.status = 'completed'
            _log_audit('telegram_purchase.approve', request_row, actor=reviewer)
            db.session.flush()
            api.answer_callback(callback_id, 'Service created')
        else:
            api.answer_callback(callback_id, 'Provisioning failed')
            if chat_id:
                api.send_message(
                    chat_id,
                    f"Purchase #{request_row.id} payment is approved, but provisioning failed.\n"
                    f"Error: {provision_result}",
                    reply_markup={'inline_keyboard': [[{
                        'text': '🔄 Retry provisioning',
                        'callback_data': f'admin-purchase:{request_row.id}:approve',
                    }]]},
                )
    if chat_id and request_row.status in ('completed', 'rejected'):
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
        if request_row.status == 'completed':
            result = provision_result if isinstance(provision_result, dict) else {}
            client = result.get('client') if isinstance(result.get('client'), dict) else {}
            delivery_link = str(
                client.get('dashboard_link') or client.get('dash_sub_url')
                or client.get('sub_link') or ''
            ).strip()
            if delivery_link:
                _send_link_with_qr(api, identity.telegram_chat_id, delivery_link)
            api.send_message(
                identity.telegram_chat_id,
                _cc(language)['purchase_completed'].format(
                    account_name=request_row.detail.account_name,
                    delivery_link=delivery_link,
                ).rstrip(),
                reply_markup={'inline_keyboard': [[{
                    'text': _cc(language)['menu_orders'],
                    'callback_data': f'purchase-order:{request_row.id}',
                }]]},
            )
        else:
            key = 'purchase_approved' if request_row.status == 'approved' else 'purchase_rejected'
            text = _cc(language)[key]
            if request_row.status == 'rejected' and refunded_amount:
                text = _cc(language)['purchase_rejected_refund'].format(
                    amount=f"{refunded_amount:,}",
                    balance=f"{int(customer.credit or 0):,}",
                )
            api.send_message(
                identity.telegram_chat_id, text,
                reply_markup={'inline_keyboard': [[{
                    'text': _cc(language)['menu_orders'],
                    'callback_data': f'purchase-order:{request_row.id}',
                }]]},
            )
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
            api.send_message(item.claim.telegram_identity.telegram_chat_id, _cc(language)[key])
    except (ValueError, PermissionError) as exc:
        if callback_id:
            api.answer_callback(callback_id, str(exc)[:120])
    return True


def _record_referral(referrer_id: int, referee_id: int) -> bool:
    """Record a /start ref_<id> referral. No self-referrals; one referrer per user."""
    try:
        referrer_id = int(referrer_id)
        referee_id = int(referee_id)
    except (TypeError, ValueError):
        return False
    if referrer_id <= 0 or referrer_id == referee_id:
        return False
    if TelegramReferral.query.filter_by(referee_telegram_user_id=referee_id).first():
        return False
    db.session.add(TelegramReferral(
        referrer_telegram_user_id=referrer_id,
        referee_telegram_user_id=referee_id,
    ))
    db.session.flush()
    return True


def _qualify_referral(referee_id: int) -> None:
    referral = TelegramReferral.query.filter_by(
        referee_telegram_user_id=int(referee_id)).first()
    if referral and referral.qualified_at is None:
        referral.qualified_at = datetime.utcnow()
        db.session.flush()


def _handle_start(api: TelegramBotApi, bot: TelegramBotInstance, chat_id: int,
                  user_id: int, state: TelegramBotUserState, payload: str = ''):
    payload = str(payload or '').strip()
    if payload.startswith('ref_'):
        try:
            _record_referral(int(payload[4:]), user_id)
            db.session.flush()
        except (TypeError, ValueError):
            pass
    languages = bot.enabled_languages()
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if identity and identity.customer_id and identity.phone_verified_at:
        customer = db.session.get(CustomerAccount, identity.customer_id)
        preferred = str(getattr(customer, 'preferred_language', '') or '')
        if preferred in languages:
            state.language = preferred
        state.step = "verified"
        db.session.flush()
        _send_main_menu(api, bot, chat_id, state.language)
        return
    if len(languages) > 1:
        state.step = "choose_language"
        db.session.flush()
        api.send_message(
            chat_id, _cc(state.language)["choose_language"],
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
    if data.startswith(('announcement-send:', 'announcement-cancel:')):
        try:
            action, raw_id = data.split(':', 1)
            row = db.session.get(TelegramAnnouncement, int(raw_id))
            reviewer = _telegram_admin(user_id)
            allowed = row and reviewer and (reviewer.is_superadmin or reviewer.role == 'superadmin'
                or (reviewer.id == bot.owner_admin_id and row.source_bot_instance_id == bot.id))
            if not allowed:
                api.answer_callback(callback_id, text='دسترسی ندارید.', show_alert=True)
                return
            if action == 'announcement-send':
                _queue_telegram_announcement(row)
                db.session.commit()
                api.answer_callback(callback_id, text=f'در صف ارسال قرار گرفت: {row.total_count} گیرنده')
                api.send_message(chat_id, f'✅ اطلاع‌رسانی برای {row.total_count} گیرنده در صف قرار گرفت.')
            else:
                row.status = 'cancelled'
                db.session.commit()
                api.answer_callback(callback_id, text='لغو شد')
        except (TypeError, ValueError) as exc:
            db.session.rollback()
            api.answer_callback(callback_id, text=str(exc)[:180], show_alert=True)
        return
    if data.startswith('admin-claim:'):
        _handle_admin_claim_callback(api, callback, data)
        return
    if data.startswith('admin-support:'):
        _handle_admin_support_callback(api, bot, callback, data)
        return
    if data.startswith('admin-service:'):
        _handle_admin_service_callback(api, callback, data)
        return
    if data.startswith('renew-request:'):
        _handle_renewal_request_callback(api, callback, data)
        return
    if data.startswith('admin-purchase:'):
        _handle_admin_purchase_callback(api, callback, data)
        return
    if data.startswith('admin-topup:'):
        _handle_admin_topup_callback(api, callback, data)
        return
    if data.startswith('admin-edit-card:'):
        _handle_admin_edit_card_callback(api, callback, data)
        return
    if data.startswith('admin-set-card:'):
        _handle_admin_set_card_callback(api, callback, data)
        return
    if not _is_allowed(bot, user_id):
        api.answer_callback(callback_id)
        return
    state = _state(bot, user_id)
    language = state.language if state.language in COPY else bot.default_language
    if data == 'noop':
        api.answer_callback(callback_id)
        return
    if data == 'trial_recheck':
        api.answer_callback(callback_id)
        # Drop cached membership entries for this user so a fresh join is seen at once.
        for key in [key for key in _CHANNEL_MEMBER_CACHE if key[1] == int(user_id)]:
            _CHANNEL_MEMBER_CACHE.pop(key, None)
        _start_trial(api, bot, chat_id, user_id, state)
        return
    if data == 'tutorial-devices':
        api.answer_callback(callback_id)
        _send_tutorial_devices(api, chat_id, language)
        return
    if data.startswith('tutorial-os:'):
        os_type = data.partition(':')[2]
        api.answer_callback(callback_id)
        _send_tutorial_apps(api, chat_id, language, os_type)
        return
    if data.startswith('tutorial-app:'):
        try:
            app_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        api.answer_callback(callback_id)
        _send_tutorial_app(api, chat_id, language, app_id)
        return
    if data == 'service-list':
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        api.answer_callback(callback_id)
        _send_owned_services(api, chat_id, language, identity)
        return
    if data == 'purchase-list':
        api.answer_callback(callback_id)
        _send_purchase_orders(api, bot, chat_id, user_id, language)
        return
    if data == 'promo-code':
        if not _rate_ok(user_id, 'promo-code', 10, 60):
            api.answer_callback(callback_id)
            return
        session_row = _purchase_session(bot.id, user_id)
        session_row.action = 'awaiting_promo_code'
        state.step = 'awaiting_promo_code'
        db.session.flush()
        api.answer_callback(callback_id)
        api.send_message(chat_id, _cc(language)['promo_code_prompt'])
        return
    if data == 'purchase-start':
        if not _rate_ok(user_id, 'purchase-start', 10, 60):
            api.answer_callback(callback_id)
            return
        state.step = 'verified'
        api.answer_callback(callback_id)
        if _purchase_policy_values(bot)['customer_selects_server']:
            _send_purchase_servers(api, bot, chat_id, language, user_id=user_id)
        else:
            _send_purchase_packages(api, bot, chat_id, language, None, user_id=user_id)
        return
    if data == 'purchase-pay-card':
        session_row = _purchase_session(bot.id, user_id)
        if str(session_row.action or '') not in ('awaiting_payment_method', 'awaiting_receipt'):
            api.answer_callback(callback_id)
            api.send_message(chat_id, _cc(language)['start_first'])
            return
        api.answer_callback(callback_id)
        _send_purchase_card_payment(api, bot, chat_id, user_id, language, state)
        return
    if data == 'purchase-pay-wallet':
        session_row = _purchase_session(bot.id, user_id)
        if str(session_row.action or '') != 'awaiting_payment_method':
            api.answer_callback(callback_id)
            api.send_message(chat_id, _cc(language)['start_first'])
            return
        api.answer_callback(callback_id)
        _create_wallet_purchase_request(api, bot, chat_id, user_id, language, state)
        return
    if data.startswith('purchase-order:'):
        try:
            request_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        request_row = _customer_purchase_order(bot.id, user_id, request_id)
        api.answer_callback(callback_id)
        if request_row:
            _send_purchase_order(api, chat_id, language, request_row)
        else:
            api.send_message(chat_id, _cc(language)['purchase_order_missing'])
        return
    if data == 'support-list':
        api.answer_callback(callback_id)
        _send_support_requests(api, bot, chat_id, user_id, language)
        return
    if data.startswith('support-ticket:'):
        try:
            request_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        request_row = _customer_support_request(user_id, request_id)
        api.answer_callback(callback_id)
        if request_row:
            _send_support_ticket(api, chat_id, user_id, language, request_row)
        else:
            api.send_message(chat_id, _cc(language)['support_ticket_missing'])
        return
    if data.startswith('support-reply:'):
        try:
            request_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        request_row = _customer_support_request(user_id, request_id)
        if not request_row or request_row.status != 'pending':
            api.answer_callback(callback_id)
            api.send_message(chat_id, _cc(language)['support_ticket_missing'])
            return
        session_row = _service_session(bot.id, user_id)
        session_row.service_ownership_id = request_row.service_ownership_id
        session_row.action = f'support_request:{request_row.id}'
        state.step = 'awaiting_support_message'
        db.session.flush()
        api.answer_callback(callback_id)
        api.send_message(chat_id, _cc(language)['support_prompt'])
        return
    if data.startswith('buy-server:'):
        try:
            server_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        rules = _purchase_server_rules(bot)
        owner_id = _effective_owner_id(bot, user_id)
        server = next((
            row for row in _eligible_purchase_servers(bot, owner_id=owner_id)
            if row.id == server_id and rules.get(row.id) and rules[row.id].customer_visible
        ), None)
        api.answer_callback(callback_id)
        if server is None:
            api.send_message(chat_id, _cc(language)['payment_unavailable'])
            return
        _send_purchase_packages(api, bot, chat_id, language, server, user_id=user_id)
        return
    if data.startswith('buy-package:'):
        if not _rate_ok(user_id, 'buy-package', 15, 60):
            api.answer_callback(callback_id)
            return
        parts = data.split(':')
        try:
            server_id = int(parts[1])
            package_id = int(parts[2])
        except (IndexError, TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        policy_values = _purchase_policy_values(bot)
        owner_id = _effective_owner_id(bot, user_id)
        package = next((row for row in _purchase_packages(bot, owner_id=owner_id) if row.id == package_id), None)
        if server_id == 0 and not policy_values['customer_selects_server']:
            server = _assign_purchase_server(bot, package_id, owner_id=owner_id) if package else None
        elif server_id > 0 and policy_values['customer_selects_server']:
            rules = _purchase_server_rules(bot)
            server = next((
                row for row in _eligible_purchase_servers(bot, package_id, owner_id=owner_id)
                if row.id == server_id and rules.get(row.id) and rules[row.id].customer_visible
            ), None)
        else:
            server = None
        api.answer_callback(callback_id)
        if server is None or package is None:
            api.send_message(chat_id, _cc(language)['payment_unavailable'])
            return
        _continue_purchase_selection(
            api, bot, chat_id, user_id, language, server, package, state,
        )
        return
    if data.startswith('service-emergency:'):
        try:
            ownership_id = int(data.partition(':')[2])
        except (TypeError, ValueError):
            api.answer_callback(callback_id)
            return
        _grant_emergency_access(api, bot, callback, ownership_id)
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
            api.send_message(chat_id, _cc(language)['invalid_service'])
            return
        api.answer_callback(callback_id)
        _send_service_details(api, bot, chat_id, language, ownership)
        return
    if data.startswith('service-rotate:'):
        _handle_service_rotate_callback(api, bot, callback, data)
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
            api.send_message(chat_id, _cc(language)['invalid_service'])
            return
        client = _cached_owned_service(ownership)
        raw = client.get('raw_client') if client and isinstance(client.get('raw_client'), dict) else {}
        sub_id = str((raw.get('subId') or client.get('subId')) if client else '').strip()
        base_url = _public_base_url().rstrip('/')
        api.answer_callback(callback_id)
        if sub_id and base_url:
            safe_sub_id = quote(sub_id, safe='')
            link = f"{base_url}/s/{ownership.server_id}/{safe_sub_id}"
            _send_link_with_qr(
                api, chat_id, link,
                caption=f"{_cc(language)['get_link_button']}:\n{link}",
            )
        else:
            request_row, _duplicate = _create_service_request(
                bot.id, user_id, ownership, 'support', package=None,
                note='Connection link unavailable in Telegram worker snapshot',
            )
            api.send_message(chat_id, _cc(language)['link_unavailable'])
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
            api.send_message(chat_id, _cc(language)['invalid_service'])
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
            api.send_message(chat_id, _cc(language)['invalid_service'])
            return
        api.answer_callback(callback_id)
        _send_renewal_payment_choice(
            api, bot, chat_id, user_id, language, ownership, package, state,
        )
        return
    if data.startswith('renew-pay-card:'):
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
            api.send_message(chat_id, _cc(language)['invalid_service'])
            return
        api.answer_callback(callback_id)
        _begin_renewal_card_payment(
            api, bot, chat_id, user_id, language, ownership, package, state,
        )
        return
    if data.startswith('renew-pay-wallet:'):
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
            api.send_message(chat_id, _cc(language)['invalid_service'])
            return
        api.answer_callback(callback_id)
        _create_renewal_wallet_request(api, bot, chat_id, user_id, language, ownership, package)
        return
    if data == 'wallet-topup-start':
        if not _rate_ok(user_id, 'wallet-topup-start', 10, 60):
            api.answer_callback(callback_id)
            return
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        if not identity or not identity.customer_id or not identity.phone_verified_at:
            api.answer_callback(callback_id)
            _send_contact_prompt(api, chat_id, language)
            return
        session_row = _service_session(bot.id, user_id)
        session_row.service_ownership_id = None
        session_row.action = 'topup_amount'
        state.step = 'awaiting_topup_amount'
        db.session.flush()
        api.answer_callback(callback_id)
        api.send_message(chat_id, _cc(language)['wallet_topup_enter_amount'])
        return
    if data == 'wallet-history':
        api.answer_callback(callback_id)
        _send_wallet_history(api, chat_id, user_id, language)
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
            api.send_message(chat_id, _cc(language)['invalid_service'])
            return
        session_row = _service_session(bot.id, user_id)
        session_row.service_ownership_id = ownership.id
        session_row.action = 'support'
        state.step = 'awaiting_support_message'
        db.session.flush()
        api.answer_callback(callback_id)
        api.send_message(chat_id, _cc(language)['support_prompt'])
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
                _send_main_menu(api, bot, chat_id, language)
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
        api.send_message(chat_id, _cc(state.language)["send_subscription"])
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
        api.send_message(chat_id, _cc(state.language)["admin_review"])
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
        api.send_message(chat_id, _cc(language)["phone_mismatch"])
        return
    phone = normalize_iran_mobile(contact.get("phone_number"))
    if not phone:
        api.send_message(chat_id, _cc(language)["phone_invalid"])
        return

    identity = _identity(sender, chat_id)
    if identity.customer_id:
        current = db.session.get(CustomerAccount, identity.customer_id)
        if current and current.primary_phone and current.primary_phone != phone:
            state.step = "needs_review"
            db.session.flush()
            api.send_message(chat_id, _cc(language)["phone_conflict"])
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
            api.send_message(chat_id, _cc(language)["phone_conflict"])
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
    _qualify_referral(user_id)
    state.step = "verified"
    db.session.flush()
    api.send_message(
        chat_id, _cc(language)["verified"],
        reply_markup={"remove_keyboard": True},
    )
    _send_main_menu(api, bot, chat_id, language)
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
        api.send_message(chat_id, _cc(language)["start_first"])
        return
    if session_row.locked_until and session_row.locked_until > datetime.utcnow():
        api.send_message(chat_id, _cc(language)["proof_limited"])
        return
    token = _extract_subscription_token(text)
    if not token:
        api.send_message(chat_id, _cc(language)["invalid_subscription"])
        return
    result = verify_ownership_claim_subscription(item, identity.customer_id, token)
    if result.get("status") == "invalid_subscription":
        session_row.failed_attempts = int(session_row.failed_attempts or 0) + 1
        if session_row.failed_attempts >= 5:
            session_row.locked_until = datetime.utcnow() + timedelta(minutes=15)
        db.session.flush()
        api.send_message(chat_id, _cc(language)["invalid_subscription"])
        return
    if result.get("status") == "conflict":
        state.step = "needs_review"
        session_row.selected_item_id = None
        db.session.flush()
        api.send_message(chat_id, _cc(language)["claim_conflict"])
        _notify_claim_admins(api, item.claim, user_id)
        return
    state.step = "verified"
    session_row.selected_item_id = None
    session_row.failed_attempts = 0
    session_row.locked_until = None
    db.session.flush()
    api.send_message(chat_id, _cc(language)["service_attached"])
    _send_main_menu(api, bot, chat_id, language)
    _send_claim_candidates(api, chat_id, language, item.claim)


def _handle_support_message(api: TelegramBotApi, bot: TelegramBotInstance, message: dict,
                            sender: dict, state: TelegramBotUserState, text: str):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    if not _rate_ok(user_id, 'support', 10, 60):
        return
    language = state.language if state.language in COPY else bot.default_language
    session_row = TelegramServiceSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id,
    ).first()
    attachment = _support_attachment_from_message(message)
    note = str(text or message.get('caption') or '').strip()[:4000]
    if (not session_row or not session_row.service_ownership_id
            or (not note and not attachment) or note.startswith('/')):
        api.send_message(chat_id, _cc(language)['support_prompt'])
        return
    _identity_row, ownership = _owned_service(user_id, session_row.service_ownership_id)
    if not ownership:
        state.step = 'verified'
        session_row.action = None
        session_row.service_ownership_id = None
        db.session.flush()
        api.send_message(chat_id, _cc(language)['invalid_service'])
        return
    requested_id = None
    if str(session_row.action or '').startswith('support_request:'):
        try:
            requested_id = int(str(session_row.action).partition(':')[2])
        except (TypeError, ValueError):
            requested_id = None
    existing_request = _customer_support_request(user_id, requested_id) if requested_id else None
    if existing_request and existing_request.status != 'pending':
        existing_request = None
    request_row, _duplicate = _create_service_request(
        bot.id, user_id, ownership, 'support', package=None, note=note,
        attachment=attachment,
        source_chat_id=chat_id,
        source_message_id=int(message.get('message_id') or 0) or None,
    )
    state.step = 'verified'
    session_row.action = None
    session_row.service_ownership_id = None
    db.session.flush()
    api.send_message(
        chat_id,
        _cc(language)['support_pending'].format(request_id=request_row.id),
        reply_markup={"inline_keyboard": [[{
            "text": _cc(language)['support_view_button'],
            "callback_data": f"support-ticket:{request_row.id}",
        }]]},
    )
    _notify_service_request_admins(api, request_row, incoming_message=message)


def _handle_admin_support_message(api: TelegramBotApi, bot: TelegramBotInstance,
                                  message: dict, sender: dict,
                                  state: TelegramBotUserState, text: str):
    admin_chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    session_row = TelegramServiceSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id,
    ).first()
    try:
        request_id = int(str(getattr(session_row, 'action', '') or '').partition(':')[2])
    except (TypeError, ValueError):
        request_id = 0
    request_row = db.session.get(TelegramServiceRequest, request_id) if request_id else None
    reviewer = _service_request_reviewer(user_id, request_row) if request_row else None
    attachment = _support_attachment_from_message(message)
    note = str(text or message.get('caption') or '').strip()[:4000]
    if (not request_row or request_row.request_type != 'support' or request_row.status != 'pending'
            or not reviewer or not _support_reviewer_can_handle(reviewer, request_row)
            or (not note and not attachment) or note.startswith('/')):
        api.send_message(admin_chat_id, 'Send text, an image, or a supported file for the open ticket.')
        return
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=request_row.telegram_user_id,
        customer_id=request_row.customer_id,
    ).first()
    if not identity or not identity.telegram_chat_id:
        api.send_message(admin_chat_id, 'Customer Telegram chat is unavailable.')
        return
    delivered_message_id = None
    try:
        if attachment:
            result, _route_name = api.copy_message(
                identity.telegram_chat_id,
                admin_chat_id,
                int(message.get('message_id') or 0),
            )
            delivered_message_id = int((result or {}).get('message_id') or 0) or None
        else:
            heading = 'پاسخ پشتیبانی:' if state.language == 'fa' else 'Support reply:'
            result, _route_name = api.send_message(
                identity.telegram_chat_id,
                f'{heading}\n{note}',
            )
            delivered_message_id = int((result or {}).get('message_id') or 0) or None
    except TelegramApiError as exc:
        api.send_message(admin_chat_id, f'Delivery failed: {redact_connection_error(exc)}')
        return
    db.session.add(TelegramServiceRequestMessage(
        request_id=request_row.id,
        sender_type='admin',
        admin_id=reviewer.id,
        message=note,
        source_chat_id=admin_chat_id,
        source_message_id=int(message.get('message_id') or delivered_message_id or 0) or None,
        **(attachment or {}),
    ))
    if not request_row.assigned_admin_id:
        request_row.assigned_admin_id = reviewer.id
    if not request_row.first_response_at:
        request_row.first_response_at = datetime.utcnow()
    request_row.updated_at = datetime.utcnow()
    session_row.action = None
    session_row.service_ownership_id = None
    state.step = 'verified'
    db.session.flush()
    api.send_message(
        admin_chat_id,
        f'✅ Reply sent to support ticket #{request_row.id}.',
        reply_markup={"inline_keyboard": [[
            {"text": "💬 Reply again", "callback_data": f"admin-support:{request_row.id}:reply"},
            {"text": "✅ Close ticket", "callback_data": f"admin-support:{request_row.id}:close"},
        ]]},
    )


def _handle_promo_code_entry(api: TelegramBotApi, bot: TelegramBotInstance,
                             message: dict, sender: dict, state: TelegramBotUserState,
                             text: str):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    language = state.language if state.language in COPY else bot.default_language
    session_row = _purchase_session(bot.id, user_id)
    code = str(text or '').strip().upper()
    if not code or code in ('-', '/CANCEL'):
        session_row.promo_code = None
        session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['promo_code_cleared'])
        return
    promo = TelegramPromo.query.filter_by(code=code, enabled=True).first()
    session_row.promo_code = code if promo else None
    session_row.action = None
    state.step = 'verified'
    db.session.flush()
    api.send_message(
        chat_id,
        _cc(language)['promo_code_saved'] if promo else _cc(language)['promo_code_invalid'],
    )


def _handle_purchase_account_name(api: TelegramBotApi, bot: TelegramBotInstance,
                                  message: dict, sender: dict,
                                  state: TelegramBotUserState, text: str):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    language = state.language if state.language in COPY else bot.default_language
    value = str(text or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{2,31}', value):
        api.send_message(chat_id, _cc(language)['purchase_account_name_invalid'])
        return
    session_row = TelegramPurchaseSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, action='awaiting_account_name',
    ).first()
    server = db.session.get(Server, session_row.server_id) if session_row else None
    package = db.session.get(Package, session_row.package_id) if session_row else None
    if not session_row or not server or not package:
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['start_first'])
        return
    if _purchase_account_name_exists(server.id, value):
        api.send_message(chat_id, _cc(language)['purchase_account_name_taken'])
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


def _finalize_purchase_request(api: TelegramBotApi, bot: TelegramBotInstance,
                               chat_id: int, user_id: int, language: str,
                               state: TelegramBotUserState,
                               session_row: TelegramPurchaseSession,
                               identity: TelegramIdentity,
                               request_row: TelegramPurchaseRequest):
    """Record promo usage, name/detail, reset the session, then notify both sides."""
    try:
        promo_discounts = json.loads(session_row.promo_discounts_json or '{}')
    except (TypeError, ValueError):
        promo_discounts = {}
    for promo_id_raw, discounted in promo_discounts.items():
        try:
            promo_id = int(promo_id_raw)
        except (TypeError, ValueError):
            continue
        db.session.add(TelegramPromoUse(
            promo_id=promo_id,
            telegram_user_id=user_id,
            customer_id=identity.customer_id,
            purchase_request_id=request_row.id,
            amount_discounted=int(discounted or 0),
        ))
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
    session_row.quoted_amount = None
    session_row.promo_id = None
    session_row.promo_code = None
    session_row.discount_amount = None
    session_row.promo_discounts_json = None
    state.step = 'verified'
    db.session.flush()
    api.send_message(
        chat_id, _cc(language)['purchase_pending'],
        reply_markup={'inline_keyboard': [[{
            'text': _cc(language)['menu_orders'],
            'callback_data': f'purchase-order:{request_row.id}',
        }]]},
    )
    _notify_purchase_admins(api, request_row)


def _create_wallet_purchase_request(api: TelegramBotApi, bot: TelegramBotInstance,
                                    chat_id: int, user_id: int, language: str,
                                    state: TelegramBotUserState):
    session_row = _purchase_session(bot.id, user_id)
    identity, customer = _wallet_customer(user_id)
    server = db.session.get(Server, session_row.server_id) if session_row.server_id else None
    package = db.session.get(Package, session_row.package_id) if session_row.package_id else None
    if str(session_row.action or '') != 'awaiting_payment_method' or not server or not package \
            or not identity or not customer:
        api.send_message(chat_id, _cc(language)['start_first'])
        return
    amount = int(session_row.quoted_amount or 0)
    if int(customer.credit or 0) < amount:
        api.send_message(
            chat_id,
            _cc(language)['wallet_insufficient'].format(
                balance=f"{int(customer.credit or 0):,}",
                needed=f"{max(0, amount - int(customer.credit or 0)):,}"),
        )
        return
    duplicate = TelegramPurchaseRequest.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, status='pending',
    ).first()
    if duplicate:
        session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['purchase_duplicate'])
        return
    frozen_discount = int(session_row.discount_amount or 0)
    request_row = TelegramPurchaseRequest(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        customer_id=identity.customer_id,
        server_id=server.id,
        package_id=package.id,
        bank_card_id=None,
        amount=amount,
        original_amount=amount + frozen_discount,
        discount_amount=frozen_discount or None,
        promo_code=(str(session_row.promo_code or '').strip().upper() or None),
        receipt_file_id=f'wallet:{user_id}',
        receipt_file_unique_id=None,
        receipt_kind='photo',
        duplicate_receipt=False,
        payment_method='wallet',
        source_chat_id=chat_id,
        source_message_id=0,
        status='pending',
    )
    db.session.add(request_row)
    db.session.flush()
    customer.credit = int(customer.credit or 0) - amount
    db.session.add(CustomerTransaction(
        customer_id=customer.id,
        type='purchase',
        amount=-amount,
        request_ref=f'purchase:{request_row.id}',
    ))
    _finalize_purchase_request(
        api, bot, chat_id, user_id, language, state, session_row, identity, request_row,
    )


def _handle_purchase_receipt(api: TelegramBotApi, bot: TelegramBotInstance,
                             message: dict, sender: dict,
                             state: TelegramBotUserState):
    chat_id = int((message.get('chat') or {}).get('id'))
    user_id = int(sender['id'])
    if not _rate_ok(user_id, 'receipt', 10, 120):
        return
    language = state.language if state.language in COPY else bot.default_language
    receipt = _receipt_from_message(message)
    if receipt is None:
        api.send_message(chat_id, _cc(language)['receipt_invalid'])
        return
    session_row = TelegramPurchaseSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, action='awaiting_receipt',
    ).first()
    identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
    if not session_row or not identity or not identity.customer_id:
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['start_first'])
        return
    server = db.session.get(Server, session_row.server_id)
    package = db.session.get(Package, session_row.package_id)
    card = db.session.get(BankCard, session_row.bank_card_id)
    owner_id = _effective_owner_id(bot, user_id)
    if (not server or not package or not card or not card.is_active
            or not _purchase_card_accessible(card, bot, owner_id=owner_id)):
        session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['payment_unavailable'])
        return
    duplicate = TelegramPurchaseRequest.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id, status='pending',
    ).first()
    if duplicate:
        session_row.action = None
        state.step = 'verified'
        db.session.flush()
        api.send_message(chat_id, _cc(language)['purchase_duplicate'])
        return
    kind, file_id, unique_id = receipt
    duplicate_receipt = _receipt_is_duplicate(unique_id)
    quoted = session_row.quoted_amount
    frozen_discount = int(session_row.discount_amount or 0)
    final_amount = int(quoted) if quoted is not None else _resolve_purchase_price(owner_id, package)
    request_row = TelegramPurchaseRequest(
        bot_instance_id=bot.id,
        telegram_user_id=user_id,
        customer_id=identity.customer_id,
        server_id=server.id,
        package_id=package.id,
        bank_card_id=card.id,
        amount=final_amount,
        original_amount=final_amount + frozen_discount,
        discount_amount=frozen_discount or None,
        promo_code=(str(session_row.promo_code or '').strip().upper() or None),
        receipt_file_id=file_id,
        receipt_file_unique_id=unique_id or None,
        receipt_kind=kind,
        duplicate_receipt=duplicate_receipt,
        payment_method='card',
        source_chat_id=chat_id,
        source_message_id=int(message.get('message_id') or 0),
        status='pending',
    )
    db.session.add(request_row)
    db.session.flush()
    _finalize_purchase_request(
        api, bot, chat_id, user_id, language, state, session_row, identity, request_row,
    )


def _support_request_for_group_message(bot: TelegramBotInstance, message: dict):
    chat_id = int((message.get('chat') or {}).get('id') or 0)
    if (not bot.support_group_enabled or not bot.support_group_chat_id
            or chat_id != int(bot.support_group_chat_id)):
        return None
    thread_id = int(message.get('message_thread_id') or 0)
    query = TelegramServiceRequest.query.filter_by(
        bot_instance_id=bot.id,
        request_type='support',
        status='pending',
        support_group_chat_id=chat_id,
    )
    if thread_id:
        return query.filter_by(support_message_thread_id=thread_id).first()
    reply_to = message.get('reply_to_message') if isinstance(message.get('reply_to_message'), dict) else {}
    reply_message_id = int(reply_to.get('message_id') or 0)
    if reply_message_id:
        return query.filter_by(support_group_message_id=reply_message_id).first()
    return None


def _handle_support_group_message(api: TelegramBotApi, bot: TelegramBotInstance,
                                  message: dict) -> bool:
    """Relay authorized operator messages from a configured group/topic to the customer."""
    request_row = _support_request_for_group_message(bot, message)
    if not request_row:
        return False
    sender = message.get('from') or {}
    if sender.get('is_bot') or not sender.get('id'):
        return True
    reviewer = _service_request_reviewer(int(sender['id']), request_row)
    if not reviewer or not _support_reviewer_can_handle(reviewer, request_row):
        return True
    text = str(message.get('text') or message.get('caption') or '').strip()[:4000]
    attachment = _support_attachment_from_message(message)
    if text.lower() in ('/close', '/close@' + str(bot.bot_username or '').lower()):
        if not request_row.assigned_admin_id:
            request_row.assigned_admin_id = reviewer.id
        request_row.status = 'completed'
        request_row.reviewed_by_admin_id = reviewer.id
        request_row.reviewed_at = datetime.utcnow()
        request_row.updated_at = datetime.utcnow()
        customer = db.session.get(CustomerAccount, request_row.customer_id)
        language = str(getattr(customer, 'preferred_language', '') or 'fa')
        language = language if language in COPY else 'fa'
        identity = TelegramIdentity.query.filter_by(
            telegram_user_id=request_row.telegram_user_id,
            customer_id=request_row.customer_id,
        ).first()
        if identity and identity.telegram_chat_id:
            try:
                api.send_message(identity.telegram_chat_id, _cc(language)['request_completed'])
            except TelegramApiError:
                pass
        thread_id = int(message.get('message_thread_id') or 0)
        try:
            api.send_message(
                int((message.get('chat') or {}).get('id')),
                f'✅ Support ticket #{request_row.id} closed.',
                **({'message_thread_id': thread_id} if thread_id else {}),
            )
        except TelegramApiError:
            pass
        if request_row.support_message_thread_id:
            try:
                api.close_forum_topic(
                    request_row.support_group_chat_id,
                    request_row.support_message_thread_id,
                )
            except TelegramApiError:
                pass
        return True
    if (not text and not attachment) or text.startswith('/'):
        return True
    identity = TelegramIdentity.query.filter_by(
        telegram_user_id=request_row.telegram_user_id,
        customer_id=request_row.customer_id,
    ).first()
    if not identity or not identity.telegram_chat_id:
        return True
    try:
        if attachment:
            api.copy_message(
                identity.telegram_chat_id,
                int((message.get('chat') or {}).get('id')),
                int(message.get('message_id') or 0),
            )
        else:
            customer = db.session.get(CustomerAccount, request_row.customer_id)
            language = str(getattr(customer, 'preferred_language', '') or 'fa')
            heading = 'پاسخ پشتیبانی:' if language == 'fa' else 'Support reply:'
            api.send_message(identity.telegram_chat_id, f'{heading}\n{text}')
    except TelegramApiError:
        return True
    db.session.add(TelegramServiceRequestMessage(
        request_id=request_row.id,
        sender_type='admin',
        admin_id=reviewer.id,
        message=text,
        source_chat_id=int((message.get('chat') or {}).get('id')),
        source_message_id=int(message.get('message_id') or 0),
        **(attachment or {}),
    ))
    if not request_row.assigned_admin_id:
        request_row.assigned_admin_id = reviewer.id
    if not request_row.first_response_at:
        request_row.first_response_at = datetime.utcnow()
    request_row.updated_at = datetime.utcnow()
    thread_id = int(message.get('message_thread_id') or 0)
    try:
        api.send_message(
            int((message.get('chat') or {}).get('id')),
            f'✅ Reply delivered for ticket #{request_row.id}.',
            **({'message_thread_id': thread_id} if thread_id else {}),
        )
    except TelegramApiError:
        pass
    return True


def _handle_message(api: TelegramBotApi, bot: TelegramBotInstance, message: dict):
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or not sender.get("id") or not chat.get("id"):
        return
    user_id = int(sender["id"])
    chat_id = int(chat["id"])
    pending_admin_reply = TelegramServiceSession.query.filter_by(
        bot_instance_id=bot.id, telegram_user_id=user_id,
    ).first()
    is_authorized_admin_reply = bool(
        pending_admin_reply
        and str(pending_admin_reply.action or '').startswith('admin_support:')
    )
    if not _is_allowed(bot, user_id) and not is_authorized_admin_reply:
        return
    state = _state(bot, user_id)
    _identity(sender, chat_id)
    db.session.flush()
    text = str(message.get("text") or "").strip()
    # Route main-menu reply buttons by copy key, not by raw text, so per-bot
    # label overrides (both languages) keep working.
    menu_key = menu_label_map(bot).get(text)
    if text == "/start" or text.startswith("/start "):
        parts = text.split(None, 1)
        _handle_start(api, bot, chat_id, user_id, state,
                      payload=(parts[1] if len(parts) > 1 else ''))
    elif text in ('/announce', '/announcement', '/اطلاع_رسانی'):
        reviewer = _telegram_admin(user_id)
        can_manage = reviewer and (reviewer.is_superadmin or reviewer.role == 'superadmin'
                                   or reviewer.id == bot.owner_admin_id)
        if not can_manage:
            api.send_message(chat_id, 'این دستور فقط برای مدیر مجاز است.')
        else:
            state.step = 'awaiting_announcement'
            db.session.flush()
            api.send_message(chat_id,
                'متن اطلاع‌رسانی را ارسال کنید. این پیام برای تمام کاربرانی که همین ربات را استارت کرده‌اند آماده می‌شود؛ قبل از ارسال نهایی پیش‌نمایش می‌بینید.')
    elif state.step == 'awaiting_announcement':
        reviewer = _telegram_admin(user_id)
        if not reviewer or not text or len(text) > 4096:
            api.send_message(chat_id, 'متن معتبر تا ۴۰۹۶ کاراکتر ارسال کنید.')
        else:
            filters = {'bot_scope': 'selected', 'bot_ids': [bot.id], 'server_ids': [],
                       'linked_only': False, 'event_match': 'any',
                       'started_from': None, 'started_to': None,
                       'purchased_from': None, 'purchased_to': None,
                       'renewed_from': None, 'renewed_to': None}
            row = TelegramAnnouncement(
                title=f'Bot announcement {datetime.utcnow():%Y-%m-%d %H:%M}',
                message_text=text, filters_json=json.dumps(filters, separators=(',', ':')),
                created_by_admin_id=reviewer.id, source_bot_instance_id=bot.id)
            db.session.add(row)
            db.session.flush()
            state.step = 'verified'
            api.send_message(chat_id, f'پیش‌نمایش پیام:\n\n{text}', reply_markup={'inline_keyboard': [[
                {'text': '✅ ارسال همگانی', 'callback_data': f'announcement-send:{row.id}'},
                {'text': '❌ لغو', 'callback_data': f'announcement-cancel:{row.id}'},
            ]]})
    elif state.step == "awaiting_admin_support_reply":
        _handle_admin_support_message(api, bot, message, sender, state, text)
    elif menu_key == "menu_services":
        state.step = 'verified'
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        _send_owned_services(api, chat_id, state.language, identity)
    elif menu_key == "menu_trial":
        state.step = 'verified'
        _start_trial(api, bot, chat_id, user_id, state)
    elif menu_key == "menu_invite":
        state.step = 'verified'
        username = str(bot.bot_username or '').lstrip('@')
        if username:
            link = f"https://t.me/{username}?start=ref_{user_id}"
            api.send_message(chat_id, _cc(state.language)['invite_link'].format(link=link))
        else:
            api.send_message(chat_id, _cc(state.language)['invite_unavailable'])
    elif menu_key == "menu_buy_service":
        state.step = 'verified'
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        if identity and identity.customer_id and identity.phone_verified_at:
            if _purchase_policy_values(bot)['customer_selects_server']:
                _send_purchase_servers(api, bot, chat_id, state.language, user_id=user_id)
            else:
                _send_purchase_packages(api, bot, chat_id, state.language, None, user_id=user_id)
        else:
            _send_contact_prompt(api, chat_id, state.language)
    elif menu_key == "menu_orders":
        state.step = 'verified'
        _send_purchase_orders(api, bot, chat_id, user_id, state.language)
    elif menu_key == "menu_wallet":
        state.step = 'verified'
        _send_wallet_menu(api, bot, chat_id, user_id, state.language)
    elif menu_key == "menu_tutorial":
        state.step = 'verified'
        _send_tutorial_devices(api, chat_id, state.language)
    elif menu_key == "menu_add_service":
        state.step = 'verified'
        identity = TelegramIdentity.query.filter_by(telegram_user_id=user_id).first()
        if identity and identity.customer_id and identity.phone_verified_at:
            claim = discover_phone_ownership_claim(identity)
            _send_claim_candidates(api, chat_id, state.language, claim)
        else:
            _send_contact_prompt(api, chat_id, state.language)
    elif menu_key == "menu_support_requests":
        state.step = 'verified'
        _send_support_requests(api, bot, chat_id, user_id, state.language)
    elif menu_key == "menu_language":
        state.step = "choose_language"
        db.session.flush()
        api.send_message(
            chat_id, _cc(state.language)["choose_language"],
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
    elif state.step == "awaiting_promo_code":
        _handle_promo_code_entry(api, bot, message, sender, state, text)
    elif state.step == "awaiting_purchase_receipt":
        _handle_purchase_receipt(api, bot, message, sender, state)
    elif state.step == "awaiting_topup_amount":
        _handle_wallet_topup_amount(api, bot, message, sender, state, text)
    elif state.step == "awaiting_topup_receipt":
        _handle_wallet_topup_receipt(api, bot, message, sender, state)
    elif state.step == "awaiting_renewal_receipt":
        _handle_renewal_receipt(api, bot, message, sender, state)
    elif state.step == "share_contact":
        _send_contact_prompt(api, chat_id, state.language)
    else:
        api.send_message(chat_id, _cc(state.language)["start_first"])


def process_update(api: TelegramBotApi, bot: TelegramBotInstance, update: dict):
    # The Telegram worker is a separate process and background snapshot readers
    # are intentionally disabled here. Pull only when Redis' snapshot version
    # changed so service details never depend on stale process-local memory.
    load_snapshot_from_redis()
    _copy_context.copy = resolve_copy(bot)
    _copy_context.overrides = parse_copy_overrides(bot)
    if isinstance(update.get("callback_query"), dict):
        _handle_callback(api, bot, update["callback_query"])
    elif isinstance(update.get("message"), dict):
        message = update["message"]
        chat_type = str((message.get('chat') or {}).get('type') or '')
        if chat_type in ('group', 'supergroup'):
            _handle_support_group_message(api, bot, message)
        else:
            _handle_message(api, bot, message)


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
        # Customer updates always run before periodic operational scans, so an
        # SLA sweep can never delay a button response that is already waiting.
        now_monotonic = time.monotonic()
        if now_monotonic - sla_scan_at_by_bot.get(bot.id, 0) >= 30:
            try:
                _scan_support_sla(api, bot)
                db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception('[telegram-support] SLA scan failed for bot %s', bot.id)
            finally:
                sla_scan_at_by_bot[bot.id] = now_monotonic
    except TelegramApiError as exc:
        db.session.rollback()
        runtime = _runtime(bot.id)
        runtime.status = "error"
        runtime.last_error = redact_connection_error(exc, (token,))
        runtime.last_heartbeat_at = datetime.utcnow()
        runtime.lease_expires_at = datetime.utcnow() + timedelta(seconds=60)
        db.session.commit()
        time.sleep(5)


def _run_bot(bot_id: int, stop_event: threading.Event):
    """Long-poll one bot independently so another bot can never block it."""
    while running and not stop_event.is_set():
        with app.app_context():
            try:
                bot = db.session.get(TelegramBotInstance, int(bot_id))
                if bot is None or bot.transport_mode != "polling" or bot.archived_at is not None:
                    return
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
            except Exception:
                db.session.rollback()
                app.logger.exception('[telegram-worker] bot loop failed for bot %s', bot_id)
                stop_event.wait(2)
                continue
        stop_event.wait(1)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        while running:
            with app.app_context():
                desired_ids = {
                    int(row.id) for row in
                    TelegramBotInstance.query.filter(
                        TelegramBotInstance.transport_mode == "polling",
                        TelegramBotInstance.archived_at.is_(None),
                    ).all()
                }
            for bot_id in desired_ids:
                current = bot_threads.get(bot_id)
                if current and current[0].is_alive():
                    continue
                stop_event = threading.Event()
                thread = threading.Thread(
                    target=_run_bot, args=(bot_id, stop_event),
                    name=f'telegram-bot-{bot_id}', daemon=True,
                )
                bot_threads[bot_id] = (thread, stop_event)
                thread.start()
            for bot_id in list(bot_threads):
                thread, stop_event = bot_threads[bot_id]
                if bot_id not in desired_ids or not thread.is_alive():
                    stop_event.set()
                    bot_threads.pop(bot_id, None)
            time.sleep(2 if desired_ids else 3)
    finally:
        for _thread, stop_event in bot_threads.values():
            stop_event.set()
        deadline = time.monotonic() + 5
        for thread, _stop_event in bot_threads.values():
            thread.join(timeout=max(0, deadline - time.monotonic()))


if __name__ == "__main__":
    main()
