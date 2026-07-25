"""Telegram operations, bots, promos, announcements, and backup API routes (extracted from app.py)."""
import io
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, send_file, session, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from panel.adapters.xui import _safe_response_json
from panel.extensions import db, limiter
from panel.models import (
    Admin, BankCard, CustomerAccount, CustomerTransaction, Package, Server,
    ServiceOwnership, TelegramAnnouncement, TelegramBotInstance,
    TelegramBotRuntime, TelegramBotStartEvent, TelegramBotTestUser,
    TelegramEgressProfile, TelegramIdentity, TelegramPromo, TelegramPromoUse,
    TelegramProxyEndpoint, TelegramPurchaseRequest, TelegramServiceRequest,
    TelegramServiceRequestMessage, TelegramWalletTopup,
)
from panel.routes.common import (
    login_required, superadmin_required, user_management_required,
)
from panel.services.backup import (
    TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES,
    TELEGRAM_BACKUP_MAX_INTERVAL_MINUTES,
    _get_system_setting_value, _get_telegram_backup_settings,
    _normalize_proxy_url, _parse_int, _set_system_setting_value,
    _telegram_backup_route_proxies,
)
from panel.services.subscription import generate_client_link
from telegram_diagnostics import (
    classify_telegram_connection_error, redact_connection_error,
)
from telegram_xray import build_xray_config_from_uri

bp = Blueprint('telegram', __name__)


bp = Blueprint('telegram', __name__)


def _telegram_egress_from_payload(profile, data):
    from app import (  # deferred: app-level helper, avoids circular import
        _decrypt_telegram_secret, _encrypt_telegram_secret, _find_telegram_egress_candidate,
    )
    profile.name = str(data.get('name') or profile.name or '').strip()[:120]
    if not profile.name:
        raise ValueError('Egress profile name is required')
    try:
        profile.local_port = int(data.get('local_port') if data.get('local_port') is not None
                                 else (profile.local_port or 12080))
        profile.priority = int(data.get('priority') if data.get('priority') is not None
                               else (profile.priority or 50))
    except (TypeError, ValueError):
        raise ValueError('Local port and priority must be whole numbers')
    if profile.local_port < 1024 or profile.local_port > 65535:
        raise ValueError('Local SOCKS port must be between 1024 and 65535')
    profile.priority = max(0, min(profile.priority, 10000))
    if 'enabled' in data:
        profile.enabled = bool(data.get('enabled'))

    uri = str(data.get('config_uri') or '').strip()
    if data.get('server_id') and data.get('inbound_id') and data.get('client_id'):
        server, inbound, client, uri = _find_telegram_egress_candidate(
            data.get('server_id'), data.get('inbound_id'), data.get('client_id'),
        )
        profile.source_type = 'managed_server_client'
        profile.server_id = server.id
        profile.inbound_id = int(inbound.get('id'))
        profile.client_email_snapshot = str(client.get('email') or '')[:255]
    elif uri:
        profile.source_type = 'manual_uri'
        profile.server_id = None
        profile.inbound_id = None
        profile.client_email_snapshot = None
    if uri:
        build_xray_config_from_uri(uri, profile.local_port)
        scheme = urlparse(uri).scheme.lower()
        profile.protocol = 'shadowsocks' if scheme == 'ss' else scheme
        profile.config_encrypted = _encrypt_telegram_secret(uri)
        profile.runtime_status = 'pending'
    elif not profile.config_encrypted:
        raise ValueError('Choose a 3x-ui account and inbound, or provide a supported configuration URI')
    else:
        build_xray_config_from_uri(_decrypt_telegram_secret(profile.config_encrypted), profile.local_port)
    return profile


def _allocate_telegram_backup_egress_port() -> int:
    used = {int(value) for (value,) in db.session.query(TelegramEgressProfile.local_port).all()}
    for port in range(13080, 13181):
        if port in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.05)
            if probe.connect_ex(('127.0.0.1', port)) != 0:
                return port
    raise RuntimeError('No free local port is available for the Telegram backup route')


def _ensure_telegram_backup_egress_profile(data: dict) -> TelegramEgressProfile:
    """Create or refresh the dedicated Xray route used only by backups."""
    from app import _central_telegram_bot  # deferred: app-level helper, avoids circular import
    existing_id = _parse_int(
        _get_system_setting_value('telegram_backup_egress_profile_id', ''), 0, min_value=0,
    )
    profile = db.session.get(TelegramEgressProfile, existing_id) if existing_id else None
    if profile and profile.source_type != 'telegram_backup_account':
        profile = None
    if profile is None:
        bot = _central_telegram_bot(create=True)
        profile = TelegramEgressProfile(
            bot_instance_id=bot.id,
            name='Telegram backup route',
            local_port=_allocate_telegram_backup_egress_port(),
            priority=10,
            enabled=True,
            config_encrypted='',
        )
        db.session.add(profile)
    payload = {
        'name': 'Telegram backup route',
        'local_port': profile.local_port,
        'priority': 10,
        'enabled': True,
        'server_id': data.get('server_id'),
        'inbound_id': data.get('inbound_id'),
        'client_id': data.get('client_id'),
    }
    _telegram_egress_from_payload(profile, payload)
    profile.source_type = 'telegram_backup_account'
    db.session.flush()
    _set_system_setting_value('telegram_backup_egress_profile_id', str(profile.id))
    return profile


def _disable_telegram_backup_egress_profile():
    profile_id = _parse_int(
        _get_system_setting_value('telegram_backup_egress_profile_id', ''), 0, min_value=0,
    )
    profile = db.session.get(TelegramEgressProfile, profile_id) if profile_id else None
    if profile and profile.source_type == 'telegram_backup_account':
        profile.enabled = False
        profile.runtime_status = 'disabled'


def _telegram_service_visible_to(admin, row):
    if not admin:
        return False
    if admin.is_superadmin or str(admin.role or '').lower() in ('admin', 'superadmin'):
        return True
    return (str(admin.role or '').lower() == 'reseller' and row.ownership
            and row.ownership.reseller_id == admin.id)


def _telegram_operation_customer_payload(row, identity_map=None, customer_map=None):
    from app import _telegram_operation_identity  # deferred: app-level helper, avoids circular import
    identity = ((identity_map or {}).get(int(row.telegram_user_id))
                if identity_map is not None else
                _telegram_operation_identity(row.telegram_user_id, row.customer_id))
    customer = ((customer_map or {}).get(int(row.customer_id))
                if customer_map is not None else db.session.get(CustomerAccount, row.customer_id))
    phone = ((customer.primary_phone if customer else None)
             or (identity.phone_normalized if identity else None))
    telegram_username = str(identity.username or '').lstrip('@') if identity else ''
    private_url = (f'https://t.me/{telegram_username}' if telegram_username
                   else f'tg://user?id={int(row.telegram_user_id)}')
    return {
        'telegram_user_id': int(row.telegram_user_id),
        'telegram_username': telegram_username or None,
        'telegram_private_url': private_url,
        'telegram_name': (' '.join(filter(None, [identity.first_name, identity.last_name])).strip()
                          if identity else None),
        'phone': phone,
        'customer_phone': phone,
        'customer_name': customer.display_name if customer else None,
        'customer_credit': int(customer.credit or 0) if customer else None,
    }


def _serialize_telegram_purchase(row, identity_map=None, customer_map=None):
    detail = row.detail
    allocation = row.inbound_allocation
    payload = {
        'id': row.id,
        'kind': 'purchase',
        'status': row.status,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
        'reviewed_at': row.reviewed_at.isoformat() if row.reviewed_at else None,
        'server_name': row.server.name if row.server else None,
        'account_name': detail.account_name if detail else None,
        'package_name': row.package.name if row.package else None,
        'amount': int(row.amount or 0),
        'original_amount': int(row.original_amount or 0) if row.original_amount is not None else None,
        'discount_amount': int(row.discount_amount or 0) if row.discount_amount is not None else None,
        'promo_code': row.promo_code,
        'duplicate_receipt': bool(getattr(row, 'duplicate_receipt', False)),
        'payment_method': str(getattr(row, 'payment_method', '') or 'card'),
        'bank_card_id': row.bank_card_id,
        'note': None,
        'receipt_kind': row.receipt_kind,
        'receipt_url': url_for('telegram.telegram_purchase_receipt', request_id=row.id),
        'allocation_mode': allocation.mode if allocation else None,
        'inbound_ids': allocation.inbound_ids() if allocation else [],
        'messages': [],
    }
    payload.update(_telegram_operation_customer_payload(row, identity_map, customer_map))
    return payload


def _support_assignable_operators(row, admins=None):
    admins = admins if admins is not None else Admin.query.filter_by(enabled=True).all()
    reseller_id = getattr(row.ownership, 'reseller_id', None) if row.ownership else None
    result = []
    for admin in admins:
        role = str(admin.role or '').lower()
        if not (admin.is_superadmin or role in ('admin', 'superadmin')
                or (role == 'reseller' and admin.id == reseller_id)):
            continue
        result.append({
            'id': admin.id,
            'username': admin.username,
            'role': role or 'admin',
        })
    return result


