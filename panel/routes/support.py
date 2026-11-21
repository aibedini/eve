"""Restricted operator workspace and employer-owned team management."""

import io
import re
import secrets
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from panel.extensions import db, limiter
from panel.models import (
    Admin, ClientPortalUser, CustomerAccount, Package, ServiceOwnership, SmsGatewayEvent, SupportActivity, SupportBudget,
    SupportChat, SupportChatMessage, SupportContract, SupportOperator, SupportPayroll, SupportRole,
    SystemSetting, TelegramBotInstance, TelegramServiceRequest, Transaction,
)
from panel.routes.common import admin_is_superadmin, current_admin, login_required
from panel.services import audit, support
from panel.services.billing import pricing_quote_revision

bp = Blueprint('support', __name__)


@bp.app_context_processor
def support_navigation():
    admin = db.session.get(Admin, session['admin_id']) if session.get('admin_id') else None
    return {'support_team_available': bool(admin and admin.enabled and admin.role == 'reseller'
                                          and support.tenant_setting(admin.id, 'delegation'))}


def restrict_support_session():
    admin_id = session.get('admin_id')
    if admin_id is None:
        return
    admin = db.session.get(Admin, admin_id)
    if not admin or admin.role != 'support':
        return
    if request.blueprint == 'support' or request.endpoint in ('static', 'auth.logout'):
        return
    if request.path.startswith('/api/'):
        return jsonify(success=False, error='Access denied'), 403
    return redirect(url_for('support.workspace'))


def _profile():
    profile = support.profile_for(current_admin())
    if not profile or (profile.owner_id is not None and not support.owner_for(profile)):
        abort(403)
    return profile


def _require(profile, capability):
    if capability not in support.capabilities_for(profile):
        abort(403)


def _lang():
    from app import _get_panel_ui_lang

    return _get_panel_ui_lang()


def _context(admin):
    return dict(admin_username=admin.username, role=admin.role, is_superadmin=admin_is_superadmin(admin),
                support_fa=_lang() == 'fa')


def _activity(operator_id, owner_id):
    return SupportActivity.query.filter_by(operator_id=operator_id, owner_id=owner_id)


def _span():
    now = datetime.utcnow()
    period = request.args.get('period', 'month')
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'day':
        start, previous = day, day - timedelta(days=1)
    elif period == 'week':
        start = day - timedelta(days=day.weekday())
        previous = start - timedelta(days=7)
    else:
        period = 'month'
        start = day.replace(day=1)
        previous = (start - timedelta(days=1)).replace(day=1)
    return period, start, now, previous, min(start, previous + (now - start))


def _metrics(rows):
    counts = dict.fromkeys(('answered', 'sms', 'delivered', 'segments', 'renewals',
                            'creations', 'free', 'gb', 'closed', 'response_minutes'), 0)
    answered = set()
    total_seconds, timed_replies = 0, 0
    for row in rows:
        if row.kind == 'sms':
            counts['sms'] += int(row.state in ('accepted', 'delivered', 'failed'))
            counts['delivered'] += int(row.state == 'delivered')
            counts['segments'] += row.details.get('segments', 0)
        elif row.state == 'completed':
            if row.kind == 'reply':
                if row.customer_id:
                    answered.add(row.customer_id)
                counts['closed'] += int(bool(row.details.get('closed')))
                seconds = row.details.get('response_seconds')
                if seconds is not None:
                    total_seconds, timed_replies = total_seconds + seconds, timed_replies + 1
            elif row.kind in ('create', 'renew'):
                free = row.details.get('free')
                key = 'renewals' if row.kind == 'renew' else ('free' if free else 'creations')
                counts[key] += 1
                if not free:
                    counts['gb'] += float(row.details.get('volume_gb', 0))
    counts['answered'] = len(answered)
    counts['gb'] = round(counts['gb'], 2)
    if timed_replies:
        counts['response_minutes'] = round(total_seconds / timed_replies / 60, 1)
    return counts


def _row_range(query, start, end):
    return query.filter(SupportActivity.created_at >= start, SupportActivity.created_at < end).order_by(
        SupportActivity.created_at, SupportActivity.id).yield_per(500)


def _locations(profile):
    return [(server_id, inbound_id) for server_id, inbounds in profile.locations.items()
            for inbound_id in inbounds if support.location_allowed(profile, int(server_id), inbound_id)]


def _request_payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    return data


def _target(profile, server_id, inbound_id, email):
    from app import _get_cached_raw_client
    from panel.core.redis_client import load_snapshot_from_redis

    load_snapshot_from_redis()
    raw = _get_cached_raw_client(server_id, inbound_id, email)
    target = support.scoped_account(profile, server_id, inbound_id, email, raw)
    if not target:
        abort(404)
    return target, raw


