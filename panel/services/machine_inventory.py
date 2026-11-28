"""Service inventory assembled from panel snapshots and EVE ownership records."""
import hashlib
import hmac
import json
from collections import defaultdict

from sqlalchemy import and_, literal

from panel.core.phone import _extract_iran_mobile_from_text
from panel.core.redis_client import GLOBAL_REFRESH_LOCK, GLOBAL_SERVER_DATA
from panel.extensions import db
from panel.models import ClientOwnership, Server, ServiceOwnership
from panel.models.machine import MachineServiceIndex
from panel.services.lifecycle import resolve_client_uuid
from panel.services.machine_security import configured_secret, phone_lookup_hash, utcnow


def email_lookup_hash(email):
    secret = configured_secret('EVE_MACHINE_SERVICE_KEY_SECRET')
    if not email or not secret:
        return None
    return hmac.digest(secret, f'eve-machine-email-v1\0{email.strip().lower()}'.encode(), 'sha256').hex()


def contact_pepper_tag(pepper):
    return hmac.digest(pepper, b'eve-machine-service-contact-v1', 'sha256').hex() if pepper else None


def legacy_identity(row_id):
    return f'~legacy:{row_id}'


def snapshot_identity(client, inbound_id):
    identity = resolve_client_uuid(client.get('raw_client')) or resolve_client_uuid(client)
    if identity:
        return identity if len(identity) <= 100 else '~identity:' + hashlib.sha256(identity.encode()).hexdigest()
    email = str(client.get('email') or '').strip().lower()
    if email:
        return '~snapshot:' + hashlib.sha256(f'{inbound_id}\0{email}'.encode()).hexdigest()
    return None


def _contact(email, comment, pepper):
    return phone_lookup_hash(_extract_iran_mobile_from_text(email, comment), pepper) if pepper else None