def _serialize_telegram_service(row, identity_map=None, customer_map=None, admins=None):
    ownership = row.ownership
    messages = []
    for message in row.messages:
        messages.append({
            'id': message.id,
            'sender_type': message.sender_type,
            'sender_name': message.admin.username if message.admin else None,
            'message': message.message,
            'created_at': message.created_at.isoformat() if message.created_at else None,
            'attachment_kind': message.attachment_kind,
            'attachment_name': message.attachment_name,
            'attachment_mime': message.attachment_mime,
            'attachment_size': int(message.attachment_size or 0),
            'attachment_url': (
                url_for('telegram.telegram_support_message_attachment', request_id=row.id,
                        message_id=message.id)
                if message.attachment_file_id else None
            ),
        })
    if row.request_type == 'support' and row.note and not messages:
        messages.append({
            'id': None, 'sender_type': 'customer', 'sender_name': None,
            'message': row.note,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        })
    last_sender = messages[-1].get('sender_type') if messages else 'customer'
    support_state = ('closed' if row.status != 'pending'
                     else 'waiting_customer' if last_sender == 'admin'
                     else 'in_progress' if row.assigned_admin_id
                     else 'waiting_admin')
    priority = str(row.support_priority or 'normal').lower()
    if priority not in ('low', 'normal', 'high', 'urgent'):
        priority = 'normal'
    sla_minutes = max(0, int(getattr(row.bot, 'support_sla_minutes', 0) or 0))
    last_customer_at = next((message.created_at for message in reversed(row.messages)
                             if message.sender_type == 'customer'), row.created_at)
    last_customer_message_id = next((message.id for message in reversed(row.messages)
                                     if message.sender_type == 'customer'), None)
    response_due_at = (last_customer_at + timedelta(minutes=sla_minutes)
                       if row.status == 'pending' and last_sender != 'admin'
                       and sla_minutes and last_customer_at else None)
    sla_overdue = bool(response_due_at and response_due_at < datetime.utcnow())
    payload = {
        'id': row.id,
        'kind': row.request_type,
        'status': row.status,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
        'reviewed_at': row.reviewed_at.isoformat() if row.reviewed_at else None,
        'server_name': ownership.server.name if ownership and ownership.server else None,
        'account_name': ownership.client_email_snapshot if ownership else None,
        'package_name': row.package.name if row.package else None,
        'amount': int(row.amount or 0),
        'note': row.note,
        'payment_method': str(getattr(row, 'payment_method', '') or 'card'),
        'bank_card_id': getattr(row, 'bank_card_id', None),
        'receipt_url': None,
        'messages': messages,
        'support_state': support_state if row.request_type == 'support' else None,
        'support_group_routed': bool(row.support_group_chat_id),
        'support_message_thread_id': row.support_message_thread_id,
        'support_priority': priority,
        'assigned_admin_id': row.assigned_admin_id,
        'assigned_admin_username': row.assigned_admin.username if row.assigned_admin else None,
        'assignable_operators': _support_assignable_operators(row, admins),
        'first_response_at': row.first_response_at.isoformat() if row.first_response_at else None,
        'support_sla_minutes': sla_minutes,
        'support_response_due_at': response_due_at.isoformat() if response_due_at else None,
        'support_sla_overdue': sla_overdue,
        'support_sla_warned': bool(
            last_customer_message_id
            and row.sla_warning_message_id == last_customer_message_id
        ),
        'support_sla_escalated': bool(
            last_customer_message_id
            and row.sla_escalated_message_id == last_customer_message_id
        ),
        'support_sla_warning_at': row.sla_warning_at.isoformat() if row.sla_warning_at else None,
        'support_sla_escalated_at': row.sla_escalated_at.isoformat() if row.sla_escalated_at else None,
    }
    payload.update(_telegram_operation_customer_payload(row, identity_map, customer_map))
    return payload


def _telegram_operation_notify(row, *, result=None, reply=None):
    """Best-effort customer notification; an API outage must not undo an operator action."""
    from app import _telegram_bot_api_client, _telegram_operation_identity  # deferred: app-level helper, avoids circular import
    identity = _telegram_operation_identity(row.telegram_user_id, row.customer_id)
    if not identity or not identity.telegram_chat_id:
        return False
    bot = db.session.get(TelegramBotInstance, row.bot_instance_id)
    if not bot:
        return False
    customer = db.session.get(CustomerAccount, row.customer_id)
    language = str(getattr(customer, 'preferred_language', '') or 'fa')
    try:
        from telegram_bot_worker import COPY, _deliver_or_request_membership
        language_copy = COPY.get(language, COPY['fa'])
        if reply is not None:
            heading = 'پاسخ پشتیبانی:' if language == 'fa' else 'Support reply:'
            text_value = f'{heading}\n{reply}'
        elif isinstance(row, TelegramPurchaseRequest):
            if row.status == 'completed':
                return _deliver_or_request_membership(
                    _telegram_bot_api_client(bot), bot, row, identity.telegram_chat_id,
                    language, row.telegram_user_id, result=result,
                )
            else:
                key = 'purchase_approved' if row.status == 'approved' else 'purchase_rejected'
                text_value = language_copy[key]
        else:
            key = 'request_completed' if row.status == 'completed' else 'request_rejected'
            text_value = language_copy[key]
        _telegram_bot_api_client(bot).send_message(identity.telegram_chat_id, text_value)
        return True
    except Exception as exc:
        logging.warning('Telegram operation notification failed for %s #%s: %s',
                        type(row).__name__, row.id, redact_connection_error(exc))
        return False




@bp.route('/api/telegram-operations', methods=['GET'])
@limiter.limit('60 per minute')
@login_required
def get_telegram_operations():
    from app import _telegram_operations_admin, _telegram_purchase_visible_to  # deferred: app-level helper, avoids circular import
    admin = _telegram_operations_admin()
    kind = str(request.args.get('kind') or 'all').strip().lower()
    status = str(request.args.get('status') or 'all').strip().lower()
    search = str(request.args.get('search') or '').strip().lower()[:120]
    purchases = TelegramPurchaseRequest.query.options(
        joinedload(TelegramPurchaseRequest.server),
        joinedload(TelegramPurchaseRequest.package),
        joinedload(TelegramPurchaseRequest.detail),
        joinedload(TelegramPurchaseRequest.inbound_allocation),
    ).order_by(
        TelegramPurchaseRequest.created_at.desc(), TelegramPurchaseRequest.id.desc(),
    ).limit(250).all()
    services = TelegramServiceRequest.query.options(
        joinedload(TelegramServiceRequest.ownership).joinedload(ServiceOwnership.server),
        joinedload(TelegramServiceRequest.package),
        joinedload(TelegramServiceRequest.messages).joinedload(TelegramServiceRequestMessage.admin),
        joinedload(TelegramServiceRequest.bot),
        joinedload(TelegramServiceRequest.assigned_admin),
    ).order_by(
        TelegramServiceRequest.created_at.desc(), TelegramServiceRequest.id.desc(),
    ).limit(250).all()
    visible_purchases = [row for row in purchases if _telegram_purchase_visible_to(admin, row)]
    visible_services = [row for row in services if _telegram_service_visible_to(admin, row)]
    visible_rows = visible_purchases + visible_services
    telegram_ids = {int(row.telegram_user_id) for row in visible_rows}
    customer_ids = {int(row.customer_id) for row in visible_rows}
    identity_map = {int(row.telegram_user_id): row for row in TelegramIdentity.query.filter(
        TelegramIdentity.telegram_user_id.in_(telegram_ids or {-1}),
    ).all()}
    customer_map = {int(row.id): row for row in CustomerAccount.query.filter(
        CustomerAccount.id.in_(customer_ids or {-1}),
    ).all()}
    enabled_admins = Admin.query.filter_by(enabled=True).order_by(Admin.username.asc()).all()
    all_items = ([_serialize_telegram_purchase(row, identity_map, customer_map)
                  for row in visible_purchases]
                 + [_serialize_telegram_service(row, identity_map, customer_map, enabled_admins)
                    for row in visible_services])
    all_items.sort(key=lambda item: (item.get('created_at') or '', item['id']), reverse=True)
    today = datetime.utcnow().date()
    counters = {
        'waiting_review': sum(item['status'] == 'pending' and item['kind'] in ('purchase', 'renewal')
                              for item in all_items),
        'provisioning_retry': sum(item['status'] == 'approved' and item['kind'] == 'purchase'
                                  for item in all_items),
        'open_support': sum(item['status'] == 'pending' and item['kind'] == 'support'
                            for item in all_items),
        'support_waiting_admin': sum(item.get('support_state') == 'waiting_admin'
                                     for item in all_items),
        'support_waiting_customer': sum(item.get('support_state') == 'waiting_customer'
                                        for item in all_items),
        'support_in_progress': sum(item.get('support_state') == 'in_progress'
                                   for item in all_items),
        'support_sla_overdue': sum(bool(item.get('support_sla_overdue'))
                                   for item in all_items),
        'support_sla_escalated': sum(bool(item.get('support_sla_escalated'))
                                     for item in all_items),
        'completed_today': sum(
            item['status'] == 'completed' and item.get('updated_at')
            and datetime.fromisoformat(item['updated_at']).date() == today
            for item in all_items
        ),
    }
    filtered = []
    for item in all_items:
        if kind != 'all' and item['kind'] != kind:
            continue
        if status != 'all':
            support_filter_match = bool(
                item['kind'] == 'support' and (
                    item.get('support_state') == status
                    or (status == 'sla_overdue' and item.get('support_sla_overdue'))
                    or (status == 'sla_escalated' and item.get('support_sla_escalated'))
                    or (status in ('low', 'normal', 'high', 'urgent')
                        and item.get('support_priority') == status)
                )
            )
            if item['status'] != status and not support_filter_match:
                continue
        if search:
            haystack = ' '.join(str(item.get(key) or '') for key in (
                'id', 'telegram_user_id', 'telegram_username', 'phone', 'customer_name',
                'server_name', 'account_name', 'package_name', 'note',
            )).lower()
            if search not in haystack:
                continue
        filtered.append(item)
    bot_names = {
        int(bot.id): bot.display_name
        for bot in TelegramBotInstance.query.all()
    }
    per_bot_map = {}
    for row in visible_purchases:
        entry = per_bot_map.setdefault(int(row.bot_instance_id), {
            'bot_id': int(row.bot_instance_id),
            'bot_name': bot_names.get(int(row.bot_instance_id)) or f'#{int(row.bot_instance_id)}',
            'purchases': 0, 'completed': 0, 'revenue': 0, 'open_tickets': 0,
        })
        entry['purchases'] += 1
        if row.status in ('approved', 'completed'):
            entry['revenue'] += int(row.amount or 0)
        if row.status == 'completed':
            entry['completed'] += 1
    for row in visible_services:
        entry = per_bot_map.setdefault(int(row.bot_instance_id), {
            'bot_id': int(row.bot_instance_id),
            'bot_name': bot_names.get(int(row.bot_instance_id)) or f'#{int(row.bot_instance_id)}',
            'purchases': 0, 'completed': 0, 'revenue': 0, 'open_tickets': 0,
        })
        if row.status == 'pending' and row.request_type == 'support':
            entry['open_tickets'] += 1
    per_bot = []
    for entry in per_bot_map.values():
        entry['completion_rate'] = (
            round(entry['completed'] / entry['purchases'], 3) if entry['purchases'] else None
        )
        per_bot.append(entry)
    per_bot.sort(key=lambda entry: entry['bot_id'])
    return jsonify({'success': True, 'items': filtered, 'counters': counters, 'per_bot': per_bot})


def _telegram_purchase_for_admin(request_id):
    from app import _telegram_operations_admin, _telegram_purchase_visible_to  # deferred: app-level helper, avoids circular import
    row = db.session.get(TelegramPurchaseRequest, request_id)
    if not row:
        return None, (jsonify({'success': False, 'error': 'Purchase request not found'}), 404)
    if not _telegram_purchase_visible_to(_telegram_operations_admin(), row):
        return None, (jsonify({'success': False, 'error': 'Access denied'}), 403)
    return row, None