@bp.get('/support')
@login_required
def workspace():
    admin = current_admin()
    profile = _profile()
    allowed = support.capabilities_for(profile)
    period, start, end, previous, previous_end = _span()
    query = _activity(admin.id, profile.owner_id)
    limits = support.sms_limits(profile)
    used = [db.session.get(SupportBudget, (key, end.date()))
            for key in (f'sms:operator:{admin.id}', f'sms:team:{profile.owner_id or 0}')]
    remaining = max(0, min(limit - (row.used if row else 0) for limit, row in zip(limits, used)))
    tickets = []
    chats = []
    from panel.core.redis_client import load_snapshot_from_redis
    if {'telegram_reply', 'telegram_review', 'chat'} & allowed:
        load_snapshot_from_redis()
    if {'telegram_reply', 'telegram_review'} & allowed:
        rows = TelegramServiceRequest.query.join(ServiceOwnership).filter(
            TelegramServiceRequest.status == 'pending',
            (TelegramServiceRequest.assigned_admin_id.is_(None) |
             (TelegramServiceRequest.assigned_admin_id == admin.id)),
            ServiceOwnership.reseller_id == profile.owner_id,
            ServiceOwnership.revoked_at.is_(None),
            ServiceOwnership.server_id.in_([int(sid) for sid in profile.locations]),
        ).order_by(TelegramServiceRequest.created_at).limit(200).all()
        tickets = [row for row in rows if row.request_type in ('support', 'renewal')
                   and support.can_access_request(admin, row, _ticket_permission(row))][:25]
    if 'chat' in allowed:
        rows = db.session.query(SupportChat, ServiceOwnership).join(
            ServiceOwnership, SupportChat.service_id == ServiceOwnership.id).filter(
            SupportChat.open.is_(True), ServiceOwnership.reseller_id == profile.owner_id,
            ServiceOwnership.server_id.in_([int(sid) for sid in profile.locations]),
            (SupportChat.assigned_to.is_(None) | (SupportChat.assigned_to == admin.id)),
        ).order_by(SupportChat.updated_at).limit(200).all()
        chats = [service for _, service in rows if support.can_access_service(profile, service)][:25]
    return render_template('support_workspace.html', **_context(admin), allowed=allowed, period=period,
                           current=_metrics(_row_range(query, start, end)),
                           previous=_metrics(_row_range(query, previous, previous_end)),
                           sms_remaining=remaining, locations=_locations(profile), tickets=tickets, chats=chats)


@bp.get('/support/earnings')
@login_required
def earnings():
    admin = current_admin()
    profile = _profile()
    period, start, end, _, _ = _span()
    rows = _row_range(_activity(admin.id, profile.owner_id), start.replace(day=1), end)
    current = support.period_amount(rows, start, end,
                                    salary=support.monthly_salary(admin.id, start.date()) if period == 'month' else 0)
    settlements = SupportPayroll.query.filter_by(operator_id=admin.id, owner_id=profile.owner_id).order_by(
        SupportPayroll.period.desc()).limit(24).all()
    return render_template('support_earnings.html', **_context(admin), period=period, amounts=current,
                           settlements=settlements)


@bp.get('/api/support/customers')
@login_required
@limiter.limit('60 per minute')
def customers():
    profile = _profile()
    _require(profile, 'customers')
    from app import GLOBAL_SERVER_DATA
    from panel.core.redis_client import load_snapshot_from_redis

    search = request.args.get('q', '').strip().casefold()[:100]
    if len(search) < 2:
        return jsonify(items=[])
    load_snapshot_from_redis()
    results = []
    locations = {(int(sid), iid) for sid, iid in _locations(profile)}
    for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
        server_id, inbound_id = inbound.get('server_id'), inbound.get('id')
        if (server_id, inbound_id) not in locations:
            continue
        for entry in inbound.get('clients') or []:
            email = entry.get('email') or ''
            if search not in email.casefold():
                continue
            raw = entry.get('raw_client') or {}
            account = support.scoped_account(profile, server_id, inbound_id, email, raw)
            if not account:
                continue
            service, uuid = account
            results.append({
                'server_id': server_id, 'inbound_id': inbound_id, 'email': email,
                'enabled': bool(raw.get('enable')), 'expiry': raw.get('expiryTime'),
                'gb': round(int(raw.get('totalGB') or 0) / 1024 ** 3, 2),
                'sms': bool(service and service.customer and service.customer.phone_verified_at),
            })
            if len(results) >= 25:
                return jsonify(items=results)
    return jsonify(items=results)


def _sale_capability(kind, free):
    return f'free_{kind}' if free else kind


@bp.get('/api/support/packages/<int:server_id>/<int:inbound_id>/<kind>')
@login_required
def packages(server_id, inbound_id, kind):
    profile = _profile()
    if kind not in ('renew', 'create') or not support.location_allowed(profile, server_id, inbound_id):
        abort(404)
    if not {_sale_capability(kind, free) for free in (True, False)} & support.capabilities_for(profile):
        abort(403)
    owner = support.owner_for(profile)
    free = request.args.get('free') == '1'
    _require(profile, _sale_capability(kind, free))
    items = []
    from panel.routes.clients import _operation_pricing
    for package in Package.query.filter_by(enabled=True).order_by(Package.id).all():
        try:
            quote = _operation_pricing(current_admin(), owner, server_id, kind, package=package, free=free)
        except (PermissionError, ValueError):
            continue
        items.append(dict(id=package.id, name=package.name, days=quote['days'], gb=quote['volume_gb'],
                          price=0 if free else quote['retail'], revision=pricing_quote_revision(quote)))
    return jsonify(items=items)