def rebuild_service_index(pepper):
    """Reconcile after snapshot publication. An unavailable panel keeps its last observation."""
    secret = configured_secret('EVE_MACHINE_SERVICE_KEY_SECRET')
    if not secret:
        return
    with GLOBAL_REFRESH_LOCK:
        inbounds = list(GLOBAL_SERVER_DATA.get('inbounds') or [])
        complete_servers = {int(row['server_id']) for row in GLOBAL_SERVER_DATA.get('servers_status') or []
                            if row.get('server_id') is not None and row.get('success') is True}
        if GLOBAL_SERVER_DATA.get('_snapshot_complete') is False:
            complete_servers.clear()
    server_ids = set(db.session.scalars(db.select(Server.id)))
    pepper_tag = contact_pepper_tag(pepper)
    existing = {(row.server_id, row.client_uuid): row for row in MachineServiceIndex.query.all()}
    desired = {}
    by_email = defaultdict(dict)
    for inbound in inbounds:
        try:
            sid = int(inbound.get('server_id'))
        except (TypeError, ValueError):
            continue
        if sid not in server_ids:
            continue
        inbound_id = str(inbound.get('id'))
        for client in inbound.get('clients') or []:
            identity = snapshot_identity(client, inbound_id)
            if not identity:
                continue
            email = str(client.get('email') or '').strip()
            digest = email_lookup_hash(email)
            if digest:
                by_email[(sid, digest)].setdefault(identity, set()).add(inbound_id)
            key = sid, identity
            if key in desired:
                continue
            raw = client.get('raw_client') or {}
            comment = client.get('comment', raw.get('comment')) or ''
            projection = json.dumps([
                client.get('enable', raw.get('enable')), client.get('expiryTimestamp', raw.get('expiryTime')),
                client.get('totalGB', raw.get('totalGB')), client.get('service_state'), client.get('package_id'),
                comment,
            ], default=str).encode()
            revision = hmac.digest(secret, b'eve-machine-inventory-v1\0' + projection, 'sha256').hex()
            desired[key] = dict(email_hash=digest, phone_hash=_contact(email, comment, pepper), contact_pepper=pepper_tag,
                                snapshot_revision=revision, legacy_id=None, reseller_id=None)

    for key, row in existing.items():
        if row.server_id in server_ids and row.server_id not in complete_servers and row.snapshot_revision:
            retained_hash = row.phone_hash if row.contact_pepper == pepper_tag else None
            desired.setdefault(key, dict(email_hash=row.email_hash, phone_hash=retained_hash, contact_pepper=pepper_tag,
                                         snapshot_revision=row.snapshot_revision, legacy_id=None, reseller_id=None))

    owners = ServiceOwnership.query.filter_by(revoked_at=None).all()
    owned_by_email = defaultdict(set)
    for row in owners:
        digest = email_lookup_hash(row.client_email_snapshot)
        if digest:
            owned_by_email[(row.server_id, digest)].add(row.client_uuid)

    previous_aliases = {row.legacy_id: key for key, row in existing.items() if row.legacy_id is not None}
    for row in ClientOwnership.query.order_by(ClientOwnership.id).all():
        if row.server_id not in server_ids:
            continue
        digest = email_lookup_hash(row.client_email)
        identity = row.client_uuid
        if not identity:
            candidates = by_email[(row.server_id, digest)]
            matching = [uuid for uuid, ids in candidates.items()
                        if row.inbound_id is None or str(row.inbound_id) in ids]
            if not candidates:
                matching = list(owned_by_email[(row.server_id, digest)])
            previous = previous_aliases.get(row.id)
            if len(matching) == 1:
                identity = matching[0]
            elif previous in desired and desired[previous]['email_hash'] == digest:
                identity = previous[1]
            else:
                identity = legacy_identity(row.id)
        entry = desired.setdefault((row.server_id, identity), dict(
            email_hash=digest, phone_hash=_contact(row.client_email, None, pepper),
            contact_pepper=pepper_tag, snapshot_revision=None))
        entry.update(legacy_id=row.id, reseller_id=row.reseller_id)

    for row in owners:
        if row.server_id not in server_ids:
            continue
        desired.setdefault((row.server_id, row.client_uuid), dict(
            email_hash=email_lookup_hash(row.client_email_snapshot),
            phone_hash=_contact(row.client_email_snapshot, None, pepper),
            contact_pepper=pepper_tag, snapshot_revision=None, legacy_id=None, reseller_id=None,
        ))

    now = utcnow()
    for key, values in desired.items():
        row = existing.pop(key, None)
        if row is None:
            db.session.add(MachineServiceIndex(server_id=key[0], client_uuid=key[1], updated_at=now, **values))
        elif any(getattr(row, name) != value for name, value in values.items()):
            for name, value in values.items():
                setattr(row, name, value)
            row.updated_at = now
    for row in existing.values():
        db.session.delete(row)
    db.session.commit()


def service_inventory():
    index = MachineServiceIndex
    owner = ServiceOwnership
    active = owner.revoked_at.is_(None)
    owned_since = db.case((active, db.func.coalesce(owner.verified_at, owner.created_at))).label('owned_since')
    same_service = and_(owner.server_id == index.server_id, owner.client_uuid == index.client_uuid)
    indexed = db.select(
        index.server_id, index.client_uuid, db.case((active, owner.customer_id)).label('customer_id'),
        db.func.coalesce(db.case((active, owner.reseller_id)), index.reseller_id).label('reseller_id'),
        db.case((active, owner.client_email_snapshot)).label('client_email_snapshot'),
        index.email_hash, index.phone_hash, index.contact_pepper, index.legacy_id,
        owned_since,
        db.case((owner.updated_at > index.updated_at, owner.updated_at), else_=index.updated_at).label('updated_at'),
    ).select_from(index).outerjoin(owner, same_service)
    # Registered customer services remain readable immediately after creation.
    direct = db.select(
        owner.server_id, owner.client_uuid, owner.customer_id, owner.reseller_id,
        owner.client_email_snapshot, literal(None), literal(None), literal(None), literal(None), owned_since, owner.updated_at,
    ).where(active, ~db.exists(db.select(index.server_id).where(same_service)))
    return indexed.union_all(direct).subquery()