def _telegram_service_for_admin(request_id):
    from app import _telegram_operations_admin  # deferred: app-level helper, avoids circular import
    row = db.session.get(TelegramServiceRequest, request_id)
    if not row:
        return None, (jsonify({'success': False, 'error': 'Service request not found'}), 404)
    if not _telegram_service_visible_to(_telegram_operations_admin(), row):
        return None, (jsonify({'success': False, 'error': 'Access denied'}), 403)
    return row, None


def _telegram_topup_for_admin(topup_id):
    from app import _telegram_operations_admin  # deferred: app-level helper, avoids circular import
    row = db.session.get(TelegramWalletTopup, topup_id)
    if not row:
        return None, (jsonify({'success': False, 'error': 'Top-up request not found'}), 404)
    admin = _telegram_operations_admin()
    if not admin:
        return None, (jsonify({'success': False, 'error': 'Access denied'}), 403)
    if admin.is_superadmin or str(admin.role or '').lower() in ('admin', 'superadmin'):
        return row, None
    bot = db.session.get(TelegramBotInstance, row.bot_instance_id)
    if str(admin.role or '').lower() == 'reseller' and bot and bot.owner_admin_id == admin.id:
        return row, None
    return None, (jsonify({'success': False, 'error': 'Access denied'}), 403)


def _telegram_update_request_card(row, kind, request_id):
    from app import _log_audit, _telegram_operations_admin  # deferred: app-level helper, avoids circular import
    payload = request.get_json(silent=True) or {}
    try:
        card_id = int(payload.get('card_id') or 0)
    except (TypeError, ValueError):
        card_id = 0
    card = db.session.get(BankCard, card_id) if card_id else None
    if not card or not card.is_active:
        return jsonify({'success': False, 'error': 'Active bank card not found'}), 400
    row.bank_card_id = card.id
    _log_audit(f'telegram_{kind}.update_card', row, actor=_telegram_operations_admin(),
               meta={'bank_card_id': card.id})
    db.session.commit()
    return jsonify({'success': True, 'bank_card_id': card.id})


@bp.route('/api/telegram-operations/purchases/<int:request_id>/card', methods=['PUT'])
@limiter.limit('20 per minute')
@login_required
def update_telegram_purchase_card(request_id):
    row, error = _telegram_purchase_for_admin(request_id)
    if error:
        return error
    return _telegram_update_request_card(row, 'purchase', request_id)


@bp.route('/api/telegram-operations/service-requests/<int:request_id>/card', methods=['PUT'])
@limiter.limit('20 per minute')
@login_required
def update_telegram_service_request_card(request_id):
    row, error = _telegram_service_for_admin(request_id)
    if error:
        return error
    return _telegram_update_request_card(row, 'service', request_id)


@bp.route('/api/telegram-operations/wallet-topups/<int:topup_id>/card', methods=['PUT'])
@limiter.limit('20 per minute')
@login_required
def update_telegram_wallet_topup_card(topup_id):
    row, error = _telegram_topup_for_admin(topup_id)
    if error:
        return error
    return _telegram_update_request_card(row, 'topup', topup_id)


def _refund_wallet_request(row, kind):
    """Refund a wallet-paid request back to the customer wallet; returns the amount."""
    if str(getattr(row, 'payment_method', '') or 'card') != 'wallet':
        return 0
    customer = db.session.get(CustomerAccount, row.customer_id)
    amount = int(getattr(row, 'amount', 0) or 0)
    if not customer or amount <= 0:
        return 0
    customer.credit = int(customer.credit or 0) + amount
    db.session.add(CustomerTransaction(
        customer_id=customer.id,
        type='refund',
        amount=amount,
        request_ref=f'{kind}:{row.id}',
    ))
    return amount


@bp.route('/api/telegram-operations/purchases/<int:request_id>/<action>', methods=['POST'])
@limiter.limit('20 per minute')
@login_required
def review_telegram_purchase_operation(request_id, action):
    from app import _log_audit, _telegram_operations_admin  # deferred: app-level helper, avoids circular import
    if action not in ('approve', 'retry', 'reject'):
        return jsonify({'success': False, 'error': 'Invalid action'}), 404
    row, error = _telegram_purchase_for_admin(request_id)
    if error:
        return error
    admin = _telegram_operations_admin()
    if action == 'reject':
        if row.status != 'pending':
            return jsonify({'success': False, 'error': 'Only pending purchases can be rejected'}), 409
        row.status = 'rejected'
        row.reviewed_by_admin_id = admin.id
        row.reviewed_at = datetime.utcnow()
        _refund_wallet_request(row, 'purchase')
        _log_audit('telegram_purchase.reject', row, actor=admin)
        db.session.commit()
        _telegram_operation_notify(row)
        return jsonify({'success': True, 'item': _serialize_telegram_purchase(row)})
    if row.status not in ('pending', 'approved'):
        return jsonify({'success': False, 'error': 'This purchase is already completed or rejected'}), 409
    if action == 'retry' and row.status != 'approved':
        return jsonify({'success': False, 'error': 'Retry is only available after payment approval'}), 409
    if row.status == 'pending':
        row.status = 'approved'
        row.reviewed_by_admin_id = admin.id
        row.reviewed_at = datetime.utcnow()
        db.session.flush()
    from telegram_bot_worker import _execute_purchase_request
    provisioned, result = _execute_purchase_request(row, admin)
    if not provisioned:
        db.session.commit()
        _telegram_operation_notify(row)
        return jsonify({'success': False, 'error': str(result),
                        'item': _serialize_telegram_purchase(row)}), 409
    row.status = 'completed'
    _log_audit(f'telegram_purchase.{action}', row, actor=admin)
    db.session.commit()
    _telegram_operation_notify(row, result=result)
    return jsonify({'success': True, 'item': _serialize_telegram_purchase(row)})


@bp.route('/api/telegram-operations/purchases/<int:request_id>/receipt', methods=['GET'])
@limiter.limit('60 per minute')
@login_required
def telegram_purchase_receipt(request_id):
    from app import _telegram_bot_api_client  # deferred: app-level helper, avoids circular import
    row, error = _telegram_purchase_for_admin(request_id)
    if error:
        return error
    bot = db.session.get(TelegramBotInstance, row.bot_instance_id)
    if not bot:
        return jsonify({'success': False, 'error': 'Telegram bot is unavailable'}), 404
    try:
        content, content_type, filename, route_name = _telegram_bot_api_client(bot).download_file(
            row.receipt_file_id,
        )
        safe_inline_types = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'application/pdf'}
        content_type = str(content_type or '').split(';', 1)[0].strip().lower()
        inline = content_type in safe_inline_types
        response = send_file(
            io.BytesIO(content), mimetype=(content_type if inline else 'application/octet-stream'),
            download_name=(secure_filename(filename) or 'telegram-receipt'),
            as_attachment=not inline, max_age=0,
        )
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Telegram-Route'] = route_name
        return response
    except Exception as exc:
        safe_error = redact_connection_error(exc)
        logging.warning('Telegram receipt download failed for purchase #%s: %s', row.id, safe_error)
        return jsonify({'success': False, 'error': safe_error}), 502


TELEGRAM_SUPPORT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024
TELEGRAM_SUPPORT_INLINE_TYPES = {
    'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'application/pdf',
}
TELEGRAM_SUPPORT_ALLOWED_TYPES = TELEGRAM_SUPPORT_INLINE_TYPES | {
    'text/plain', 'application/zip', 'application/x-zip-compressed',
}


def _telegram_support_attachment_from_result(result, *, fallback_kind, fallback_name,
                                             fallback_mime, fallback_size):
    result = result if isinstance(result, dict) else {}
    if fallback_kind == 'photo':
        photos = result.get('photo') if isinstance(result.get('photo'), list) else []
        payload = photos[-1] if photos and isinstance(photos[-1], dict) else {}
        kind = 'photo'
        name = fallback_name or 'support-image.jpg'
        mime = fallback_mime or 'image/jpeg'
    else:
        payload = result.get('document') if isinstance(result.get('document'), dict) else {}
        kind = 'document'
        name = str(payload.get('file_name') or fallback_name or 'support-file')[:255]
        mime = str(payload.get('mime_type') or fallback_mime or 'application/octet-stream')[:127]
    return {
        'attachment_kind': kind,
        'attachment_file_id': str(payload.get('file_id') or '') or None,
        'attachment_file_unique_id': str(payload.get('file_unique_id') or '')[:255] or None,
        'attachment_name': name[:255],
        'attachment_mime': mime,
        'attachment_size': int(payload.get('file_size') or fallback_size or 0),
        'source_message_id': int(result.get('message_id') or 0) or None,
    }


@bp.route('/api/telegram-operations/services/<int:request_id>/messages/<int:message_id>/attachment')
@limiter.limit('60 per minute')
@login_required
def telegram_support_message_attachment(request_id, message_id):
    from app import _telegram_bot_api_client  # deferred: app-level helper, avoids circular import
    row, error = _telegram_service_for_admin(request_id)
    if error:
        return error
    message = TelegramServiceRequestMessage.query.filter_by(
        id=message_id, request_id=row.id,
    ).first()
    if not message or not message.attachment_file_id:
        return jsonify({'success': False, 'error': 'Attachment not found'}), 404
    bot = db.session.get(TelegramBotInstance, row.bot_instance_id)
    if not bot:
        return jsonify({'success': False, 'error': 'Telegram bot is unavailable'}), 404
    try:
        content, detected_type, detected_name, route_name = _telegram_bot_api_client(bot).download_file(
            message.attachment_file_id, max_bytes=TELEGRAM_SUPPORT_ATTACHMENT_MAX_BYTES,
        )
        content_type = str(message.attachment_mime or detected_type or 'application/octet-stream')
        content_type = content_type.split(';', 1)[0].strip().lower()
        inline = content_type in TELEGRAM_SUPPORT_INLINE_TYPES
        filename = secure_filename(message.attachment_name or detected_name) or 'support-attachment'
        response = send_file(
            io.BytesIO(content),
            mimetype=(content_type if inline else 'application/octet-stream'),
            download_name=filename,
            as_attachment=not inline,
            max_age=0,
        )
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Telegram-Route'] = route_name
        return response
    except Exception as exc:
        safe_error = redact_connection_error(exc)
        logging.warning('Telegram support attachment failed for message #%s: %s',
                        message.id, safe_error)
        return jsonify({'success': False, 'error': safe_error}), 502