@bp.post('/api/support/<int:server_id>/<int:inbound_id>/<kind>/<email>')
@login_required
@limiter.limit('30 per minute')
def account_action(server_id, inbound_id, kind, email):
    profile = _profile()
    if kind not in ('renew', 'create') or not support.location_allowed(profile, server_id, inbound_id):
        abort(404)
    data = _request_payload()
    if (set(data) - {'email', 'mode', 'package_id', 'free', 'quote_revision'}
            or data.get('mode') != 'package' or type(data.get('package_id')) is not int
            or type(data.get('free')) is not bool or not 1 <= len(email) <= 100
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9@_.+ -]{0,99}', email)
            or not re.fullmatch(r'[a-f0-9]{64}', str(data.get('quote_revision', '')))):
        abort(400)
    if data.get('email') != email:
        abort(400)
    operation_id = request.headers.get('Idempotency-Key', '')
    if not re.fullmatch(r'[a-zA-Z0-9-]{20,96}', operation_id):
        abort(400)
    _require(profile, _sale_capability(kind, data['free']))
    if kind == 'renew':
        (_, g.support_client_uuid), _ = _target(profile, server_id, inbound_id, email)
    package = db.session.get(Package, data['package_id'])
    if not package or not package.enabled:
        abort(404)
    from panel.routes.clients import _operation_pricing, add_client, renew_client
    from panel.services.client_operations import load_client_operation

    g.support_profile = profile
    existing = load_client_operation(operation_id, admin_id=profile.admin_id)
    free = data['free']
    if existing and (existing.server_id != server_id or existing.inbound_id != inbound_id
                     or existing.client_email != email):
        return jsonify(success=False, code='operation_conflict'), 409
    if not existing:
        try:
            quote = _operation_pricing(current_admin(), support.owner_for(profile), server_id, kind,
                                       package=package, free=free)
        except (ValueError, PermissionError):
            abort(403)
        if pricing_quote_revision(quote) != data['quote_revision']:
            return jsonify(success=False, code='pricing_changed'), 409
    if free and not existing:
        if not 0 < package.volume <= profile.free_max_gb:
            return jsonify(success=False, code='free_volume_limit'), 409
        if not support.reserve_daily_slot(f'free:operator:{profile.admin_id}', profile.free_limit,
                                          datetime.utcnow().date()):
            db.session.rollback()
            return jsonify(success=False, code='daily_free_limit'), 409
    # The underlying operation owns idempotency, billing, verification and rollback.
    handler = renew_client if kind == 'renew' else add_client
    response = current_app.make_response(handler(server_id, inbound_id, email) if kind == 'renew'
                                         else handler(server_id, inbound_id))
    payload = response.get_json(silent=True) or {}
    state = payload.get('final_state') or ('completed' if payload.get('success') else 'failed')
    if payload.get('success') and free and not existing:
        operation = load_client_operation(operation_id, admin_id=profile.admin_id)
        activity = SupportActivity.query.filter_by(reference=f'operation:{operation.id}').first() if operation else None
        if activity:
            support.notify_manager(activity, request.url_root)
    return jsonify(success=bool(payload.get('success')), state=state,
                   operation_id=payload.get('operation_id') or operation_id,
                   code=payload.get('code') or (None if payload.get('success') else 'operation_failed')), response.status_code


@bp.get('/api/support/operation/<operation_id>')
@login_required
def operation_status(operation_id):
    from panel.services.client_operations import load_client_operation

    profile = _profile()
    row = load_client_operation(operation_id, admin_id=profile.admin_id)
    if not row or row.action not in ('renew', 'add_client') or not support.location_allowed(
            profile, row.server_id, row.inbound_id):
        abort(404)
    return jsonify(state=row.state)


def _ticket_permission(row):
    return 'telegram_reply' if row.request_type == 'support' else 'telegram_review'


@bp.get('/support/ticket/<int:ticket_id>')
@login_required
def ticket(ticket_id):
    from panel.core.redis_client import load_snapshot_from_redis

    profile = _profile()
    load_snapshot_from_redis()
    admin = current_admin()
    row = db.get_or_404(TelegramServiceRequest, ticket_id)
    if row.request_type not in ('support', 'renewal') or not support.can_access_request(admin, row, _ticket_permission(row)):
        abort(404)
    return render_template('support_ticket.html', **_context(admin), ticket=row,
                           messages=row.messages[-40:] if row.request_type == 'support' else ())


@bp.post('/api/support/ticket/<int:ticket_id>/<action>')
@login_required
@limiter.limit('20 per minute')
def ticket_action(ticket_id, action):
    from panel.core.redis_client import load_snapshot_from_redis
    from panel.routes.telegram import reply_telegram_support_operation, review_telegram_service_operation

    _profile()
    load_snapshot_from_redis()
    row = db.get_or_404(TelegramServiceRequest, ticket_id)
    if (action == 'reply' and row.request_type == 'support'
            and support.can_access_request(current_admin(), row, 'telegram_reply')):
        result = reply_telegram_support_operation(ticket_id)
    elif (action in ('complete', 'reject') and row.request_type == 'renewal'
          and support.can_access_request(current_admin(), row, 'telegram_review')):
        result = review_telegram_service_operation(ticket_id, action)
    else:
        abort(403)
    response = current_app.make_response(result)
    return jsonify(success=bool((response.get_json(silent=True) or {}).get('success'))), response.status_code


