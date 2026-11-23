"""Allowlisted v1 projections. Customer credentials and raw records stay in Eve."""
import hmac
import re
from datetime import datetime, timezone

from sqlalchemy import and_, func

from panel.core.redis_client import GLOBAL_SERVER_DATA
from panel.core.snapshot_model import client_key
from panel.extensions import db
from panel.models import (FAQ, ClientOperation, NotificationTemplate, OnlineChatScript,
                          OwnershipClaim, OwnershipClaimItem, Package, RenewalEvent, RenewTemplate,
                          ServiceLifecycleState, ServiceOwnership, SmsSendLog, TelegramIdentity, Transaction)
from panel.models.support import SupportChat, SupportChatMessage
from panel.models.telegram import CustomerTransaction, TelegramServiceRequest
from panel.services.lifecycle import make_service_key, resolve_client_uuid
from panel.services.machine_security import (configured_secret, private_service_identity, public_operation_id,
                                             public_service_key, timestamp, utcnow)

GIB = 1024 ** 3


def masked_digits(value):
    digits = re.sub(r'[^0-9]', '', str(value or ''))
    return ('••••' + digits[-4:]) if len(digits) >= 8 else ('••••' if value else None)


def masked_name(value):
    return '••••' if value else None


def card_hash(value):
    digits = re.sub(r'[^0-9]', '', str(value or ''))
    secret = configured_secret('EVE_MACHINE_PAYMENT_PEPPER')
    return hmac.digest(secret, digits.encode('ascii'), 'sha256').hex() if secret and len(digits) == 16 else None


def customer_views(rows):
    identities = (TelegramIdentity.query.filter(TelegramIdentity.customer_id.in_([row.id for row in rows]))
                  .order_by(TelegramIdentity.id).all()) if rows else []
    telegram = {row.customer_id: '••••' for row in identities}
    return [{'customer_id': row.id, 'display_name': masked_name(row.display_name),
             'primary_phone_masked': masked_digits(row.primary_phone),
             'telegram_masked': telegram.get(row.id), 'status': row.status} for row in rows]


def active_services():
    return ServiceOwnership.query.filter(ServiceOwnership.revoked_at.is_(None))


def find_service(key):
    identity = private_service_identity(key)
    if identity is None:
        return None
    return active_services().filter_by(server_id=identity[0], client_uuid=identity[1]).first()


def snapshot_clients(owners):
    keys = {(r.server_id, r.client_uuid) for r in owners}
    server_ids = {sid for sid, _ in keys}
    snapshot = {}
    indexes = GLOBAL_SERVER_DATA.get('normalized_indexes') or {}
    for owner in owners:
        entities = (indexes.get(owner.server_id) or {}).get('entities') or {}
        hint = client_key({'email': owner.client_email_snapshot, 'id': owner.client_uuid})
        candidate = entities.get(hint)
        if candidate and (resolve_client_uuid(candidate.get('raw_client')) or resolve_client_uuid(candidate)) == owner.client_uuid:
            snapshot[(owner.server_id, owner.client_uuid)] = candidate
    missing = keys - snapshot.keys()
    if not missing:
        return snapshot
    for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
        try:
            sid = int(inbound.get('server_id'))
        except (ValueError, TypeError):
            continue
        if sid not in server_ids:
            continue
        for client in inbound.get('clients') or []:
            uuid = resolve_client_uuid(client.get('raw_client')) or resolve_client_uuid(client)
            if (sid, uuid) in missing:
                snapshot[(sid, uuid)] = client
                missing.remove((sid, uuid))
                if not missing:
                    return snapshot
    return snapshot


