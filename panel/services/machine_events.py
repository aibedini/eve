"""Transactional outbox for versioned, metadata-only God AI events."""
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from sqlalchemy import event, inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from panel.core.advisory_lock import resource_lock
from panel.extensions import db
from panel.models import (ClientOperation, CustomerAccount, OwnershipClaim, RenewalEvent,
                          ServiceLifecycleState, ServiceOwnership, Transaction)
from panel.models.machine import MachineEvent, MachinePhoneLookup
from panel.models.support import SupportChat
from panel.models.telegram import CustomerTransaction, TelegramServiceRequest
from panel.services.machine_security import (configured_secret, phone_lookup_hash, public_operation_id,
                                             public_service_key, timestamp, utcnow)

logger = logging.getLogger(__name__)
DESTINATION = '/api/v1/integrations/eve/events'


def webhook_config():
    base = os.environ.get('EVE_GOD_AI_ORIGIN', '').strip()
    secret = configured_secret('EVE_GOD_AI_WEBHOOK_SECRET')
    if not base or not secret or not configured_secret('EVE_MACHINE_SERVICE_KEY_SECRET'):
        return None
    try:
        parts = urlsplit(base)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.path not in ('', '/') or parts.query or parts.fragment
                or not re.fullmatch(r'[A-Za-z0-9.-]+', parts.hostname)):
            return None
        # Validate the configured port before letting requests build the URL.
        _ = parts.port
    except ValueError:
        return None
    return base.rstrip('/') + DESTINATION, secret


def signature(secret, unix_seconds, delivery_id, body):
    prefix = f'{unix_seconds}.{delivery_id}.'.encode('ascii')
    return 'v1=' + hmac.digest(secret, prefix + body, 'sha256').hex()


def event_row(event_type, data, when=None, dedupe=None):
    now = utcnow()
    identity = hashlib.sha256(f'{event_type}.{dedupe}'.encode()).hexdigest()[:32] if dedupe else uuid4().hex
    event_id = 'evt_' + identity
    return {'id': event_id, 'next_attempt_at': now, 'attempts': 0,
            'body': json.dumps({'event_id': event_id, 'event_type': f'eve.{event_type}.v1',
                                'occurred_at': timestamp(when or now), 'data': data},
                               ensure_ascii=True, separators=(',', ':'))}


def _service_ref(server_id, identity):
    if not server_id or not identity:
        return None
    return public_service_key(server_id, identity)


def _change(row, attribute):
    return inspect(row).attrs[attribute].history.has_changes()


def _events(row, new, deleted, connection):
    if isinstance(row, ServiceLifecycleState) and (new or _change(row, 'generation')) and row.generation > 0:
        key = _service_ref(row.server_id, row.client_uuid)
        if key:
            state = getattr(row, 'observed_state_for_machine_event', None)
            state = 'expiring' if state == 'expiring_soon' else state
            if state not in ('active', 'inactive', 'expired', 'expiring', 'volume_low', 'volume_ended'):
                state = 'unknown'
            yield event_row('service.lifecycle_changed', {
                'service_key': key, 'generation': row.generation,
                'state': state,
            }, row.last_lifecycle_change_at)
    elif isinstance(row, RenewalEvent) and row.verified and (new or _change(row, 'verified')):
        key = _service_ref(row.server_id, row.client_uuid)
        if key:
            yield event_row('renewal.verified', {'service_key': key, 'operation_id': public_operation_id(row.operation_id),
                                                'days': row.days, 'gb': row.volume_bytes / (1024 ** 3)
                                                if row.volume_bytes is not None else None}, row.verified_at)
    elif isinstance(row, ServiceOwnership) and (new or deleted or _change(row, 'customer_id') or _change(row, 'revoked_at')):
        key = _service_ref(row.server_id, row.client_uuid)
        if key:
            history = inspect(row).attrs.customer_id.history
            for previous in history.deleted:
                yield event_row('ownership.changed', {'service_key': key, 'customer_id': previous,
                                                        'relation': 'owner', 'action': 'revoked'})
            yield event_row('ownership.changed', {'service_key': key, 'customer_id': row.customer_id,
                                                    'relation': 'owner',
                                                    'action': 'revoked' if deleted or row.revoked_at else 'assigned'})
    elif isinstance(row, OwnershipClaim) and new:
        yield event_row('ownership_claim.created', {'claim_id': row.id, 'evidence_kind': row.claim_method}, row.created_at)
    elif isinstance(row, TelegramServiceRequest) and new and row.request_type == 'support':
        yield event_row('support_request.created', {'request_id': row.id, 'customer_id': row.customer_id,
                                                     'channel': 'telegram'}, row.created_at)
    elif isinstance(row, SupportChat) and new:
        table = ServiceOwnership.__table__
        customer_id = connection.execute(db.select(table.c.customer_id).where(table.c.id == row.service_id)).scalar()
        if customer_id is not None:
            yield event_row('support_request.created', {'request_id': row.service_id, 'customer_id': customer_id,
                                                         'channel': 'eve'}, row.updated_at)
    elif (isinstance(row, CustomerTransaction) and row.type in ('topup', 'purchase', 'renewal')
          and row.status == 'completed' and (new or _change(row, 'status'))):
        yield event_row('payment.recorded', {'payment_id': row.id, 'customer_id': row.customer_id,
                                             'amount': row.amount, 'sender_card_hash': None}, row.created_at)