@bp.get('/api/support/ticket/<int:ticket_id>/attachment/<int:message_id>')
@login_required
def ticket_attachment(ticket_id, message_id):
    from panel.core.redis_client import load_snapshot_from_redis
    from panel.routes.telegram import telegram_support_message_attachment

    _profile()
    load_snapshot_from_redis()
    row = db.get_or_404(TelegramServiceRequest, ticket_id)
    if row.request_type != 'support' or not support.can_access_request(current_admin(), row, 'telegram_reply'):
        abort(404)
    return telegram_support_message_attachment(ticket_id, message_id)


@bp.get('/api/support/ticket/<int:ticket_id>/receipt')
@login_required
@limiter.limit('20 per minute')
def ticket_receipt(ticket_id):
    from app import _telegram_bot_api_client
    from panel.core.redis_client import load_snapshot_from_redis

    _profile()
    load_snapshot_from_redis()
    row = db.get_or_404(TelegramServiceRequest, ticket_id)
    if (row.request_type != 'renewal' or not row.receipt_file_id
            or not support.can_access_request(current_admin(), row, 'telegram_review')):
        abort(404)
    bot = db.session.get(TelegramBotInstance, row.bot_instance_id)
    if not bot:
        abort(404)
    try:
        content, _, _, _ = _telegram_bot_api_client(bot).download_file(row.receipt_file_id, max_bytes=5 * 1024 ** 2)
    except Exception:
        return jsonify(success=False, code='receipt_unavailable'), 502
    return send_file(io.BytesIO(content), mimetype='application/octet-stream', as_attachment=True,
                     download_name=f'receipt-{ticket_id}', max_age=0)


def _portal_customer():
    user = db.session.get(ClientPortalUser, session['client_id']) if session.get('client_id') else None
    if not user or not user.enabled or user.must_change_password:
        return None
    return CustomerAccount.query.filter_by(primary_phone=user.mobile).filter(
        CustomerAccount.phone_verified_at.is_not(None)).first()


@bp.get('/client/support')
def portal_support():
    customer = _portal_customer()
    if not session.get('client_id'):
        return redirect(url_for('auth.login'))
    services = ServiceOwnership.query.filter_by(customer_id=customer.id, revoked_at=None).order_by(
        ServiceOwnership.id).limit(100).all() if customer else []
    return render_template('support_portal.html', services=services, support_fa=_lang() == 'fa')


def _chat_messages(service_id):
    rows = SupportChatMessage.query.filter_by(service_id=service_id).order_by(
        SupportChatMessage.id.desc()).limit(60).all()
    return [{'body': row.body, 'operator': bool(row.operator_id),
             'at': row.created_at.isoformat()} for row in reversed(rows)]


def _chat_body(data):
    body = data.get('message')
    if not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000:
        abort(400)
    return body.strip()


@bp.route('/api/support/portal-chat/<int:service_id>', methods=['GET', 'POST'])
@limiter.limit('30 per minute')
def portal_chat(service_id):
    customer = _portal_customer()
    if not customer:
        abort(403)
    service = ServiceOwnership.query.filter_by(id=service_id, customer_id=customer.id, revoked_at=None).first_or_404()
    if request.method == 'POST':
        body = _chat_body(_request_payload())
        # Serialize the first conversation and subsequent reopens for this service.
        service = ServiceOwnership.query.filter_by(
            id=service.id, customer_id=customer.id, revoked_at=None).with_for_update().first_or_404()
        chat = SupportChat.query.filter_by(service_id=service.id).with_for_update().first()
        if not chat:
            chat = SupportChat(service_id=service_id, open=True)
            db.session.add(chat)
            db.session.flush()
        if not chat.open:
            chat.assigned_to = None
        if chat.assigned_to:
            assigned = support.profile_for(db.session.get(Admin, chat.assigned_to))
            if 'chat' not in support.capabilities_for(assigned) or not support.owns_service(assigned, service):
                chat.assigned_to = None
        chat.open, chat.updated_at = True, datetime.utcnow()
        db.session.add(SupportChatMessage(service_id=service_id, body=body))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify(success=False, code='chat_changed'), 409
    return jsonify(items=_chat_messages(service_id))


def _operator_chat(service_id, lock=False):
    from panel.core.redis_client import load_snapshot_from_redis

    profile = _profile()
    _require(profile, 'chat')
    load_snapshot_from_redis()
    service = db.get_or_404(ServiceOwnership, service_id)
    chat = SupportChat.query.filter_by(service_id=service_id)
    if lock:
        chat = chat.with_for_update()
    chat = chat.first_or_404()
    if (chat.assigned_to not in (None, profile.admin_id) or not support.can_access_service(profile, service)):
        abort(404)
    return profile, service, chat


@bp.get('/support/chat/<int:service_id>')
@login_required
def chat_page(service_id):
    _, service, chat = _operator_chat(service_id)
    return render_template('support_chat.html', **_context(current_admin()), service=service, chat=chat)