@bp.route('/api/telegram-operations/services/<int:request_id>/<action>', methods=['POST'])
@limiter.limit('20 per minute')
@login_required
def review_telegram_service_operation(request_id, action):
    from app import _log_audit, _telegram_operations_admin  # deferred: app-level helper, avoids circular import
    if action not in ('complete', 'reject'):
        return jsonify({'success': False, 'error': 'Invalid action'}), 404
    row, error = _telegram_service_for_admin(request_id)
    if error:
        return error
    if row.status != 'pending':
        return jsonify({'success': False, 'error': 'This request has already been reviewed'}), 409
    admin = _telegram_operations_admin()
    if action == 'complete' and row.request_type == 'renewal':
        from telegram_bot_worker import _execute_renewal_request
        renewed, result = _execute_renewal_request(row, admin)
        if not renewed:
            return jsonify({'success': False, 'error': str(result),
                            'item': _serialize_telegram_service(row)}), 409
    row.status = 'completed' if action == 'complete' else 'rejected'
    row.reviewed_by_admin_id = admin.id
    row.reviewed_at = datetime.utcnow()
    if action == 'reject' and row.request_type == 'renewal':
        _refund_wallet_request(row, 'renewal')
    _log_audit(f'telegram_service.{action}', row, actor=admin)
    db.session.commit()
    _telegram_operation_notify(row)
    return jsonify({'success': True, 'item': _serialize_telegram_service(row)})


@bp.route('/api/telegram-operations/services/<int:request_id>/triage', methods=['POST'])
@limiter.limit('20 per minute')
@login_required
def triage_telegram_support_operation(request_id):
    row, error = _telegram_service_for_admin(request_id)
    if error:
        return error
    if row.request_type != 'support' or row.status != 'pending':
        return jsonify({'success': False, 'error': 'Only open support tickets can be triaged'}), 409
    data = request.get_json(silent=True) or {}
    if 'support_priority' in data:
        priority = str(data.get('support_priority') or '').strip().lower()
        if priority not in ('low', 'normal', 'high', 'urgent'):
            return jsonify({'success': False, 'error': 'Invalid support priority'}), 400
        row.support_priority = priority
    if 'assigned_admin_id' in data:
        raw_assignee = data.get('assigned_admin_id')
        if raw_assignee in (None, ''):
            row.assigned_admin_id = None
        else:
            try:
                assignee_id = int(raw_assignee)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'Invalid support assignee'}), 400
            allowed_ids = {item['id'] for item in _support_assignable_operators(row)}
            if assignee_id not in allowed_ids:
                return jsonify({'success': False, 'error': 'This operator cannot be assigned to the ticket'}), 403
            row.assigned_admin_id = assignee_id
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'item': _serialize_telegram_service(row)})


@bp.route('/api/telegram-operations/services/<int:request_id>/reply', methods=['POST'])
@limiter.limit('20 per minute')
@login_required
def reply_telegram_support_operation(request_id):
    from app import (  # deferred: app-level helper, avoids circular import
        _telegram_bot_api_client, _telegram_operation_identity, _telegram_operations_admin,
    )
    row, error = _telegram_service_for_admin(request_id)
    if error:
        return error
    if row.request_type != 'support':
        return jsonify({'success': False, 'error': 'Replies are only available for support requests'}), 409
    if row.status != 'pending':
        return jsonify({'success': False, 'error': 'This support request is closed'}), 409
    is_multipart = bool(request.content_type and request.content_type.startswith('multipart/form-data'))
    data = request.form if is_multipart else (request.get_json(silent=True) or {})
    message = str(data.get('message') or '').strip()
    attachment = request.files.get('attachment') if is_multipart else None
    if (not message and not attachment) or len(message) > 4000:
        return jsonify({'success': False, 'error': 'Add a reply or attachment (maximum 4000 characters)'}), 400
    admin = _telegram_operations_admin()
    identity = _telegram_operation_identity(row.telegram_user_id, row.customer_id)
    bot = db.session.get(TelegramBotInstance, row.bot_instance_id)
    if not identity or not identity.telegram_chat_id or not bot:
        return jsonify({'success': False, 'error': 'Customer Telegram chat is unavailable'}), 409
    complete = False
    try:
        attachment_values = {}
        if attachment:
            filename = secure_filename(attachment.filename or '') or 'support-attachment'
            content_type = str(attachment.mimetype or 'application/octet-stream').lower()
            if content_type not in TELEGRAM_SUPPORT_ALLOWED_TYPES:
                return jsonify({'success': False, 'error': 'Unsupported attachment type'}), 400
            content = attachment.stream.read(TELEGRAM_SUPPORT_ATTACHMENT_MAX_BYTES + 1)
            if not content or len(content) > TELEGRAM_SUPPORT_ATTACHMENT_MAX_BYTES:
                return jsonify({'success': False, 'error': 'Attachment must be between 1 byte and 20 MB'}), 400
            as_photo = content_type.startswith('image/') and content_type != 'image/gif' and len(content) <= 10 * 1024 * 1024
            result, _route_name = _telegram_bot_api_client(bot).send_upload(
                identity.telegram_chat_id, content, filename, content_type,
                as_photo=as_photo, caption=message,
            )
            attachment_values = _telegram_support_attachment_from_result(
                result,
                fallback_kind=('photo' if as_photo else 'document'),
                fallback_name=filename,
                fallback_mime=content_type,
                fallback_size=len(content),
            )
            if not attachment_values.get('attachment_file_id'):
                raise ValueError('Telegram did not return an attachment file ID')
            attachment_values['source_chat_id'] = int(identity.telegram_chat_id)
        elif not _telegram_operation_notify(row, reply=message):
            return jsonify({'success': False, 'error': 'Telegram delivery failed; reply was not saved'}), 502
        db.session.add(TelegramServiceRequestMessage(
            request_id=row.id, sender_type='admin', admin_id=admin.id,
            message=message, **attachment_values,
        ))
        if not row.assigned_admin_id:
            row.assigned_admin_id = admin.id
        if not row.first_response_at:
            row.first_response_at = datetime.utcnow()
        row.updated_at = datetime.utcnow()
        complete = str(data.get('complete') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        if complete:
            row.status = 'completed'
            row.reviewed_by_admin_id = admin.id
            row.reviewed_at = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': redact_connection_error(exc)}), 502
    if complete:
        _telegram_operation_notify(row)
    return jsonify({'success': True, 'item': _serialize_telegram_service(row)})


def _telegram_promo_from_payload(promo: TelegramPromo, data: dict):
    name = str(data.get('name') or promo.name or '').strip()[:120]
    if not name:
        raise ValueError('Promo name is required')
    promo.name = name
    promo.description = str(data.get('description') or '').strip()[:2000] or None
    code = str(data.get('code') or '').strip().upper()
    if code:
        if not re.fullmatch(r'[A-Z0-9_-]{3,64}', code):
            raise ValueError('Promo code must be 3-64 characters of A-Z, 0-9, underscore, or dash')
        clash = TelegramPromo.query.filter(
            TelegramPromo.code == code, TelegramPromo.id != (promo.id or 0)).first()
        if clash:
            raise ValueError('This promo code is already in use')
    promo.code = code or None
    kind = str(data.get('kind') or promo.kind or 'percent').strip().lower()
    if kind not in ('percent', 'fixed'):
        raise ValueError('Promo kind must be percent or fixed')
    promo.kind = kind
    try:
        promo.value = float(data.get('value'))
    except (TypeError, ValueError):
        raise ValueError('Promo value must be a number')
    if promo.value < 0 or (kind == 'percent' and promo.value > 100):
        raise ValueError('Promo value is out of range')
    applies_to = str(data.get('applies_to') or promo.applies_to or 'both').strip().lower()
    if applies_to not in ('purchase', 'renewal', 'both'):
        raise ValueError('Invalid applies_to value')
    promo.applies_to = applies_to

    def _int_or_none(key, minimum=0):
        value = data.get(key)
        if value in (None, ''):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f'{key} must be a whole number')
        if value < minimum:
            raise ValueError(f'{key} is out of range')
        return value

    promo.max_discount_amount = _int_or_none('max_discount_amount')
    promo.min_amount = _int_or_none('min_amount')
    promo.min_purchases_30d = _int_or_none('min_purchases_30d')
    promo.min_purchases_90d = _int_or_none('min_purchases_90d')
    promo.min_referrals = _int_or_none('min_referrals')
    promo.requires_channel_chat_id = _int_or_none('requires_channel_chat_id')
    promo.max_uses_total = _int_or_none('max_uses_total', minimum=1)
    promo.max_uses_per_user = _int_or_none('max_uses_per_user', minimum=1)
    promo.priority = _int_or_none('priority') or 0
    for field, model, label in (
            ('bot_instance_id', TelegramBotInstance, 'bot'),
            ('package_id', Package, 'package'),
            ('owner_admin_id', Admin, 'owner')):
        raw = data.get(field)
        if raw in (None, ''):
            setattr(promo, field, None)
            continue
        try:
            row = db.session.get(model, int(raw))
        except (TypeError, ValueError):
            row = None
        if row is None:
            raise ValueError(f'Invalid {label} reference')
        setattr(promo, field, row.id)
    for field in ('starts_at', 'ends_at'):
        raw = str(data.get(field) or '').strip()
        if not raw:
            setattr(promo, field, None)
            continue
        try:
            setattr(promo, field, datetime.fromisoformat(raw.replace('Z', '+00:00')).replace(tzinfo=None))
        except ValueError:
            raise ValueError(f'{field} must be an ISO datetime')
    if promo.starts_at and promo.ends_at and promo.ends_at <= promo.starts_at:
        raise ValueError('The promo window end must be after its start')
    promo.first_purchase_only = bool(data.get('first_purchase_only', False))
    promo.stackable = bool(data.get('stackable', False))
    promo.apply_on_reseller_pricing = bool(data.get('apply_on_reseller_pricing', True))
    promo.enabled = bool(data.get('enabled', True))


@bp.route('/api/settings/telegram-promos', methods=['GET'])
@superadmin_required
def list_telegram_promos():
    promos = TelegramPromo.query.order_by(TelegramPromo.id.desc()).all()
    stats = {
        promo_id: (uses, discounted)
        for promo_id, uses, discounted in db.session.query(
            TelegramPromoUse.promo_id,
            func.count(TelegramPromoUse.id),
            func.coalesce(func.sum(TelegramPromoUse.amount_discounted), 0),
        ).group_by(TelegramPromoUse.promo_id).all()
    } if promos else {}
    payload = []
    for promo in promos:
        item = promo.to_safe_dict()
        uses, discounted = stats.get(promo.id, (0, 0))
        item['uses'] = int(uses or 0)
        item['discounted_total'] = int(discounted or 0)
        payload.append(item)
    return jsonify({'success': True, 'promos': payload})