def sync_phone_lookup(connection, customer_id, phone, pepper):
    digest = phone_lookup_hash(phone, pepper)
    table = MachinePhoneLookup.__table__
    if digest:
        insert = pg_insert if connection.dialect.name == 'postgresql' else sqlite_insert
        query = insert(table).values(customer_id=customer_id, phone_hash=digest)
        connection.execute(query.on_conflict_do_update(
            index_elements=[table.c.customer_id], set_={'phone_hash': query.excluded.phone_hash}))
    else:
        connection.execute(table.delete().where(table.c.customer_id == customer_id))


@event.listens_for(Session, 'after_flush')
def capture_machine_changes(session, _flush):
    pepper = configured_secret('EVE_MACHINE_PHONE_PEPPER')
    push = webhook_config() is not None
    if not pepper and not push:
        return
    connection = session.connection()
    rows = []
    operation_ids = set()
    for row in session.new | session.dirty | session.deleted:
        new = row in session.new
        deleted = row in session.deleted
        if pepper and isinstance(row, CustomerAccount) and (new or deleted or _change(row, 'primary_phone')):
            sync_phone_lookup(connection, row.id, None if deleted else row.primary_phone, pepper)
        if push:
            rows.extend(_events(row, new, deleted, connection))
            if (isinstance(row, ClientOperation) and row.transaction_id
                    and (new or _change(row, 'transaction_id') or _change(row, 'state'))):
                operation_ids.add(row.transaction_id)
            if isinstance(row, RenewalEvent) and row.verified and (new or _change(row, 'verified')) and row.operation_id:
                linked = connection.execute(db.select(ClientOperation.__table__.c.transaction_id).where(
                    ClientOperation.__table__.c.idempotency_key == row.operation_id)).scalar()
                if linked:
                    operation_ids.add(linked)
    if operation_ids:
        from panel.services.machine_context import card_hash, cash_payments
        query = cash_payments(Transaction.id, Transaction.amount, Transaction.sender_card, Transaction.created_at)
        for tx_id, amount, card, created_at, customer_id in connection.execute(query.where(Transaction.id.in_(operation_ids))):
            rows.append(event_row('payment.recorded', {'payment_id': -tx_id, 'customer_id': customer_id,
                                                       'amount': abs(amount), 'sender_card_hash': card_hash(card)},
                                  created_at, dedupe=f'cash:{tx_id}'))
    if rows:
        insert = pg_insert if connection.dialect.name == 'postgresql' else sqlite_insert
        connection.execute(insert(MachineEvent.__table__).on_conflict_do_nothing(index_elements=['id']), rows)


