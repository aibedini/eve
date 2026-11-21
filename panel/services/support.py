"""Authorization and accounting shared by the web workspace and Telegram."""

import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import (
    Admin, ClientOwnership, Server, ServiceOwnership, SupportActivity, SupportBudget, SupportContract,
    SupportOperator, SystemSetting,
)

CAPABILITIES = {
    'customers': ('View customers', 'مشاهدهٔ مشتریان'),
    'renew': ('Renew paid accounts', 'تمدید پولی'),
    'create': ('Create paid accounts', 'ساخت پولی'),
    'free_create': ('Create free accounts', 'ساخت رایگان'),
    'free_renew': ('Renew free accounts', 'تمدید رایگان'),
    'correction': ('Request corrections', 'درخواست اصلاح'),
    'telegram_reply': ('Reply on Telegram', 'پاسخ در تلگرام'),
    'telegram_review': ('Review Telegram renewals', 'بررسی تمدید تلگرام'),
    'chat': ('Reply to portal chat', 'پاسخ به چت پورتال'),
    'sms': ('Send support SMS', 'ارسال پیامک پشتیبانی'),
}
DEFAULT_CAPABILITIES = frozenset(('customers', 'renew', 'correction', 'telegram_reply', 'chat'))


def profile_for(admin):
    if admin and admin.enabled and admin.role == 'support':
        return db.session.get(SupportOperator, admin.id)


def owner_for(profile):
    if profile.owner_id is None:
        return None
    owner = db.session.get(Admin, profile.owner_id)
    if owner and owner.enabled and owner.role == 'reseller':
        return owner


def tenant_setting(owner_id, option):
    row = db.session.get(SystemSetting, f'support:{owner_id or 0}:{option}')
    return str(row.value or '') if row else ''


def capabilities_for(profile):
    if not profile or (profile.owner_id is not None and not owner_for(profile)):
        return frozenset()
    allowed = set(profile.role.permissions if profile.role else DEFAULT_CAPABILITIES)
    for key, value in profile.overrides.items():
        if value is True:
            allowed.add(key)
        elif value is False:
            allowed.discard(key)
    if profile.owner_id is not None:
        allowed.intersection_update(tenant_setting(profile.owner_id, 'delegation').split(','))
    return allowed.intersection(CAPABILITIES)


def location_allowed(profile, server_id, inbound_id=None):
    if not profile or (profile.owner_id is not None and not owner_for(profile)):
        return False
    inbounds = profile.locations.get(str(server_id), ())
    if not inbounds or (inbound_id is not None and inbound_id not in inbounds):
        return False
    server = db.session.get(Server, server_id)
    if not server or not server.enabled:
        return False
    owner = owner_for(profile)
    if not owner:
        return True
    from app import get_reseller_access_maps, is_inbound_accessible, is_server_accessible

    allowed, assignments = get_reseller_access_maps(owner)
    if inbound_id is not None:
        return is_inbound_accessible(server_id, inbound_id, allowed, assignments)
    return is_server_accessible(server_id, allowed, assignments)


def owns_service(profile, service):
    return bool(service and not service.revoked_at and profile
                and service.reseller_id == profile.owner_id
                and location_allowed(profile, service.server_id))


def scoped_account(profile, server_id, inbound_id, email, raw):
    """Return the canonical service and UUID for the exact requested membership."""
    from panel.services.billing import pricing_owner

    if not location_allowed(profile, server_id, inbound_id):
        return None
    client_uuid = str((raw or {}).get('id') or '')
    if not client_uuid:
        return None
    filters = profile.account_filter or {}
    if filters and client_uuid not in filters.get(str(server_id), ()):
        return None
    service = ServiceOwnership.query.filter_by(server_id=server_id, client_uuid=client_uuid).first()
    if service and not owns_service(profile, service):
        return None
    legacy = ClientOwnership.query.filter_by(server_id=server_id, inbound_id=inbound_id,
                                            client_uuid=client_uuid).all()
    for record in legacy:
        owner = db.session.get(Admin, record.reseller_id)
        if owner and owner.role == 'reseller' and owner.id != profile.owner_id:
            return None
    if pricing_owner(server_id, client_uuid, email, inbound_id=inbound_id) != profile.owner_id:
        return None
    return service, client_uuid


def can_access_service(profile, service):
    if not owns_service(profile, service):
        return False
    from app import GLOBAL_SERVER_DATA

    for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
        if inbound.get('server_id') != service.server_id:
            continue
        inbound_id = inbound.get('id')
        if not location_allowed(profile, service.server_id, inbound_id):
            continue
        for client in inbound.get('clients', ()):
            raw = client.get('raw_client') or {}
            if str(raw.get('id')) == service.client_uuid:
                return bool(scoped_account(profile, service.server_id, inbound_id, client.get('email'), raw))
    return False