@bp.route('/api/settings/telegram-promos', methods=['POST'])
@superadmin_required
def create_telegram_promo():
    from app import _log_audit  # deferred: app-level helper, avoids circular import
    promo = TelegramPromo(name='')
    try:
        _telegram_promo_from_payload(promo, request.get_json(silent=True) or {})
        db.session.add(promo)
        _log_audit('telegram_promo.create', promo,
                   actor=db.session.get(Admin, session['admin_id']))
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'promo': promo.to_safe_dict()}), 201


@bp.route('/api/settings/telegram-promos/<int:promo_id>', methods=['PUT', 'DELETE'])
@superadmin_required
def update_telegram_promo(promo_id):
    from app import _log_audit  # deferred: app-level helper, avoids circular import
    promo = db.session.get(TelegramPromo, promo_id)
    if not promo:
        return jsonify({'success': False, 'error': 'Promo not found'}), 404
    if request.method == 'DELETE':
        TelegramPromoUse.query.filter_by(promo_id=promo.id).delete(synchronize_session=False)
        _log_audit('telegram_promo.delete', ('TelegramPromo', promo.id),
                   actor=db.session.get(Admin, session['admin_id']),
                   meta={'code': promo.code, 'name': promo.name})
        db.session.delete(promo)
        db.session.commit()
        return jsonify({'success': True})
    try:
        _telegram_promo_from_payload(promo, request.get_json(silent=True) or {})
        _log_audit('telegram_promo.update', promo,
                   actor=db.session.get(Admin, session['admin_id']))
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'promo': promo.to_safe_dict()})


def _telegram_announcement_filters(data, actor=None):
    from app import _telegram_announcement_datetime  # deferred: app-level helper, avoids circular import
    source = data if isinstance(data, dict) else {}
    filters = {
        'bot_scope': str(source.get('bot_scope') or 'all').strip().lower(),
        'bot_ids': sorted({int(v) for v in (source.get('bot_ids') or []) if str(v).isdigit()}),
        'server_ids': sorted({int(v) for v in (source.get('server_ids') or []) if str(v).isdigit()}),
        'linked_only': bool(source.get('linked_only')),
        'event_match': 'all' if source.get('event_match') == 'all' else 'any',
    }
    if filters['bot_scope'] not in ('all', 'central', 'reseller', 'selected'):
        raise ValueError('Invalid bot scope')
    for event in ('started', 'purchased', 'renewed'):
        start = _telegram_announcement_datetime(source.get(f'{event}_from'))
        end = _telegram_announcement_datetime(source.get(f'{event}_to'))
        if start and end and start > end:
            raise ValueError(f'{event} date range is invalid')
        filters[f'{event}_from'] = start.isoformat() if start else None
        filters[f'{event}_to'] = end.isoformat() if end else None
    if actor and actor.role == 'reseller' and not actor.is_superadmin:
        owned = TelegramBotInstance.query.filter_by(owner_admin_id=actor.id).first()
        if not owned:
            raise ValueError('No Telegram bot is assigned to this reseller')
        filters['bot_scope'], filters['bot_ids'] = 'selected', [owned.id]
    return filters


@bp.route('/api/telegram-announcements', methods=['GET', 'POST'])
@login_required
def telegram_announcements_api():
    actor = db.session.get(Admin, session['admin_id'])
    if not actor or actor.role not in ('superadmin', 'reseller'):
        return jsonify({'success': False, 'error': 'Access Denied'}), 403
    if request.method == 'GET':
        query = TelegramAnnouncement.query
        if actor.role == 'reseller' and not actor.is_superadmin:
            query = query.filter_by(created_by_admin_id=actor.id)
        rows = query.order_by(TelegramAnnouncement.id.desc()).limit(100).all()
        return jsonify({'success': True, 'announcements': [row.to_safe_dict() for row in rows]})
    data = request.get_json(silent=True) or {}
    title = str(data.get('title') or '').strip()[:160]
    message = str(data.get('message_text') or '').strip()
    if not title or not message or len(message) > 4096:
        return jsonify({'success': False, 'error': 'Title and a message up to 4096 characters are required'}), 400
    try:
        filters = _telegram_announcement_filters(data.get('filters'), actor)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    row = TelegramAnnouncement(title=title, message_text=message,
        filters_json=json.dumps(filters, separators=(',', ':'), sort_keys=True),
        created_by_admin_id=actor.id)
    db.session.add(row)
    db.session.commit()
    return jsonify({'success': True, 'announcement': row.to_safe_dict()}), 201


@bp.route('/api/telegram-announcements/preview', methods=['POST'])
@login_required
def preview_telegram_announcement():
    from app import _telegram_announcement_recipients  # deferred: app-level helper, avoids circular import
    actor = db.session.get(Admin, session['admin_id'])
    if not actor or actor.role not in ('superadmin', 'reseller'):
        return jsonify({'success': False, 'error': 'Access Denied'}), 403
    try:
        filters = _telegram_announcement_filters((request.get_json(silent=True) or {}).get('filters'), actor)
        recipients = _telegram_announcement_recipients(filters)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    by_bot = {}
    for item in recipients:
        by_bot[str(item['bot_instance_id'])] = by_bot.get(str(item['bot_instance_id']), 0) + 1
    return jsonify({'success': True, 'count': len(recipients), 'by_bot': by_bot})


@bp.route('/api/telegram-announcements/<int:announcement_id>/<action>', methods=['POST'])
@login_required
def mutate_telegram_announcement(announcement_id, action):
    from app import _log_audit, _queue_telegram_announcement  # deferred: app-level helper, avoids circular import
    actor = db.session.get(Admin, session['admin_id'])
    row = db.session.get(TelegramAnnouncement, announcement_id)
    if not actor or not row or (actor.role == 'reseller' and not actor.is_superadmin and row.created_by_admin_id != actor.id):
        return jsonify({'success': False, 'error': 'Announcement not found'}), 404
    try:
        if action in ('send', 'resume'):
            _queue_telegram_announcement(row)
        elif action == 'pause' and row.status in ('queued', 'sending'):
            row.status = 'paused'
        elif action == 'cancel' and row.status not in ('completed', 'cancelled'):
            row.status = 'cancelled'
        else:
            raise ValueError('Action is not valid for the current status')
        _log_audit(f'telegram_announcement.{action}', row, actor=actor)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 409
    return jsonify({'success': True, 'announcement': row.to_safe_dict()})


@bp.route('/api/telegram-bots', methods=['GET'])
@limiter.limit('60 per minute')
@login_required
def list_telegram_bots():
    from app import _telegram_bot_health  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    if user.role == 'superadmin' or user.is_superadmin:
        query = TelegramBotInstance.query
        if request.args.get('include_archived') != '1':
            query = query.filter(TelegramBotInstance.archived_at.is_(None))
        bots = query.order_by(TelegramBotInstance.id.asc()).all()
    elif user.role == 'reseller':
        bots = TelegramBotInstance.query.filter(
            TelegramBotInstance.owner_admin_id == user.id,
            TelegramBotInstance.archived_at.is_(None),
        ).order_by(TelegramBotInstance.id.asc()).all()
    else:
        return jsonify({'success': False, 'error': 'Access Denied'}), 403
    owner_names = {
        admin.id: admin.username
        for admin in Admin.query.filter(Admin.id.in_(
            [bot.owner_admin_id for bot in bots if bot.owner_admin_id])).all()
    } if bots else {}
    payload = []
    for bot in bots:
        item = bot.to_safe_dict()
        item['owner_username'] = owner_names.get(bot.owner_admin_id)
        item['runtime'] = bot.runtime.to_safe_dict() if bot.runtime else None
        item['health'] = _telegram_bot_health(bot)
        payload.append(item)
    return jsonify({'success': True, 'bots': payload})


@bp.route('/api/telegram-bots', methods=['POST'])
@limiter.limit('20 per minute')
@login_required
def create_telegram_bot():
    from app import (  # deferred: app-level helper, avoids circular import
        _encrypt_telegram_secret, _telegram_bot_token_conflict, _validate_telegram_token,
    )
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    if user.role == 'superadmin' or user.is_superadmin:
        try:
            owner_id = int(data.get('owner_admin_id'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'A reseller owner is required'}), 400
        owner = db.session.get(Admin, owner_id)
        if not owner or owner.role != 'reseller':
            return jsonify({'success': False, 'error': 'Owner must be a reseller'}), 400
    elif user.role == 'reseller':
        owner = user
    else:
        return jsonify({'success': False, 'error': 'Access Denied'}), 403
    scope_key = f'reseller:{owner.id}'
    if TelegramBotInstance.query.filter_by(scope_key=scope_key).first():
        return jsonify({'success': False, 'error': 'This reseller already has a bot'}), 409
    display_name = str(data.get('display_name') or '').strip()[:120] or owner.username
    token = str(data.get('bot_token') or '').strip()
    if token and not _validate_telegram_token(token):
        return jsonify({'success': False, 'error': 'Bot token format is invalid'}), 400
    enabled = bool(data.get('enabled'))
    if enabled and not token:
        return jsonify({'success': False, 'error': 'Configure a bot token before enabling the bot'}), 400
    bot = TelegramBotInstance(
        scope_key=scope_key,
        owner_type='reseller',
        owner_admin_id=owner.id,
        display_name=display_name,
        enabled=enabled,
        transport_mode='polling',
    )
    if token:
        if _telegram_bot_token_conflict(bot, token=token):
            return jsonify({'success': False, 'error': 'This token is already used by another bot'}), 409
        try:
            bot.token_encrypted = _encrypt_telegram_secret(token)
        except RuntimeError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
    db.session.add(bot)
    db.session.commit()
    return jsonify({'success': True, 'bot': bot.to_safe_dict()}), 201