@bp.route('/api/support/chat/<int:service_id>', methods=['GET', 'POST'])
@login_required
@limiter.limit('30 per minute')
def operator_chat(service_id):
    profile, service, chat = _operator_chat(service_id, lock=request.method == 'POST')
    if request.method == 'POST':
        if not chat.open:
            abort(409)
        data = _request_payload()
        body = _chat_body(data)
        closed = data.get('complete') is True
        latest_customer = SupportChatMessage.query.filter_by(service_id=service.id, operator_id=None).order_by(
            SupportChatMessage.id.desc()).first()
        response_seconds = (max(0, int((datetime.utcnow() - latest_customer.created_at).total_seconds()))
                            if latest_customer else None)
        message = SupportChatMessage(service_id=service_id, body=body, operator_id=profile.admin_id)
        db.session.add(message)
        db.session.flush()
        chat.assigned_to, chat.updated_at, chat.open = profile.admin_id, datetime.utcnow(), not closed
        support.new_activity(profile, 'reply', f'portal-message:{message.id}', service=service,
                             details={'channel': 'chat', 'closed': closed, 'response_seconds': response_seconds})
        db.session.commit()
    return jsonify(items=_chat_messages(service_id), open=chat.open)


@bp.get('/api/support/account/<int:server_id>/<int:inbound_id>/<email>/history')
@login_required
def account_history(server_id, inbound_id, email):
    profile = _profile()
    _require(profile, 'customers')
    (_, uuid), _ = _target(profile, server_id, inbound_id, email)
    rows = SupportActivity.query.filter_by(server_id=server_id, client_uuid=uuid, owner_id=profile.owner_id,
                                           operator_id=profile.admin_id).order_by(
        SupportActivity.id.desc()).limit(40).all()
    return jsonify(items=[{
        'kind': row.kind, 'state': row.state, 'at': row.created_at.isoformat(),
        'before': row.details.get('before'), 'after': row.details.get('after'),
        'reason': row.details.get('reason'),
    } for row in rows])


@bp.post('/api/support/account/<int:server_id>/<int:inbound_id>/<email>/correction')
@login_required
@limiter.limit('10 per minute')
def correction(server_id, inbound_id, email):
    profile = _profile()
    _require(profile, 'correction')
    (service, uuid), raw = _target(profile, server_id, inbound_id, email)
    reason = str(_request_payload().get('reason') or '').strip()
    if not 3 <= len(reason) <= 1000:
        abort(400)
    row = support.new_activity(profile, 'correction', f'correction:{secrets.token_hex(16)}',
                               service=service, state='pending',
                               details={'email': email, 'reason': reason, 'inbound_id': inbound_id,
                                        'before': {'expiry': raw.get('expiryTime'), 'bytes': raw.get('totalGB')}})
    row.server_id, row.client_uuid = server_id, uuid
    audit.record('support.correction_requested', actor=current_admin(),
                 meta={'server_id': server_id, 'client_uuid': uuid, 'reference': row.reference})
    db.session.commit()
    notified = support.notify_manager(row, request.url_root)
    return jsonify(success=True, notified=notified), 201


@bp.post('/api/support/account/<int:server_id>/<int:inbound_id>/<email>/sms')
@login_required
@limiter.limit('20 per minute')
def send_sms(server_id, inbound_id, email):
    profile = _profile()
    _require(profile, 'sms')
    (service, _), _ = _target(profile, server_id, inbound_id, email)
    data = _request_payload()
    text = str(data.get('message') or '').strip()
    key = str(data.get('key') or '')
    if not 1 <= len(text) <= 480 or not re.fullmatch(r'[a-zA-Z0-9-]{20,64}', key):
        abort(400)
    if not service or not service.customer or not service.customer.phone_verified_at or not service.customer.primary_phone:
        return jsonify(success=False, code='no_verified_phone'), 409
    reference = f'SupportSms-{profile.admin_id}-{key}'
    existing = SupportActivity.query.filter_by(reference=reference).first()
    if existing:
        return jsonify(success=existing.state in ('accepted', 'delivered'), state=existing.state)
    today = datetime.utcnow().date()
    operator, team = support.sms_limits(profile)
    from app import _send_sms_via_gmweb

    if not (support.reserve_daily_slot(f'sms:team:{profile.owner_id or 0}', team, today)
            and support.reserve_daily_slot(f'sms:operator:{profile.admin_id}', operator, today)):
        db.session.rollback()
        return jsonify(success=False, code='daily_sms_limit'), 409
    row = support.new_activity(profile, 'sms', reference, service=service, state='reserved',
                               details=support.snapshot_contract(profile))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(success=False, code='already_submitted'), 409
    report = _send_sms_via_gmweb(service.customer.primary_phone, text, priority='critical',
                                idempotency_key=reference)
    locked = SupportActivity.query.filter_by(id=row.id).populate_existing().with_for_update().one()
    locked.gateway_request_id = report.get('request_id') or report.get('gateway_request_id')
    if locked.state == 'reserved':
        locked.state = 'accepted' if report.get('accepted') else 'unconfirmed'
    db.session.commit()
    if locked.gateway_request_id:
        recent = SmsGatewayEvent.query.filter(or_(
            SmsGatewayEvent.request_id == locked.gateway_request_id,
            SmsGatewayEvent.gateway_request_id == locked.gateway_request_id,
            SmsGatewayEvent.message_id == locked.gateway_request_id,
        ), SmsGatewayEvent.event_type.in_(('sms.delivered', 'sms.delivery_failed'))).order_by(
            SmsGatewayEvent.occurred_at.desc()).limit(50).all()
        for event in recent:
            support.record_sms_delivery({
                'event_type': event.event_type, 'carrier_status': event.carrier_status,
                'request_id': event.request_id, 'gateway_request_id': event.gateway_request_id,
                'message_id': event.message_id, 'diagnostics_json': event.diagnostics_json,
            })
    db.session.commit()
    return jsonify(success=locked.state in ('accepted', 'delivered'), state=locked.state)