def service_views(owners):
    if not owners:
        return []
    from sqlalchemy.orm import joinedload
    keys = {(r.server_id, r.client_uuid) for r in owners}
    snapshot = snapshot_clients(owners)
    key_strings = [make_service_key(*key) for key in keys]
    states = {row.service_key: row for row in ServiceLifecycleState.query.filter(
        ServiceLifecycleState.service_key.in_(key_strings)).all()}
    verified_at = func.coalesce(RenewalEvent.verified_at, RenewalEvent.renewed_at)
    latest_renewals = (db.select(
                        ServiceOwnership.id.label('owner_id'), verified_at.label('verified_at'),
                        ClientOperation.pricing_quote.label('quote'),
                        func.row_number().over(partition_by=ServiceOwnership.id,
                                               order_by=(verified_at.desc(), RenewalEvent.id.desc())).label('rank'))
            .select_from(RenewalEvent)
            .join(ServiceOwnership, and_(ServiceOwnership.server_id == RenewalEvent.server_id,
                                         ServiceOwnership.client_uuid == RenewalEvent.client_uuid))
            .outerjoin(ClientOperation, and_(ClientOperation.idempotency_key == RenewalEvent.operation_id,
                                            ClientOperation.state == 'completed'))
            .where(ServiceOwnership.id.in_([row.id for row in owners]), RenewalEvent.verified.is_(True),
                    RenewalEvent.renewed_at >= func.coalesce(ServiceOwnership.verified_at, ServiceOwnership.created_at))
            .subquery())
    verified = {row.owner_id: row for row in db.session.execute(
        db.select(latest_renewals).where(latest_renewals.c.rank == 1))}
    servers = {row.id: row.server.name for row in active_services().filter(
        ServiceOwnership.id.in_([row.id for row in owners])).options(joinedload(ServiceOwnership.server)).all()
               if row.server}
    package_ids = {c.get('package_id') for c in snapshot.values() if type(c.get('package_id')) is int}
    packages = {p.id: p.name for p in Package.query.filter(Package.id.in_(package_ids)).all()} if package_ids else {}
    result = []
    for owner in owners:
        sid, uuid = owner.server_id, owner.client_uuid
        state = states.get(make_service_key(sid, uuid))
        data = snapshot.get((sid, uuid), {})
        raw = data.get('raw_client') or {}
        expiry = data.get('expiryTimestamp', raw.get('expiryTime'))
        expiration = None
        if type(expiry) in (int, float) and 0 < expiry < 253402300799000:
            expiration = datetime.fromtimestamp(expiry / 1000, timezone.utc).replace(tzinfo=None)
        total = data.get('totalGB', raw.get('totalGB'))
        used = None
        if type(data.get('up')) in (int, float) and type(data.get('down')) in (int, float):
            used = data['up'] + data['down']
        numeric_total = total if type(total) in (int, float) else None
        comment = str(data.get('comment', raw.get('comment')) or '')
        package_id = data.get('package_id')
        last_renewal = verified.get(owner.id)
        quote = last_renewal.quote if last_renewal and isinstance(last_renewal.quote, dict) else {}
        package = None
        if type(package_id) is int and package_id in packages:
            package = {'id': package_id, 'name': packages[package_id]}
        elif type(quote.get('package_id')) is int and isinstance(quote.get('package_name'), str):
            package = {'id': quote['package_id'], 'name': quote['package_name']}
        status = data.get('service_state') or 'unknown'
        result.append({
            'service_key': public_service_key(sid, uuid), 'customer_id': owner.customer_id,
            'client_email': masked_name(owner.client_email_snapshot),
            'server_label': servers.get(owner.id), 'status': status,
            'expires_at': timestamp(expiration),
            'days_left': max(0, (expiration - utcnow()).total_seconds() / 86400) if expiration else None,
            'quota': {'total_gb': numeric_total / GIB if numeric_total is not None else None,
                      'used_gb': used / GIB if used is not None else None,
                      'remaining_gb': max(numeric_total - used, 0) / GIB if numeric_total and used is not None else None,
                      'unlimited': numeric_total == 0 if numeric_total is not None else None},
            'lifecycle': {'generation': state.generation if state else None, 'state': status},
            'flags': {tag: bool(re.search(r'#\s*' + tag + r'\b', comment, re.I)) if data else None
                      for tag in ('nosms', 'nopm')},
            'package': package,
            'owner_type': 'reseller' if owner.reseller_id else 'system', 'reseller_id': owner.reseller_id,
            'last_verified_renewal_at': timestamp(last_renewal.verified_at) if last_renewal else None,
        })
    return result


def renewal_rows(owner):
    return RenewalEvent.query.filter_by(server_id=owner.server_id, client_uuid=owner.client_uuid, verified=True).filter(
        RenewalEvent.renewed_at >= (owner.verified_at or owner.created_at))


def renewal_view(row):
    return {'operation_id': public_operation_id(row.operation_id), 'days': row.days,
            'gb': row.volume_bytes / GIB if row.volume_bytes is not None else None,
            'verified': True, 'verified_at': timestamp(row.verified_at), 'renewed_at': timestamp(row.renewed_at)}


def payment_view(row, customer_id, *, sender=None):
    return {'payment_id': -row.id if isinstance(row, Transaction) else row.id,
            'customer_id': customer_id, 'amount': row.amount if sender is None else abs(row.amount),
            'sender_card_hash': card_hash(sender), 'masked_card': masked_digits(sender),
            'recorded_at': timestamp(row.created_at)}


def cash_payments(*columns):
    # Attribute cash only through a completed operation and verified service
    # identity. Email-only finance entries cannot identify the historical payer.
    return (db.select(*columns, ServiceOwnership.customer_id).select_from(Transaction)
            .join(ClientOperation, ClientOperation.transaction_id == Transaction.id)
            .join(RenewalEvent, and_(RenewalEvent.operation_id == ClientOperation.idempotency_key,
                                    RenewalEvent.server_id == Transaction.server_id))
            .join(ServiceOwnership, and_(ServiceOwnership.server_id == RenewalEvent.server_id,
                                         ServiceOwnership.client_uuid == RenewalEvent.client_uuid))
            .where(Transaction.sender_card.is_not(None), RenewalEvent.verified.is_(True),
                    ClientOperation.state == 'completed',
                    ServiceOwnership.revoked_at.is_(None),
                    func.coalesce(ServiceOwnership.verified_at, ServiceOwnership.created_at) <= Transaction.created_at)
            .distinct())