@bp.route('/api/telegram-bots/<int:bot_id>/runtime', methods=['POST'])
@limiter.limit('20 per minute')
@login_required
def telegram_bot_runtime_action(bot_id):
    from app import _log_audit, _telegram_bot_health, _telegram_bot_manageable_by  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    bot = db.session.get(TelegramBotInstance, int(bot_id))
    if bot is None:
        return jsonify({'success': False, 'error': 'Telegram bot not found'}), 404
    is_superadmin = bool(user.role == 'superadmin' or user.is_superadmin)
    action = str((request.get_json(silent=True) or {}).get('action') or '').strip().lower()
    if action not in ('enable', 'disable', 'restart', 'archive', 'restore'):
        return jsonify({'success': False, 'error': 'Invalid runtime action'}), 400
    if action in ('archive', 'restore'):
        if not is_superadmin:
            return jsonify({'success': False, 'error': 'Access Denied: SuperAdmin only'}), 403
    elif not _telegram_bot_manageable_by(user, bot):
        return jsonify({'success': False, 'error': 'Access Denied: not the bot owner'}), 403

    if action == 'enable':
        if bot.archived_at is not None:
            return jsonify({'success': False, 'error': 'Restore the archived bot before enabling it'}), 409
        if not bot.token_encrypted:
            return jsonify({'success': False, 'error': 'Configure a bot token before enabling the bot'}), 400
        bot.enabled = True
    elif action == 'disable':
        bot.enabled = False
    elif action == 'restart':
        if bot.archived_at is not None:
            return jsonify({'success': False, 'error': 'This bot is archived'}), 409
        runtime = bot.runtime
        if runtime:
            runtime.worker_id = None
            runtime.lease_expires_at = None
            runtime.status = 'stopped'
            runtime.failed_update_count = 0
    elif action == 'archive':
        if bot.owner_type == 'system':
            return jsonify({'success': False, 'error': 'The system bot cannot be archived'}), 400
        if bot.archived_at is not None:
            return jsonify({'success': False, 'error': 'This bot is already archived'}), 409
        pending_purchases = TelegramPurchaseRequest.query.filter_by(
            bot_instance_id=bot.id, status='pending').count()
        pending_services = TelegramServiceRequest.query.filter_by(
            bot_instance_id=bot.id, status='pending').count()
        if pending_purchases or pending_services:
            return jsonify({
                'success': False,
                'error': 'Resolve the pending requests before archiving this bot',
                'pending_purchases': pending_purchases,
                'pending_service_requests': pending_services,
            }), 409
        bot.enabled = False
        bot.archived_at = datetime.utcnow()
        bot.archived_by_admin_id = user.id
        # Free the scope_key so the reseller can get a fresh bot.
        bot.scope_key = f'{bot.scope_key}:archived:{bot.id}'
    elif action == 'restore':
        if bot.archived_at is None:
            return jsonify({'success': False, 'error': 'This bot is not archived'}), 409
        original_scope = str(bot.scope_key or '').partition(':archived:')[0]
        if TelegramBotInstance.query.filter(
                TelegramBotInstance.id != bot.id,
                TelegramBotInstance.scope_key == original_scope).first():
            return jsonify({'success': False, 'error': 'Another bot already uses this reseller scope'}), 409
        bot.scope_key = original_scope
        bot.archived_at = None
        bot.archived_by_admin_id = None
    _log_audit(f'telegram_bot.{action}', bot, actor=user,
               meta={'bot_id': bot.id, 'scope_key': bot.scope_key})
    db.session.commit()
    return jsonify({'success': True, 'bot': bot.to_safe_dict(), 'health': _telegram_bot_health(bot)})


@bp.route('/api/settings/telegram-bots', methods=['GET'])
@login_required
def get_telegram_bot_settings():
    from app import (  # deferred: app-level helper, avoids circular import
        _requested_telegram_bot, _telegram_customer_inbounds, _telegram_purchase_packages_payload, _telegram_purchase_policy, _telegram_purchase_routes_payload, _telegram_purchase_servers_payload,
    )
    bot, error = _requested_telegram_bot()
    if error:
        return error
    purchase_policy = _telegram_purchase_policy(bot, create=True)
    runtime = TelegramBotRuntime.query.filter_by(bot_instance_id=bot.id).first()
    if runtime is None:
        runtime = TelegramBotRuntime(bot_instance_id=bot.id)
        db.session.add(runtime)
        db.session.commit()
    proxies = TelegramProxyEndpoint.query.filter_by(bot_instance_id=bot.id).order_by(
        TelegramProxyEndpoint.priority.asc(), TelegramProxyEndpoint.id.asc(),
    ).all()
    egresses = TelegramEgressProfile.query.filter_by(bot_instance_id=bot.id).filter(
        TelegramEgressProfile.source_type != 'telegram_backup_account',
    ).order_by(
        TelegramEgressProfile.priority.asc(), TelegramEgressProfile.id.asc(),
    ).all()
    test_users = TelegramBotTestUser.query.filter_by(bot_instance_id=bot.id).order_by(
        TelegramBotTestUser.id.asc(),
    ).all()
    db.session.commit()
    from telegram_bot_runtime import (
        COPY as TELEGRAM_BOT_COPY,
        HIDEABLE_MENU_KEYS,
        parse_copy_overrides,
    )
    start_events = TelegramBotStartEvent.query.filter_by(bot_instance_id=bot.id)
    start_stats = {
        'total': start_events.count(),
        'new_users': start_events.filter_by(is_new_user=True).count(),
        'existing_users': start_events.filter_by(is_new_user=False).count(),
        'unique_users': db.session.query(TelegramBotStartEvent.telegram_user_id).filter_by(
            bot_instance_id=bot.id).distinct().count(),
    }
    return jsonify({'success': True, 'bot': bot.to_safe_dict(),
                    'start_stats': start_stats,
                    'copy_overrides': parse_copy_overrides(bot),
                    'copy_defaults': TELEGRAM_BOT_COPY,
                    'hideable_keys': list(HIDEABLE_MENU_KEYS),
                    'runtime': runtime.to_safe_dict(),
                    'purchase_policy': purchase_policy.to_safe_dict(),
                    'purchase_servers': _telegram_purchase_servers_payload(bot),
                    'purchase_packages': _telegram_purchase_packages_payload(bot),
                    'trial_packages': [{
                        'id': package.id,
                        'name': package.name,
                        'days': int(package.days or 0),
                        'volume': int(package.volume or 0),
                    } for package in Package.query.filter_by(enabled=True, is_trial=True).order_by(
                        Package.id.asc()).all()],
                    'purchase_inbound_routes': _telegram_purchase_routes_payload(bot),
                    'purchase_inbounds': {
                        str(server.id): _telegram_customer_inbounds(server.id)
                        for server in Server.query.filter_by(enabled=True, hidden=False).all()
                    },
                    'test_users': [row.to_safe_dict() for row in test_users],
                    'proxies': [proxy.to_safe_dict() for proxy in proxies],
                    'egress_profiles': [profile.to_safe_dict() for profile in egresses]})


@bp.route('/api/settings/telegram-bots', methods=['POST'])
@login_required
def save_telegram_bot_settings():
    from app import _requested_telegram_bot, _save_telegram_bot_settings  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    return _save_telegram_bot_settings(bot, request.get_json(silent=True) or {})


@bp.route('/api/settings/telegram-bots/purchase-routes/detect', methods=['POST'])
@superadmin_required
def detect_telegram_purchase_routes():
    from app import _detect_telegram_inbound_profiles  # deferred: app-level helper, avoids circular import
    data = request.get_json(silent=True) or {}
    try:
        server_id = int(data.get('server_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid server ID'}), 400
    server = Server.query.filter_by(id=server_id, enabled=True, hidden=False).first()
    if not server:
        return jsonify({'success': False, 'error': 'Server is unavailable'}), 404
    try:
        profiles = _detect_telegram_inbound_profiles(server)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({
        'success': True,
        'server_id': server.id,
        'panel_generation': 'v3+',
        'profiles': profiles,
        'sampled_clients': sum(profile['client_count'] for profile in profiles),
    })


@bp.route('/api/settings/telegram-bots/test', methods=['POST'])
@login_required
def test_telegram_bot_settings():
    from app import _requested_telegram_bot, _telegram_bot_diagnostic  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    route = str((request.get_json(silent=True) or {}).get('route') or 'configured').strip().lower()
    if route not in ('direct', 'configured'):
        return jsonify({'success': False, 'error': 'Invalid diagnostic route'}), 400
    return jsonify(_telegram_bot_diagnostic(bot, route=route))


def _telegram_test_user_id(value):
    try:
        user_id = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError('Telegram user ID must be a number')
    if user_id <= 0 or user_id > 9223372036854775807:
        raise ValueError('Telegram user ID is out of range')
    return user_id


@bp.route('/api/settings/telegram-bots/test-users', methods=['POST'])
@login_required
def create_telegram_bot_test_user():
    from app import _requested_telegram_bot  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        row = TelegramBotTestUser(
            bot_instance_id=bot.id,
            telegram_user_id=_telegram_test_user_id(data.get('telegram_user_id')),
            label=str(data.get('label') or '').strip()[:120] or None,
            enabled=bool(data.get('enabled', True)),
        )
        db.session.add(row)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'This Telegram user is already in the test list'}), 409
    return jsonify({'success': True, 'test_user': row.to_safe_dict()})


@bp.route('/api/settings/telegram-bots/test-users/<int:test_user_id>', methods=['PUT', 'DELETE'])
@login_required
def update_telegram_bot_test_user(test_user_id):
    from app import _requested_telegram_bot  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    row = TelegramBotTestUser.query.filter_by(
        id=test_user_id, bot_instance_id=bot.id,
    ).first()
    if not row:
        return jsonify({'success': False, 'error': 'Test user not found'}), 404
    if request.method == 'DELETE':
        db.session.delete(row)
        db.session.commit()
        return jsonify({'success': True})
    data = request.get_json(silent=True) or {}
    try:
        if 'telegram_user_id' in data:
            row.telegram_user_id = _telegram_test_user_id(data.get('telegram_user_id'))
        if 'label' in data:
            row.label = str(data.get('label') or '').strip()[:120] or None
        if 'enabled' in data:
            row.enabled = bool(data.get('enabled'))
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'This Telegram user is already in the test list'}), 409
    return jsonify({'success': True, 'test_user': row.to_safe_dict()})


@bp.route('/api/settings/telegram-bots/send-test', methods=['POST'])
@login_required
def send_telegram_bot_test_message():
    from app import _requested_telegram_bot, _telegram_bot_api_client  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        if not bot.enabled:
            return jsonify({
                'success': False,
                'error': 'Enable the Telegram bot and save settings before testing',
            }), 400
        user_id = _telegram_test_user_id(data.get('telegram_user_id'))
        if bot.test_mode and not TelegramBotTestUser.query.filter_by(
                bot_instance_id=bot.id, telegram_user_id=user_id, enabled=True).first():
            return jsonify({'success': False, 'error': 'Add and enable this user in the test list first'}), 400
        api = _telegram_bot_api_client(bot)
        _result, route = api.send_message(
            user_id,
            '✅ Eve Telegram bot test succeeded.\n\nتست ربات تلگرام Eve با موفقیت انجام شد.',
        )
        return jsonify({'success': True, 'route': route})
    except (ValueError, RuntimeError) as exc:
        return jsonify({'success': False, 'error': redact_connection_error(exc)}), 400