def _manager():
    actor = current_admin()
    if admin_is_superadmin(actor):
        return actor
    if actor and actor.role == 'reseller' and support.tenant_setting(actor.id, 'delegation'):
        return actor
    abort(403)


def _manager_owner(actor, value=None):
    if actor.role == 'reseller' and not admin_is_superadmin(actor):
        if value not in (None, '', actor.id, str(actor.id)):
            abort(403)
        return actor.id
    if value in (None, '', 0, '0'):
        return None
    owner = db.session.get(Admin, _integer(value, minimum=1))
    if not owner or owner.role != 'reseller' or not owner.enabled:
        abort(404)
    return owner.id


def _managed_operator(actor, operator_id):
    profile = db.get_or_404(SupportOperator, operator_id)
    if not admin_is_superadmin(actor) and profile.owner_id != actor.id:
        abort(404)
    return profile


def _integer(value, minimum=0, maximum=2_000_000_000):
    if isinstance(value, bool) or not re.fullmatch(r'-?[0-9]{1,10}', str(value)):
        abort(400)
    number = int(value)
    if not minimum <= number <= maximum:
        abort(400)
    return number


def _permission_list(values):
    if (not isinstance(values, list)
            or any(type(item) is not str or item not in support.CAPABILITIES for item in values)):
        abort(400)
    return sorted(set(values))


def _write_setting(owner_id, option, value):
    key = f'support:{owner_id or 0}:{option}'
    row = db.session.get(SystemSetting, key)
    if not row:
        row = SystemSetting(key=key)
        db.session.add(row)
    row.value = str(value)


@bp.get('/support/team')
@login_required
def team():
    actor = _manager()
    is_super = admin_is_superadmin(actor)
    owner_id = _manager_owner(actor, request.args.get('owner'))
    operator_query = db.session.query(SupportOperator, Admin).join(
        Admin, Admin.id == SupportOperator.admin_id).filter(SupportOperator.owner_id == owner_id)
    contractors = operator_query.order_by(Admin.username).all()
    start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    activity = SupportActivity.query.filter_by(owner_id=owner_id)
    corrections = activity.filter_by(kind='correction', state='pending').order_by(
        SupportActivity.id.desc()).limit(40).all()
    account_query = request.args.get('account', '').strip()[:100]
    if account_query:
        activity = activity.filter(SupportActivity.details['email'].as_string() == account_query)
    history = activity.order_by(SupportActivity.id.desc()).limit(80).all()
    settlements = SupportPayroll.query.filter_by(owner_id=owner_id).order_by(
        SupportPayroll.period.desc(), SupportPayroll.id.desc()).limit(60).all()
    roles = SupportRole.query.order_by(SupportRole.name).all()
    owners = Admin.query.filter_by(role='reseller', enabled=True).order_by(Admin.username).all() if is_super else []
    metrics = {admin.id: _metrics(_row_range(_activity(admin.id, owner_id), start, datetime.utcnow()))
               for _, admin in contractors}
    from app import GLOBAL_SERVER_DATA, get_reseller_access_maps, is_inbound_accessible
    from panel.core.redis_client import load_snapshot_from_redis

    load_snapshot_from_redis()
    owner = db.session.get(Admin, owner_id) if owner_id else None
    allowed, assignments = get_reseller_access_maps(owner) if owner else ('*', {})
    available = [(ib['server_id'], ib['id'], ib.get('server_name', ''), ib.get('remark', ''))
                 for ib in (GLOBAL_SERVER_DATA.get('inbounds') or [])
                 if ib.get('server_id') and ib.get('id') and
                 is_inbound_accessible(ib['server_id'], ib['id'], allowed, assignments)]
    return render_template(
        'support_team.html', **_context(actor), selected_owner=owner_id,
        contractors=contractors, roles=roles, owners=owners, available=available, corrections=corrections,
        history=history, account_query=account_query, settlements=settlements, capability_labels=support.CAPABILITIES,
        delegation=support.tenant_setting(owner_id, 'delegation').split(','),
        sms_limit=support.tenant_setting(owner_id, 'sms_limit') or '0', metrics=metrics,
        role_preview={
            'caps': support.CAPABILITIES,
            'roles': {'': sorted(support.DEFAULT_CAPABILITIES), **{str(row.id): row.permissions for row in roles}},
            'ceiling': support.tenant_setting(owner_id, 'delegation').split(',') if owner_id else None,
        },
    )


@bp.post('/api/support/team/policy')
@login_required
def team_policy():
    actor = _manager()
    if not admin_is_superadmin(actor):
        abort(403)
    data = _request_payload()
    owner_id = _manager_owner(actor, data.get('owner_id'))
    grant = _permission_list(data.get('permissions', []))
    _write_setting(owner_id, 'delegation', ','.join(grant))
    _write_setting(owner_id, 'sms_limit', _integer(data.get('sms_limit'), maximum=100000))
    audit.record('support.team_policy', actor=actor, meta={'owner_id': owner_id, 'permissions': grant})
    db.session.commit()
    return jsonify(success=True)