def can_access_request(admin, row, capability):
    profile = profile_for(admin)
    return (capability in capabilities_for(profile)
            and row.assigned_admin_id in (None, admin.id)
            and can_access_service(profile, row.ownership))


def new_activity(profile, kind, reference, *, service=None, state='completed', details=None):
    row = SupportActivity(
        operator_id=profile.admin_id, owner_id=profile.owner_id, kind=kind,
        reference=reference, state=state, details=details or {},
        customer_id=service.customer_id if service else None,
        server_id=service.server_id if service else None,
        client_uuid=service.client_uuid if service else None,
    )
    db.session.add(row)
    return row


def reserve_daily_slot(scope, limit, day):
    if limit <= 0:
        return False
    try:
        with db.session.begin_nested():
            db.session.add(SupportBudget(scope=scope, day=day, used=0))
            db.session.flush()
    except IntegrityError:
        pass
    result = db.session.execute(update(SupportBudget).where(
        SupportBudget.scope == scope, SupportBudget.day == day, SupportBudget.used < limit,
    ).values(used=SupportBudget.used + 1))
    return result.rowcount == 1


def sms_limits(profile):
    raw = tenant_setting(profile.owner_id, 'sms_limit')
    return profile.sms_limit, int(raw) if raw.isdecimal() else 0


def active_contract(operator_id, at):
    return SupportContract.query.filter(
        SupportContract.operator_id == operator_id, SupportContract.starts_at <= at,
    ).order_by(SupportContract.starts_at.desc(), SupportContract.id.desc()).first()


def next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def period_amount(rows, start, end, *, salary=0):
    amounts = {'salary': int(salary), 'sales': 0, 'sms': 0, 'free': 0, 'adjustments': 0}
    volume_by_contract = {}
    for row in rows:
        details = row.details
        if row.state not in ('completed', 'delivered'):
            continue
        count = start <= row.created_at < end
        contract = details.get('terms', {})
        if row.kind == 'adjustment' and count:
            amounts['adjustments'] += details.get('amount', 0)
        elif row.kind == 'sms' and count and row.state == 'delivered':
            amounts['sms'] += contract.get('sms', 0)
        elif row.kind in ('create', 'renew'):
            if details.get('free'):
                if count:
                    amounts['free'] += contract.get('free', 0)
                continue
            mode = contract.get('mode')
            if mode == 'tiers':
                key = (row.created_at.year, row.created_at.month, details.get('contract_id'))
                start_gb = volume_by_contract.get(key, Decimal(0))
                end_gb = start_gb + Decimal(str(details.get('volume_gb', 0)))
                volume_by_contract[key] = end_gb
                if count:
                    lower = 0
                    for upper, rate in contract.get('tiers', []):
                        top = end_gb if upper is None else min(end_gb, upper)
                        amounts['sales'] += max(Decimal(0), top - max(lower, start_gb)) * rate
                        lower = upper
            elif mode == 'flat' and count:
                amounts['sales'] += contract.get(row.kind, 0)
            elif mode == 'percent' and count:
                amounts['sales'] += Decimal(details.get('collected', 0)) * contract.get('basis_points', 0) / 10000
    return {key: int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            for key, value in amounts.items()}


def monthly_salary(operator_id, period):
    start = datetime.combine(period, datetime.min.time())
    end = datetime.combine(next_month(period), datetime.min.time())
    contract = active_contract(operator_id, start)
    if not contract:
        contract = SupportContract.query.filter(
            SupportContract.operator_id == operator_id, SupportContract.starts_at < end,
        ).order_by(SupportContract.starts_at, SupportContract.id).first()
    return contract.terms.get('salary', 0) if contract else 0


def snapshot_contract(profile, at=None):
    contract = active_contract(profile.admin_id, at or datetime.utcnow())
    return {'terms': contract.terms, 'contract_id': contract.id} if contract else {}