@bp.route('/api/settings/telegram-bots/proxies', methods=['POST'])
@login_required
def create_telegram_bot_proxy():
    from app import _requested_telegram_bot, _telegram_proxy_from_payload  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    proxy = TelegramProxyEndpoint(bot_instance_id=bot.id)
    try:
        _telegram_proxy_from_payload(proxy, request.get_json(silent=True) or {})
        db.session.add(proxy)
        db.session.commit()
    except (ValueError, RuntimeError) as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'This proxy already exists'}), 409
    return jsonify({'success': True, 'proxy': proxy.to_safe_dict()})


@bp.route('/api/settings/telegram-bots/proxies/<int:proxy_id>', methods=['PUT', 'DELETE'])
@login_required
def update_telegram_bot_proxy(proxy_id):
    from app import _requested_telegram_bot, _telegram_proxy_from_payload  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    proxy = TelegramProxyEndpoint.query.filter_by(id=proxy_id, bot_instance_id=bot.id).first()
    if not proxy:
        return jsonify({'success': False, 'error': 'Proxy not found'}), 404
    if request.method == 'DELETE':
        db.session.delete(proxy)
        db.session.commit()
        return jsonify({'success': True})
    try:
        _telegram_proxy_from_payload(proxy, request.get_json(silent=True) or {})
        db.session.commit()
    except (ValueError, RuntimeError) as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'This proxy already exists'}), 409
    return jsonify({'success': True, 'proxy': proxy.to_safe_dict()})


@bp.route('/api/settings/telegram-bots/proxies/<int:proxy_id>/test', methods=['POST'])
@login_required
def test_telegram_bot_proxy(proxy_id):
    from app import _requested_telegram_bot, _telegram_bot_diagnostic  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    proxy = TelegramProxyEndpoint.query.filter_by(id=proxy_id, bot_instance_id=bot.id).first()
    if not proxy:
        return jsonify({'success': False, 'error': 'Proxy not found'}), 404
    return jsonify(_telegram_bot_diagnostic(bot, only_proxy_id=proxy.id))


@bp.route('/api/settings/telegram-bots/egress/candidates', methods=['GET'])
@superadmin_required
def get_telegram_egress_candidates():
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, TELEGRAM_EGRESS_PROTOCOLS, load_snapshot_from_redis,
    )
    load_snapshot_from_redis()
    servers = {row.id: row for row in Server.query.filter_by(enabled=True).all()}
    candidates = []
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        server_id = int(inbound.get('server_id') or 0)
        server = servers.get(server_id)
        protocol = str(inbound.get('protocol') or '').lower()
        if not server or protocol not in TELEGRAM_EGRESS_PROTOCOLS:
            continue
        inbound_enabled = inbound.get('enable', inbound.get('enabled', True))
        if inbound_enabled in (False, 0, '0', 'false', 'False'):
            continue
        stream = inbound.get('streamSettings') or {}
        if isinstance(stream, str):
            try:
                stream = json.loads(stream)
            except (TypeError, ValueError):
                stream = {}
        if not stream:
            stream = {
                'network': inbound.get('network') or 'tcp',
                'security': inbound.get('security') or 'none',
            }
        for client in (inbound.get('clients') or []):
            # The UUID/password is connection material. The browser receives only
            # the human-facing email and sends it back as the lookup reference.
            client_email = str(client.get('email') or '').strip()
            if not client_email:
                continue
            raw_client = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
            client_enabled = raw_client.get('enable', client.get('enable', True))
            if client_enabled in (False, 0, '0', 'false', 'False'):
                continue
            try:
                expiry_ms = int(raw_client.get('expiryTime') or 0)
            except (TypeError, ValueError):
                expiry_ms = 0
            if expiry_ms > 0 and expiry_ms <= int(time.time() * 1000):
                continue
            try:
                total_bytes = int(raw_client.get('totalGB') or client.get('totalGB') or 0)
                used_bytes = int(client.get('up') or 0) + int(client.get('down') or 0)
            except (TypeError, ValueError):
                total_bytes = used_bytes = 0
            if total_bytes > 0 and used_bytes >= total_bytes:
                continue
            uri = generate_client_link(client, inbound, server.host)
            if not uri:
                continue
            try:
                build_xray_config_from_uri(uri, 12080)
            except (TypeError, ValueError):
                continue
            candidates.append({
                'server_id': server.id, 'server_name': server.name,
                'inbound_id': int(inbound.get('id')), 'inbound_remark': inbound.get('remark'),
                'protocol': protocol, 'network': stream.get('network') or 'tcp',
                'security': stream.get('security') or 'none',
                'account_key': f'{server.id}:{client_email.lower()}',
                'client_id': client_email, 'client_email': client_email,
            })
            if len(candidates) >= 5000:
                break
        if len(candidates) >= 5000:
            break
    return jsonify({'success': True, 'candidates': candidates})


@bp.route('/api/settings/telegram-bots/egress', methods=['POST'])
@login_required
def create_telegram_egress_profile():
    from app import _requested_telegram_bot  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    profile = TelegramEgressProfile(bot_instance_id=bot.id, name='Managed Xray')
    try:
        _telegram_egress_from_payload(profile, request.get_json(silent=True) or {})
        db.session.add(profile)
        db.session.commit()
    except (ValueError, RuntimeError) as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': redact_connection_error(exc)}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'This local SOCKS port is already assigned'}), 409
    return jsonify({'success': True, 'profile': profile.to_safe_dict()})


@bp.route('/api/settings/telegram-bots/egress/<int:profile_id>', methods=['PUT', 'DELETE'])
@login_required
def update_telegram_egress_profile(profile_id):
    from app import _requested_telegram_bot  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    profile = TelegramEgressProfile.query.filter_by(id=profile_id, bot_instance_id=bot.id).first()
    if not profile:
        return jsonify({'success': False, 'error': 'Egress profile not found'}), 404
    if request.method == 'DELETE':
        db.session.delete(profile)
        db.session.commit()
        return jsonify({'success': True})
    try:
        _telegram_egress_from_payload(profile, request.get_json(silent=True) or {})
        db.session.commit()
    except (ValueError, RuntimeError) as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': redact_connection_error(exc)}), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'This local SOCKS port is already assigned'}), 409
    return jsonify({'success': True, 'profile': profile.to_safe_dict()})


@bp.route('/api/settings/telegram-bots/egress/<int:profile_id>/test', methods=['POST'])
@login_required
def test_telegram_egress_profile(profile_id):
    from app import _requested_telegram_bot, _telegram_bot_diagnostic  # deferred: app-level helper, avoids circular import
    bot, error = _requested_telegram_bot()
    if error:
        return error
    profile = TelegramEgressProfile.query.filter_by(id=profile_id, bot_instance_id=bot.id).first()
    if not profile:
        return jsonify({'success': False, 'error': 'Egress profile not found'}), 404
    if not profile.enabled:
        return jsonify({'success': False, 'error': 'Enable this managed route before testing it'}), 409

    # The dedicated worker reloads profiles asynchronously. A test immediately
    # after save/edit must never hit the previous Xray process and report a stale
    # failure for the new configuration.
    deadline = time.monotonic() + 12
    while profile.runtime_status == 'pending' and time.monotonic() < deadline:
        time.sleep(0.25)
        db.session.expire_all()
        profile = db.session.get(TelegramEgressProfile, profile_id)
        if not profile or profile.bot_instance_id != bot.id:
            return jsonify({'success': False, 'error': 'Egress profile is no longer available'}), 404
    if profile.runtime_status != 'running':
        runtime_error = redact_connection_error(profile.last_error) if profile.last_error else ''
        message = runtime_error or (
            'Xray is still preparing this route. Wait a few seconds and test again.'
            if profile.runtime_status == 'pending'
            else f'Xray route is not ready ({profile.runtime_status or "unknown"}).'
        )
        return jsonify({
            'success': False, 'error': message,
            'error_code': 'xray_route_not_ready',
            'runtime_status': profile.runtime_status,
        }), 503
    result = _telegram_bot_diagnostic(bot, only_egress_id=profile.id)
    if not result.get('success') and profile.runtime_status == 'runtime_missing':
        result['runtime_hint'] = ('Use Eve menu option [x] to install Xray, then restart '
                                  'eve-manager-telegram-egress.service')
    return jsonify(result)


@bp.route('/api/settings/telegram-backup', methods=['GET'])
@user_management_required
def get_telegram_backup_settings():
    settings = _get_telegram_backup_settings()
    return jsonify({'success': True, **settings})


@bp.route('/api/settings/telegram-backup', methods=['POST'])
@user_management_required
def save_telegram_backup_settings():
    try:
        data = request.get_json() or {}
    except Exception:
        data = {}

    enabled = bool(data.get('enabled'))
    send_panel_backup = bool(data.get('send_panel_backup'))
    schedule_mode = (data.get('schedule_mode') or 'interval').strip().lower()
    if schedule_mode not in ('interval', 'daily'):
        schedule_mode = 'interval'
    # daily_time: "HH:MM" in Tehran local time
    daily_time = (data.get('daily_time') or '00:00').strip()
    try:
        _h, _m = daily_time.split(':')
        _h = max(0, min(23, int(_h)))
        _m = max(0, min(59, int(_m)))
        daily_time = f"{_h:02d}:{_m:02d}"
    except Exception:
        daily_time = '00:00'
    interval = _parse_int(
        data.get('interval_minutes', TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES),
        TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES,
        min_value=1,
        max_value=TELEGRAM_BACKUP_MAX_INTERVAL_MINUTES
    )
    bot_token = (data.get('bot_token') or '').strip()
    chat_id = (data.get('chat_id') or '').strip()
    use_proxy = bool(data.get('use_proxy'))
    route_source = str(data.get('route_source') or '').strip().lower()
    if route_source not in ('direct', 'manual_proxy', 'panel_account'):
        route_source = 'manual_proxy' if use_proxy else 'direct'
    use_proxy = route_source != 'direct'
    proxy_mode = (data.get('proxy_mode') or 'url').strip().lower()
    if proxy_mode not in ('url', 'hostport'):
        proxy_mode = 'url'
    proxy_url = _normalize_proxy_url(data.get('proxy_url') or '')
    proxy_host = (data.get('proxy_host') or '').strip()
    proxy_port = _parse_int(data.get('proxy_port'), 0, min_value=0, max_value=65535)  # 0 = not set
    proxy_username = (data.get('proxy_username') or '').strip()
    proxy_password = (data.get('proxy_password') or '').strip()

    managed_profile = None
    if route_source == 'panel_account':
        try:
            managed_profile = _ensure_telegram_backup_egress_profile(data)
        except (ValueError, RuntimeError) as exc:
            db.session.rollback()
            return jsonify({'success': False, 'error': redact_connection_error(exc)}), 400
    elif route_source == 'manual_proxy':
        if proxy_mode == 'hostport' and (not proxy_host or not proxy_port):
            return jsonify({'success': False, 'error': 'Proxy host and port are required'}), 400
        if proxy_mode == 'url' and not proxy_url:
            return jsonify({'success': False, 'error': 'Proxy URL is required'}), 400
        _disable_telegram_backup_egress_profile()
    else:
        _disable_telegram_backup_egress_profile()

    _set_system_setting_value('telegram_backup_enabled', 'true' if enabled else 'false')
    _set_system_setting_value('telegram_backup_send_panel_backup', 'true' if send_panel_backup else 'false')
    _set_system_setting_value('telegram_backup_schedule_mode', schedule_mode)
    _set_system_setting_value('telegram_backup_daily_time', daily_time)
    _set_system_setting_value('telegram_backup_interval_minutes', str(interval))
    _set_system_setting_value('telegram_backup_bot_token', bot_token)
    _set_system_setting_value('telegram_backup_chat_id', chat_id)
    _set_system_setting_value('telegram_backup_use_proxy', 'true' if use_proxy else 'false')
    _set_system_setting_value('telegram_backup_route_source', route_source)
    if managed_profile:
        _set_system_setting_value('telegram_backup_egress_profile_id', str(managed_profile.id))
    _set_system_setting_value('telegram_backup_proxy_mode', proxy_mode)
    _set_system_setting_value('telegram_backup_proxy_url', proxy_url)
    _set_system_setting_value('telegram_backup_proxy_host', proxy_host)
    _set_system_setting_value('telegram_backup_proxy_port', str(proxy_port))
    _set_system_setting_value('telegram_backup_proxy_username', proxy_username)
    _set_system_setting_value('telegram_backup_proxy_password', proxy_password)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Telegram backup settings saved'})