@bp.post('/api/support/team/roles')
@login_required
def save_role():
    actor = _manager()
    if not admin_is_superadmin(actor):
        abort(403)
    data = _request_payload()
    name = str(data.get('name') or '').strip()
    if not 1 <= len(name) <= 80:
        abort(400)
    row = db.get_or_404(SupportRole, _integer(data['id'], minimum=1)) if data.get('id') else SupportRole()
    row.name, row.permissions = name, _permission_list(data.get('permissions'))
    db.session.add(row)
    try:
        db.session.flush()
        audit.record('support.role_saved', actor=actor, meta={'role_id': row.id, 'permissions': row.permissions})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(success=False, code='role_name_used'), 409
    return jsonify(success=True, id=row.id)


@bp.post('/api/support/team/operators')
@login_required
def save_operator():
    actor = _manager()
    data = _request_payload()
    owner_id = _manager_owner(actor, data.get('owner_id'))
    operator_id = data.get('id')
    if operator_id:
        profile = _managed_operator(actor, _integer(operator_id, minimum=1))
        account = db.get_or_404(Admin, profile.admin_id)
        if profile.owner_id != owner_id or account.role != 'support':
            abort(409)
    else:
        previous_id = data.get('legacy_id')
        if previous_id:
            if not admin_is_superadmin(actor):
                abort(403)
            account = db.session.get(Admin, _integer(previous_id, minimum=1))
            if not account or account.role != 'admin' or account.is_superadmin or account.id == actor.id:
                abort(409)
        else:
            account = Admin()
        account.role, account.is_superadmin, profile = 'support', False, None
    username = str(data.get('username') or '').strip()
    password = data.get('password') or ''
    if not re.fullmatch(r'[a-zA-Z0-9_.@-]{3,100}', username) or type(password) is not str:
        abort(400)
    if (not account.id or password) and not 12 <= len(password) <= 128:
        abort(400)
    raw_locations = data.get('locations')
    if not isinstance(raw_locations, list) or len(raw_locations) > 300:
        abort(400)
    locations = {}
    for place in raw_locations:
        if not isinstance(place, str) or not re.fullmatch(r'[0-9]{1,10}:[0-9]{1,10}', place):
            abort(400)
        server_id, inbound_id = (int(part) for part in place.split(':'))
        locations.setdefault(str(server_id), []).append(inbound_id)
    filters = {}
    for entry in str(data.get('account_filter') or '').split():
        sid, separator, uid = entry.partition(':')
        if not separator or sid not in locations or not re.fullmatch(r'[A-Za-z0-9_.@+-]{1,100}', uid):
            abort(400)
        filters.setdefault(sid, []).append(uid)
    if sum(map(len, filters.values())) > 300:
        abort(400)
    role_id = _integer(data['role_id'], minimum=1) if data.get('role_id') else None
    role = db.get_or_404(SupportRole, role_id) if role_id else None
    overrides = data.get('overrides') or {}
    if (not isinstance(overrides, dict) or set(overrides) - support.CAPABILITIES.keys()
            or any(type(value) is not bool for value in overrides.values())):
        abort(400)
    telegram_id = str(data.get('telegram_id') or '').strip()
    if telegram_id and (not re.fullmatch(r'[1-9][0-9]{1,19}', telegram_id)
                        or Admin.query.filter_by(telegram_id=telegram_id).filter(
                            Admin.id != (account.id or 0)).first()):
        abort(400)
    if type(data.get('enabled')) is not bool:
        abort(400)
    account.username, account.enabled, account.telegram_id = username, data['enabled'], telegram_id or None
    if password:
        account.set_password(password)
    try:
        db.session.add(account)
        db.session.flush()
        if not profile:
            profile = SupportOperator(admin_id=account.id, owner_id=owner_id)
        profile.role, profile.overrides, profile.locations = role, overrides, locations
        profile.account_filter = filters
        profile.sms_limit = _integer(data.get('sms_limit'), maximum=100000)
        profile.free_limit = _integer(data.get('free_limit'), maximum=1000)
        profile.free_max_gb = _integer(data.get('free_max_gb'), maximum=10000)
        db.session.add(profile)
        for server_id, inbounds in locations.items():
            if any(not support.location_allowed(profile, int(server_id), inbound) for inbound in inbounds):
                abort(403)
        if not account.enabled or password or data.get('legacy_id'):
            from panel.services.sessions import revoke_other_sessions
            revoke_other_sessions(account.id, None)
        audit.record('support.operator_saved', actor=actor,
                     meta={'operator_id': account.id, 'owner_id': owner_id, 'locations': locations,
                           'enabled': account.enabled, 'override': overrides})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(success=False, code='username_used'), 409
    return jsonify(success=True, id=account.id)