def record_opt_out(server_id, uuid, before, after):
    """Record only changed comment flags after panel verification."""
    if not webhook_config() or not server_id or not uuid:
        return
    owner = ServiceOwnership.query.filter_by(server_id=server_id, client_uuid=uuid, revoked_at=None).first()
    if owner is None:
        return
    rows = []
    for tag, channel in (('nosms', 'sms'), ('nopm', 'pm')):
        pattern = r'#\s*' + tag + r'\b'
        old = bool(re.search(pattern, before or '', re.I))
        new = bool(re.search(pattern, after or '', re.I))
        if old != new:
            rows.append(event_row('customer.opt_out_changed',
                                  {'customer_id': owner.customer_id, 'channel': channel, 'value': new}))
    if rows:
        db.session.execute(MachineEvent.__table__.insert(), rows)
    return bool(rows)


def deliver_due(http, url, secret, count=50, clock=time.time):
    ca_file = (os.environ.get('EVE_GOD_AI_CA_FILE') or '').strip()
    for _ in range(count):
        row = (MachineEvent.query.filter(MachineEvent.delivered_at.is_(None), MachineEvent.next_attempt_at <= utcnow())
               .order_by(MachineEvent.next_attempt_at, MachineEvent.id).first())
        if not row:
            db.session.rollback()
            return
        delivery_id, body = row.id, row.body.encode('utf-8')
        db.session.rollback()  # No transaction is held open during HTTP.
        unix_seconds = str(int(clock()))
        delivered = False
        try:
            with http.post(url, data=body, headers={
                'Content-Type': 'application/json', 'X-Eve-Delivery-Id': delivery_id,
                'X-Eve-Timestamp': unix_seconds, 'X-Eve-Signature': signature(secret, unix_seconds, delivery_id, body),
            }, timeout=(3, 8), allow_redirects=False, stream=True, verify=ca_file or True) as response:
                delivered = 200 <= response.status_code < 300
        except requests.RequestException:
            pass
        except OSError:
            logger.warning('GOD AI webhook CA file is unavailable')
        row = db.session.get(MachineEvent, delivery_id)
        row.attempts += 1
        if delivered:
            row.delivered_at = utcnow()
        else:
            row.next_attempt_at = utcnow() + timedelta(seconds=min(3600, 2 ** min(row.attempts, 12)))
        db.session.commit()


def machine_worker():
    from app import app
    from panel.core.redis_client import load_snapshot_from_redis
    from panel.services.machine_inventory import rebuild_service_index

    cursor = 0
    current_pepper = None
    backfill_complete = False
    with requests.Session() as http:
        http.trust_env = False
        while True:
            try:
                with app.app_context(), resource_lock('machine-integration') as owner:
                    if owner:
                        pepper = configured_secret('EVE_MACHINE_PHONE_PEPPER')
                        if configured_secret('EVE_MACHINE_SERVICE_KEY_SECRET'):
                            load_snapshot_from_redis()
                            rebuild_service_index(pepper)
                        if pepper != current_pepper:
                            cursor, current_pepper, backfill_complete = 0, pepper, False
                        if pepper and not backfill_complete:
                            # Serialize with concurrent phone edits on PostgreSQL; write
                            # from the customer row selected in this transaction.
                            rows = (CustomerAccount.query.filter(CustomerAccount.id > cursor)
                                    .order_by(CustomerAccount.id).limit(500).with_for_update().all())
                            for row in rows:
                                sync_phone_lookup(db.session.connection(), row.id, row.primary_phone, pepper)
                            next_cursor = rows[-1].id if rows else cursor
                            db.session.commit()
                            cursor, backfill_complete = next_cursor, len(rows) < 500
                        if config := webhook_config():
                            deliver_due(http, *config)
                        MachineEvent.query.filter(MachineEvent.delivered_at < utcnow() - timedelta(days=7)).delete()
                        db.session.commit()
            except Exception as exc:
                logger.error('Machine integration worker failed (%s)', type(exc).__name__)
            time.sleep(3 if cursor and not backfill_complete else 10)