@bp.route('/api/settings/telegram-backup/test', methods=['POST'])
@user_management_required
def test_telegram_backup_settings():
    from app import _telegram_get_me  # deferred: app-level helper, avoids circular import
    settings = _get_telegram_backup_settings()
    data = request.get_json(silent=True) or {}
    token = ((data.get('bot_token') if 'bot_token' in data else settings.get('bot_token')) or '').strip()
    if not token:
        return jsonify({'success': False, 'error': 'Bot token is required'}), 400

    use_proxy = bool(data.get('use_proxy')) if 'use_proxy' in data else bool(settings.get('use_proxy'))
    proxy_mode = ((data.get('proxy_mode') if 'proxy_mode' in data else settings.get('proxy_mode')) or 'url')
    proxy_url = ((data.get('proxy_url') if 'proxy_url' in data else settings.get('proxy_url')) or '')
    proxy_host = ((data.get('proxy_host') if 'proxy_host' in data else settings.get('proxy_host')) or '')
    proxy_port = _parse_int(data.get('proxy_port') if 'proxy_port' in data else settings.get('proxy_port'), 0, min_value=0, max_value=65535)
    proxy_username = ((data.get('proxy_username') if 'proxy_username' in data else settings.get('proxy_username')) or '')
    proxy_password = ((data.get('proxy_password') if 'proxy_password' in data else settings.get('proxy_password')) or '')
    route_source = str(
        data.get('route_source') if 'route_source' in data else settings.get('route_source') or ''
    ).strip().lower()
    if route_source not in ('direct', 'manual_proxy', 'panel_account'):
        route_source = 'manual_proxy' if use_proxy else 'direct'
    route_settings = {
        **settings,
        'use_proxy': route_source != 'direct',
        'route_source': route_source,
        'proxy_mode': proxy_mode,
        'proxy_url': proxy_url,
        'proxy_host': proxy_host,
        'proxy_port': proxy_port,
        'proxy_username': proxy_username,
        'proxy_password': proxy_password,
    }
    if route_source == 'panel_account' and all(
            data.get(key) not in (None, '') for key in ('server_id', 'inbound_id', 'client_id')):
        try:
            profile = _ensure_telegram_backup_egress_profile(data)
            db.session.commit()
            route_settings['managed_account'] = {'profile_id': profile.id}
        except (ValueError, RuntimeError) as exc:
            db.session.rollback()
            return jsonify({'success': False, 'error': redact_connection_error(exc)}), 400

    proxies, route_error = _telegram_backup_route_proxies(
        route_settings, wait_for_runtime=True,
    )
    if route_error:
        return jsonify({'success': False, 'error': route_error,
                        'error_code': 'backup_route_not_ready'}), 503

    try:
        resp = _telegram_get_me(token, proxies=proxies, timeout_sec=10)
    except Exception as exc:
        error_code, safe_error = classify_telegram_connection_error(
            exc, (token, proxy_username, proxy_password),
        )
        return jsonify({'success': False, 'error': safe_error,
                        'error_code': error_code}), 400

    if resp.status_code != 200:
        return jsonify({'success': False, 'error': f"HTTP {resp.status_code}"}), 400

    data, err = _safe_response_json(resp)
    if err:
        return jsonify({'success': False, 'error': err}), 400
    if isinstance(data, dict) and data.get('ok'):
        return jsonify({'success': True, 'message': 'Telegram connection OK'})
    msg = None
    if isinstance(data, dict):
        msg = data.get('description') or data.get('error')
    return jsonify({'success': False, 'error': msg or 'Telegram connection failed'}), 400


@bp.route('/api/telegram-backup/now', methods=['POST'])
@user_management_required
def telegram_backup_now():
    from app import (  # deferred: app-level helper, avoids circular import
        TELEGRAM_BACKUP_JOBS, TELEGRAM_BACKUP_JOBS_LOCK, _load_telegram_backup_jobs_locked, _prune_telegram_backup_jobs_locked, _run_telegram_backup_job, _save_telegram_backup_jobs_locked, _utc_iso_now,
    )
    # Enqueue as an async job so UI can show stage/progress.
    job_id = secrets.token_hex(8)
    job = {
        'id': job_id,
        'state': 'queued',
        'trigger': 'manual',
        'created_at': _utc_iso_now(),
        'created_at_ts': time.time(),
        'started_at': None,
        'finished_at': None,
        'stage': 'queued',
        'progress': {'total': 0, 'processed': 0},
        'error': None,
        'success_count': 0,
        'total': 0,
        'results': [],
    }
    with TELEGRAM_BACKUP_JOBS_LOCK:
        _load_telegram_backup_jobs_locked()
        TELEGRAM_BACKUP_JOBS[job_id] = job
        _prune_telegram_backup_jobs_locked()
        _save_telegram_backup_jobs_locked()

    t = threading.Thread(target=_run_telegram_backup_job, args=(job_id,), daemon=True)
    t.start()
    return jsonify({'success': True, 'job_id': job_id})


@bp.route('/api/telegram-backup/job/<job_id>', methods=['GET'])
@user_management_required
def telegram_backup_job_status(job_id):
    from app import (  # deferred: app-level helper, avoids circular import
        TELEGRAM_BACKUP_JOBS_LOCK, _load_telegram_backup_jobs_locked, _summarize_telegram_backup_job,
    )
    with TELEGRAM_BACKUP_JOBS_LOCK:
        job = _load_telegram_backup_jobs_locked().get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found'}), 404
        return jsonify({'success': True, 'job': _summarize_telegram_backup_job(job)})

def _xray_runtime_status_payload():
    """Report only the local Eve Xray runtime; never expose configuration URIs."""
    from app import XRAY_INSTALL_UNIT_PATH, find_xray_binary  # deferred: app-level helper, avoids circular import
    binary = find_xray_binary()
    version = None
    if binary:
        try:
            result = subprocess.run(
                [binary, 'version'], capture_output=True, text=True,
                timeout=5, check=False,
            )
            first_line = (result.stdout or result.stderr or '').splitlines()
            version = first_line[0].strip()[:120] if first_line else 'Installed'
        except (OSError, subprocess.SubprocessError):
            version = 'Installed'

    installing = False
    install_result = None
    if os.path.isfile(XRAY_INSTALL_UNIT_PATH):
        try:
            installing = subprocess.run(
                ['/bin/systemctl', 'is-active', '--quiet', 'eve-xray-install.service'],
                capture_output=True, timeout=3, check=False,
            ).returncode == 0
            if not installing and not binary:
                result = subprocess.run(
                    ['/bin/systemctl', 'show', '--property=Result', '--value',
                     'eve-xray-install.service'],
                    capture_output=True, text=True, timeout=3, check=False,
                )
                install_result = (result.stdout or '').strip()[:80] or None
        except (OSError, subprocess.SubprocessError):
            pass

    if binary:
        state = 'installed'
    elif installing:
        state = 'installing'
    elif install_result and install_result not in ('success', 'done'):
        state = 'failed'
    else:
        state = 'missing'
    return {
        'success': True,
        'installed': bool(binary),
        'state': state,
        'version': version,
        'install_available': os.path.isfile(XRAY_INSTALL_UNIT_PATH),
        'install_result': install_result,
    }


@bp.route('/api/settings/telegram-bots/xray-runtime', methods=['GET'])
@superadmin_required
def telegram_xray_runtime_status():
    response = jsonify(_xray_runtime_status_payload())
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/settings/telegram-bots/xray-runtime/install', methods=['POST'])
@superadmin_required
def telegram_xray_runtime_install():
    from app import XRAY_INSTALL_START_COMMAND, app  # deferred: app-level helper, avoids circular import
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'INSTALL':
        return jsonify({'success': False, 'error': 'Install confirmation is required'}), 400
    status = _xray_runtime_status_payload()
    if status['installed']:
        return jsonify({'success': True, 'state': 'installed',
                        'message': 'Xray is already installed'})
    if status['state'] == 'installing':
        return jsonify({'success': True, 'state': 'installing'}), 202
    if not status['install_available']:
        return jsonify({
            'success': False,
            'error': 'Browser Xray installer is not available; run one Eve update from SSH first.',
        }), 503
    try:
        result = subprocess.run(
            list(XRAY_INSTALL_START_COMMAND), capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.exception('Could not launch Xray installer')
        return jsonify({'success': False, 'error': str(exc)}), 500
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'systemd rejected the install').strip()
        return jsonify({'success': False, 'error': detail[:500]}), 500
    app.logger.warning(
        'Xray install started by admin_id=%s from %s',
        session.get('admin_id'), request.remote_addr,
    )
    return jsonify({'success': True, 'state': 'installing'}), 202