@bp.post('/api/support/team/operators/<int:operator_id>/contract')
@login_required
def save_contract(operator_id):
    actor = _manager()
    profile = _managed_operator(actor, operator_id)
    data = _request_payload()
    mode = data.get('mode')
    if mode not in ('none', 'flat', 'percent', 'tiers'):
        abort(400)
    terms = {'mode': mode, **{key: _integer(data.get(key, 0))
                             for key in ('salary', 'create', 'renew', 'free', 'sms')}}
    terms['basis_points'] = _integer(data.get('basis_points', 0), maximum=10000)
    tiers = data.get('tiers', [])
    if mode == 'tiers':
        if not isinstance(tiers, list) or not 1 <= len(tiers) <= 12:
            abort(400)
        clean = []
        previous = 0
        for index, band in enumerate(tiers):
            if not isinstance(band, list) or len(band) != 2 or (band[0] is None) != (index == len(tiers) - 1):
                abort(400)
            upper = _integer(band[0], minimum=previous + 1) if band[0] is not None else None
            clean.append([upper, _integer(band[1])])
            previous = upper
        terms['tiers'] = clean
    db.session.add(SupportContract(operator_id=operator_id, owner_id=profile.owner_id, terms=terms))
    audit.record('support.contract_added', actor=actor, meta={'operator_id': operator_id, 'terms': terms})
    db.session.commit()
    return jsonify(success=True)


@bp.post('/api/support/team/activities/<int:activity_id>/<action>')
@login_required
def review_activity(activity_id, action):
    actor = _manager()
    row = SupportActivity.query.filter_by(id=activity_id).with_for_update().first_or_404()
    profile = _managed_operator(actor, row.operator_id)
    if row.owner_id != profile.owner_id:
        abort(404)
    data = _request_payload()
    reason = str(data.get('reason') or '').strip()
    if not 3 <= len(reason) <= 1000:
        abort(400)
    if action == 'resolve' and row.kind == 'correction' and row.state == 'pending':
        row.state = 'completed'
        row.details = {**row.details, 'resolution': reason, 'resolved_by': actor.id}
    elif action == 'payment' and row.kind in ('create', 'renew') and row.state == 'completed':
        if row.details.get('free') or SupportPayroll.query.filter_by(
                operator_id=row.operator_id, period=row.created_at.date().replace(day=1)).first():
            abort(409)
        collected = _integer(data.get('amount'))
        row.details = {**row.details, 'collected': collected, 'payment_evidence': reason}
    elif action == 'adjustment' and row.kind in ('create', 'renew', 'sms'):
        support.new_activity(profile, 'adjustment', f'adjustment:{secrets.token_hex(16)}',
                             details={'amount': _integer(data.get('amount'), minimum=-2_000_000_000),
                                      'reason': reason, 'reference': row.reference, 'manager_id': actor.id})
    else:
        abort(409)
    audit.record(f'support.activity_{action}', actor=actor,
                 meta={'activity_id': row.id, 'operator_id': row.operator_id, 'reason': reason})
    db.session.commit()
    return jsonify(success=True)


@bp.post('/api/support/team/operators/<int:operator_id>/payroll')
@login_required
def approve_payroll(operator_id):
    actor = _manager()
    profile = _managed_operator(actor, operator_id)
    profile = SupportOperator.query.filter_by(admin_id=profile.admin_id).with_for_update().one()
    try:
        period = date.fromisoformat(str(_request_payload().get('month')) + '-01')
    except ValueError:
        abort(400)
    if period >= datetime.utcnow().date().replace(day=1):
        abort(409)
    existing = SupportPayroll.query.filter_by(operator_id=operator_id, period=period).first()
    if existing:
        return jsonify(success=True, id=existing.id)
    start = datetime.combine(period, datetime.min.time())
    end = datetime.combine(support.next_month(period), datetime.min.time())
    rows = _row_range(_activity(operator_id, profile.owner_id), start, end)
    breakdown = support.period_amount(rows, start, end, salary=support.monthly_salary(operator_id, period))
    amount = sum(breakdown.values())
    if not 0 <= amount <= 2_000_000_000:
        return jsonify(success=False, code='invalid_payroll_total'), 409
    row = SupportPayroll(operator_id=operator_id, owner_id=profile.owner_id, period=period,
                         amount=amount, breakdown=breakdown, approved_by=actor.id)
    db.session.add(row)
    try:
        db.session.flush()
        audit.record('support.payroll_approved', actor=actor, meta={'payroll_id': row.id, 'amount': amount})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(success=False, code='payroll_already_exists'), 409
    return jsonify(success=True, id=row.id)


@bp.post('/api/support/team/payroll/<int:payroll_id>/pay')
@login_required
def record_payroll_payment(payroll_id):
    actor = _manager()
    row = SupportPayroll.query.filter_by(id=payroll_id).with_for_update().first_or_404()
    if not admin_is_superadmin(actor) and row.owner_id != actor.id:
        abort(404)
    now = datetime.utcnow()
    claimed = db.session.execute(update(SupportPayroll).where(
        SupportPayroll.id == row.id, SupportPayroll.paid_at.is_(None),
    ).values(paid_at=now, paid_by=actor.id))
    if claimed.rowcount != 1:
        return jsonify(success=True)
    if row.amount:
        db.session.add(Transaction(
            admin_id=row.owner_id or actor.id, amount=-row.amount, category='expense',
            type='reseller_support_payroll' if row.owner_id else 'support_payroll',
            description=f'Support payroll #{row.id} ({row.period:%Y-%m})',
        ))
    audit.record('support.payroll_paid', actor=actor, meta={'payroll_id': row.id, 'amount': row.amount})
    db.session.commit()
    return jsonify(success=True)