def report_client_operation(operation, response):
    """Record work in the same transaction as durable operation finalization."""
    profile = db.session.get(SupportOperator, operation.admin_id) if operation.admin_id else None
    if not profile or operation.action not in ('renew', 'add_client') or operation.state != 'completed':
        return
    from panel.services.client_operations import operation_context, operation_expected

    reference = f'operation:{operation.id}'
    if SupportActivity.query.filter_by(reference=reference).first():
        return
    quote = operation.pricing_quote or {}
    expected = operation_expected(operation) or {}
    before = (operation_context(operation).get('before_evidence') or {})
    uuid = expected.get('client_uuid') or (response or {}).get('client_uuid')
    if not uuid and operation.client_email and operation.inbound_id is not None:
        from app import _get_cached_raw_client
        raw = _get_cached_raw_client(operation.server_id, operation.inbound_id, operation.client_email)
        uuid = (raw or {}).get('id')
    service = ServiceOwnership.query.filter_by(server_id=operation.server_id, client_uuid=uuid,
                                               revoked_at=None).first() if uuid else None
    details = {
        **snapshot_contract(profile, operation.created_at),
        'free': bool(quote.get('free')),
        'volume_gb': quote.get('volume_gb', 0),
        'days': quote.get('days', 0),
        'email': operation.client_email,
        'inbound_id': operation.inbound_id,
        'before': {key: before[key] for key in ('expiryTime', 'totalGB', 'enable') if key in before},
        'after': {key: expected[key] for key in ('expiryTime', 'totalGB', 'enable') if key in expected},
    }
    if operation.idempotency_key.startswith('telegram-renew:'):
        details['channel'] = 'telegram'
        details['collected'] = int(quote.get('retail') or 0)
    else:
        details['channel'] = 'web'
    row = new_activity(profile, 'renew' if operation.action == 'renew' else 'create',
                       reference, service=service, details=details)
    row.server_id = operation.server_id
    row.client_uuid = uuid
    return row


def record_sms_delivery(values):
    reference = values.get('eve_notification_id')
    state = values.get('carrier_status')
    if state not in ('delivered', 'failed') or (values.get('event_type') not in
            ('sms.delivered', 'sms.delivery_failed')):
        return
    query = SupportActivity.query.filter_by(kind='sms')
    if str(reference or '').startswith('SupportSms-'):
        query = query.filter_by(reference=reference)
    else:
        ids = {values.get('gateway_request_id'), values.get('request_id'), values.get('message_id')} - {None, ''}
        if not ids:
            return
        query = query.filter(SupportActivity.gateway_request_id.in_(ids))
    row = query.with_for_update().first()
    if not row:
        return
    diagnostics = json.loads(values.get('diagnostics_json') or '{}')
    segments = diagnostics.get('segment_count')
    if type(segments) is int and segments > 0:
        row.details = {**row.details, 'segments': max(segments, row.details.get('segments') or 0)}
    if state == 'delivered' and diagnostics.get('all_segments_delivered') is False:
        return
    if row.state != 'delivered':
        row.state = state


def record_telegram_reply(admin, ticket, message, *, closed=False):
    profile = profile_for(admin)
    if profile:
        db.session.flush()
        new_activity(profile, 'reply', f'telegram-message:{message.id}', service=ticket.ownership,
                     details={'channel': 'telegram', 'ticket_id': ticket.id, 'closed': closed,
                              'response_seconds': max(0, int((datetime.utcnow() - ticket.created_at).total_seconds()))})


def notify_manager(activity, base_url):
    from flask import current_app
    from app import _central_telegram_bot, _telegram_bot_api_client, get_public_base_url
    from panel.models import TelegramBotInstance

    if activity.owner_id:
        owner = db.session.get(Admin, activity.owner_id)
        recipients = [owner] if owner and owner.enabled else []
        bot = TelegramBotInstance.query.filter_by(owner_admin_id=activity.owner_id).first()
    else:
        recipients = Admin.query.filter(Admin.enabled.is_(True), Admin.is_superadmin.is_(True)).all()
        bot = _central_telegram_bot()
    if not bot or not recipients:
        return False
    actor = db.session.get(Admin, activity.operator_id)
    link = (f'{get_public_base_url(base_url).rstrip("/")}/support/team'
            + (f'?owner={activity.owner_id}' if activity.owner_id else '')
            + f'#support-activity-{activity.id}')
    kind = f'free {activity.kind}' if activity.details.get('free') else activity.kind
    text = (f'Eve · {kind} #{activity.id}\n'
            f'Operator: {actor.username if actor else activity.operator_id}\n'
            f'Account: {activity.details.get("email", "")}\n'
            f'{link}')
    notified = False
    for recipient in recipients:
        try:
            chat = int(recipient.telegram_id or 0)
            if chat > 0:
                _telegram_bot_api_client(bot).send_message(chat, text)
                notified = True
        except Exception:
            current_app.logger.warning('Support alert delivery failed for activity %s', activity.id)
    return notified