def payment_views(customer_id, since=None):
    wallet = CustomerTransaction.query.filter_by(customer_id=customer_id, status='completed').filter(
        CustomerTransaction.type.in_(('topup', 'purchase', 'renewal')))
    cash = cash_payments(Transaction).where(ServiceOwnership.customer_id == customer_id)
    if since:
        wallet = wallet.filter(CustomerTransaction.created_at >= since)
        cash = cash.where(Transaction.created_at >= since)
    result = [payment_view(row, customer_id) for row in wallet.order_by(
        CustomerTransaction.created_at.desc(), CustomerTransaction.id.desc()).limit(100)]
    result.extend(payment_view(row, customer_id, sender=row.sender_card) for row, _ in db.session.execute(
        cash.order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(100)))
    return sorted(result, key=lambda row: (row['recorded_at'] or '', row['payment_id']), reverse=True)[:100]


def support_views(customer_id):
    requests = (TelegramServiceRequest.query.filter_by(customer_id=customer_id, request_type='support')
                .order_by(TelegramServiceRequest.id.desc()).limit(100).all())
    chats = (db.session.query(SupportChat.service_id, SupportChat.open,
                              func.min(SupportChatMessage.created_at))
             .join(ServiceOwnership, ServiceOwnership.id == SupportChat.service_id)
             .outerjoin(SupportChatMessage, and_(SupportChatMessage.service_id == SupportChat.service_id,
                        SupportChatMessage.created_at >= func.coalesce(ServiceOwnership.verified_at,
                                                                      ServiceOwnership.created_at)))
             .filter(ServiceOwnership.customer_id == customer_id, ServiceOwnership.revoked_at.is_(None))
             .group_by(SupportChat.service_id, SupportChat.open, SupportChat.updated_at)
             .order_by(SupportChat.updated_at.desc()).limit(100).all())
    return [{'request_id': row.id, 'customer_id': customer_id, 'channel': 'telegram',
             'status': row.status, 'subject_masked': masked_name(row.note),
             'created_at': timestamp(row.created_at)} for row in requests] + [
                 {'request_id': request_id, 'customer_id': customer_id, 'channel': 'eve',
                  'status': 'open' if is_open else 'closed',
                  'subject_masked': None, 'created_at': timestamp(created_at)}
                 for request_id, is_open, created_at in chats]


def claim_views(status):
    claims = OwnershipClaim.query.filter_by(status=status).order_by(OwnershipClaim.id.desc()).limit(100).all()
    items = OwnershipClaimItem.query.filter(OwnershipClaimItem.claim_id.in_([c.id for c in claims])).order_by(
        OwnershipClaimItem.id).all() if claims else []
    first = {}
    for row in items:
        if row.client_uuid and row.claim_id not in first:
            first[row.claim_id] = public_service_key(row.server_id, row.client_uuid)
    return [{'claim_id': row.id, 'customer_id': row.customer_id, 'service_key': first.get(row.id),
             'status': row.status, 'evidence_kind': row.claim_method, 'phone_masked': masked_digits(row.verified_phone),
             'created_at': timestamp(row.created_at)} for row in claims]


def knowledge_view():
    # Export approved content. Executable embeds can contain integration credentials.
    return {
        'faqs': [{'id': r.id, 'title': r.title, 'content': r.content, 'platform': r.platform}
                 for r in FAQ.query.filter_by(is_enabled=True).order_by(FAQ.id).limit(100)],
        'renew_templates': [{'id': r.id, 'name': r.name, 'content': r.content}
                            for r in RenewTemplate.query.filter_by(is_active=True).order_by(RenewTemplate.id).limit(100)],
        'notification_templates': [{'id': r.id, 'name': r.name, 'type': r.type, 'content': r.content}
                                   for r in NotificationTemplate.query.filter_by(is_active=True, owner_id=None)
                                   .order_by(NotificationTemplate.id).limit(100)],
        'online_chat_scripts': [{'id': r.id, 'name': r.name, 'is_active': True}
                                for r in OnlineChatScript.query.filter_by(is_active=True)
                                .order_by(OnlineChatScript.id).limit(100)],
    }


def notification_views(owner):
    key = make_service_key(owner.server_id, owner.client_uuid)
    return [{'notification_id': row.id, 'status': row.status, 'state': row.state,
             'recipient_masked': masked_digits(row.recipient), 'created_at': timestamp(row.created_at)}
            for row in SmsSendLog.query.filter_by(service_key=key).filter(
                SmsSendLog.created_at >= (owner.verified_at or owner.created_at)).order_by(SmsSendLog.id.desc()).limit(20)]
