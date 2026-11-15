"""Read-only projections and machine-credential helpers for the EVE Context API v1.

This module is the business layer behind ``panel/routes/machine_api.py``. It exists
so the blueprint stays a thin HTTP shell and every projection can be tested without
a request:

* machine-key issuance / authentication (``eve_mk_<id>.<secret>``);
* the masked, versioned projections of customers, services, renewals, payments,
  support requests, ownership claims, knowledge and SMS notifications;
* the phone lookup hash (``HMAC(pepper, normalize_iran_mobile(phone))``).

Read-only rule: the only database write in this module is
:func:`touch_machine_key`, which updates ``machine_api_keys.last_used_at`` and
nothing else. Customer, service, ownership and finance rows are never mutated.
``subscription_link`` is never part of a projection.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import func, or_, update

from panel.core.finance_privacy import mask_card_number
from panel.core.phone import normalize_iran_mobile
from panel.core.redis_client import GLOBAL_REFRESH_LOCK, GLOBAL_SERVER_DATA
from panel.extensions import db
from panel.models import (
    ClientOwnership,
    CustomerAccount,
    FAQ,
    MachineApiKey,
    NotificationTemplate,
    OnlineChatScript,
    OwnershipClaim,
    Payment,
    Package,
    RenewalEvent,
    RenewTemplate,
    Server,
    ServiceLifecycleState,
    ServiceNotificationEvent,
    ServiceNotificationOutbox,
    ServiceObservedState,
    ServiceOwnership,
    SmsSendLog,
    TelegramIdentity,
    TelegramServiceRequest,
    Transaction,
)
from panel.security import digest_presented_token
from panel.services.lifecycle import make_service_key, resolve_client_uuid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
SCOPE_CUSTOMER_READ = 'context.customer.read'
SCOPE_SERVICE_READ = 'context.service.read'
SCOPE_RENEWAL_READ = 'context.renewal.read'
SCOPE_PAYMENT_READ_MASKED = 'context.payment.read_masked'
SCOPE_SUPPORT_READ = 'context.support.read'
SCOPE_KNOWLEDGE_READ = 'context.knowledge.read'
SCOPE_EVENTS_SUBSCRIBE = 'events.subscribe'
# GOD AI submits proposals (XR-GA-019): `proposals.write` may only create a
# pending proposal, never act on EVE; `proposals.read` reads the queue back.
SCOPE_PROPOSALS_READ = 'proposals.read'
SCOPE_PROPOSALS_WRITE = 'proposals.write'

KNOWN_SCOPES = frozenset({
    SCOPE_CUSTOMER_READ,
    SCOPE_SERVICE_READ,
    SCOPE_RENEWAL_READ,
    SCOPE_PAYMENT_READ_MASKED,
    SCOPE_SUPPORT_READ,
    SCOPE_KNOWLEDGE_READ,
    SCOPE_EVENTS_SUBSCRIBE,
    SCOPE_PROPOSALS_READ,
    SCOPE_PROPOSALS_WRITE,
})

SCHEMA_HEALTH = 'eve.context.health.v1'
SCHEMA_CUSTOMER = 'eve.context.customer.v1'
SCHEMA_CUSTOMER_LIST = 'eve.context.customer_list.v1'
SCHEMA_SERVICE = 'eve.context.service.v1'
SCHEMA_SERVICE_LIST = 'eve.context.service_list.v1'
SCHEMA_RENEWALS = 'eve.context.renewals.v1'
SCHEMA_PAYMENTS = 'eve.context.payments.v1'
SCHEMA_SUPPORT = 'eve.context.support_requests.v1'
SCHEMA_CLAIMS = 'eve.context.ownership_claims.v1'
SCHEMA_KNOWLEDGE = 'eve.context.knowledge.v1'
SCHEMA_SMS = 'eve.context.sms_notifications.v1'
SCHEMA_PROPOSALS = 'eve.context.proposals.v1'

TOKEN_PREFIX = 'eve_mk_'
TOKEN_HASH_PURPOSE = 'machine-api-key'
_TOKEN_RE = re.compile(r'^eve_mk_([1-9][0-9]{0,9})\.([A-Za-z0-9_-]{20,128})$')
_SERVICE_KEY_RE = re.compile(r'^eve:([0-9]{1,10}):(.{1,200})$')
_HEX_64_RE = re.compile(r'^[0-9a-f]{64}$')
_OPT_OUT_RE = re.compile(r'#\s*(nosms|nopm)(?![a-z0-9])', re.IGNORECASE)

PHONE_PEPPER_ENV = 'MACHINE_PHONE_PEPPER'
MIN_PHONE_PEPPER_LENGTH = 16
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
MAX_LIST_ROWS = 500
GB_BYTES = float(1024 ** 3)


class MachineApiError(Exception):
    """An HTTP-safe failure raised by this service."""

    code = 'invalid_request'
    status = 400

    def __init__(self, message=None, *, code=None, status=None, **extra):
        self.code = code or self.code
        self.status = status or self.status
        self.message = message or self.code
        self.extra = extra
        super().__init__(self.message)


class MachineApiValidationError(MachineApiError):
    code = 'invalid_request'
    status = 400


class MachineApiNotFound(MachineApiError):
    code = 'not_found'
    status = 404


class MachineApiConfigError(MachineApiError):
    code = 'machine_api_not_configured'
    status = 503


class MachineAuthError(MachineApiError):
    """Authentication/authorization failure for a machine bearer token."""

    code = 'unauthorized'
    status = 401


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def utcnow() -> datetime:
    """Naive UTC, matching every other EVE model column."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() + 'Z'
    return str(value)


def iso_now() -> str:
    return iso_utc(utcnow())


def envelope(schema_version: str, **fields) -> dict:
    """The stable response envelope shared by every Context API projection."""
    payload = {
        'schema_version': schema_version,
        'source': 'eve',
        'fetched_at': iso_now(),
    }
    payload.update(fields)
    return payload


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------
def mask_phone(value) -> str | None:
    """First four / last three digits; never enough to redial."""
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        return None
    if len(digits) >= 7:
        return f'{digits[:4]}***{digits[-3:]}'
    return '***'


def mask_client_email(value) -> str | None:
    """Mask the local part and drop the domain (the domain adds no context value).

    VPN client identifiers are often usernames without ``@`` (the contract sample
    ``g276-0912***01`` is exactly that shape); an actual e-mail is masked the same
    way and never returned in full.
    """
    text = str(value or '').strip()
    if not text:
        return None
    local = text.split('@', 1)[0]
    if len(local) >= 7:
        return f'{local[:4]}***{local[-2:]}'
    if len(local) >= 3:
        return f'{local[0]}***{local[-1]}'
    return f'{local[:1]}***' if local else None


def mask_person_name(value) -> str | None:
    text = str(value or '').strip()
    if not text:
        return None
    if len(text) <= 2:
        return f'{text[:1]}***'
    return f'{text[0]}***{text[-1]}'


# ---------------------------------------------------------------------------
# Machine credentials
# ---------------------------------------------------------------------------
def hash_machine_token(token: str) -> str:
    """Domain-separated digest of a full ``eve_mk_<id>.<secret>`` token."""
    digest = digest_presented_token(token, TOKEN_HASH_PURPOSE)
    if not digest:
        raise MachineApiValidationError('Invalid machine token', code='invalid_key')
    return digest


def parse_machine_token(token) -> tuple[int, str] | None:
    match = _TOKEN_RE.fullmatch(str(token or '').strip())
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def create_machine_api_key(name, scopes, *, expires_at=None, created_by=None,
                           commit=True):
    """Issue a machine key and return ``(row, plaintext_token)``.

    The plaintext token is returned exactly once and is never persisted; callers
    must show it to the operator immediately. Unknown scopes are rejected so a
    typo cannot silently produce a key that authorizes nothing.
    """
    clean_name = str(name or '').strip()
    if not clean_name or len(clean_name) > 120:
        raise MachineApiValidationError(
            'Key name is required (max 120 characters)', code='invalid_name')
    wanted = sorted({str(scope).strip() for scope in (scopes or []) if str(scope).strip()})
    unknown = sorted(set(wanted) - KNOWN_SCOPES)
    if unknown:
        raise MachineApiValidationError(
            'Unknown scope(s): %s' % ', '.join(unknown), code='invalid_scope')
    if not wanted:
        raise MachineApiValidationError('At least one scope is required', code='invalid_scope')

    normalized_expiry = None
    if expires_at is not None:
        if not isinstance(expires_at, datetime):
            raise MachineApiValidationError('expires_at must be a datetime', code='invalid_expires_at')
        normalized_expiry = (
            expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if expires_at.tzinfo is not None else expires_at
        )

    secret = secrets.token_urlsafe(32)
    row = MachineApiKey(
        name=clean_name,
        # Temporary unique placeholder replaced below, once the id exists.
        key_hash='pending-' + secrets.token_hex(16),
        scopes=json.dumps(wanted, separators=(',', ':')),
        expires_at=normalized_expiry,
        created_by=(str(created_by).strip()[:100] if created_by else None),
    )
    db.session.add(row)
    try:
        db.session.flush()
        token = f'{TOKEN_PREFIX}{row.id}.{secret}'
        row.key_hash = hash_machine_token(token)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception:
        db.session.rollback()
        raise
    return row, token


def authenticate_machine_token(token, required_scope=None, *, now=None):
    """Return the enabled ``MachineApiKey`` for a valid token, or raise.

    The unique-index lookup is followed by an explicit constant-time digest
    comparison, so a database row can never be accepted because of a partial
    match and the check does not leak how much of the digest matched.
    """
    parsed = parse_machine_token(token)
    if parsed is None:
        raise MachineAuthError('Invalid bearer token', code='invalid_key')
    key_id, _secret = parsed
    try:
        digest = hash_machine_token(str(token or '').strip())
    except MachineApiValidationError:
        raise MachineAuthError('Invalid bearer token', code='invalid_key')
    except Exception:
        logger.exception('machine API token hashing is not configured')
        raise MachineApiConfigError(
            'Machine API token hashing is not configured',
            code='authentication_not_configured')

    row = MachineApiKey.query.filter_by(key_hash=digest).first()
    if row is None or row.id != key_id or not hmac.compare_digest(
            str(row.key_hash or ''), digest):
        raise MachineAuthError('Invalid bearer token', code='invalid_key')
    if row.is_revoked():
        raise MachineAuthError('This machine key has been revoked', code='revoked_key')
    if row.is_expired(now):
        raise MachineAuthError('This machine key has expired', code='expired_key')
    if required_scope and required_scope not in row.scope_set():
        raise MachineAuthError(
            'The key lacks the required scope', code='insufficient_scope',
            status=403, required_scope=required_scope)
    return row


def touch_machine_key(key, *, now=None):
    """Record usage. This is the ONLY write the machine API performs."""
    if key is None or getattr(key, 'id', None) is None:
        return
    try:
        db.session.execute(
            update(MachineApiKey)
            .where(MachineApiKey.id == key.id)
            .values(last_used_at=now or utcnow())
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.warning('machine API could not update last_used_at for key %s',
                       getattr(key, 'id', None), exc_info=True)


# ---------------------------------------------------------------------------
# Phone lookup
# ---------------------------------------------------------------------------
def phone_pepper() -> str:
    pepper = (os.environ.get(PHONE_PEPPER_ENV) or '').strip()
    if not pepper:
        raise MachineApiConfigError(
            f'{PHONE_PEPPER_ENV} is not configured', code='phone_lookup_not_configured')
    if len(pepper) < MIN_PHONE_PEPPER_LENGTH:
        # A short shared pepper is an operator risk, not a reason to break the
        # read-only lookup; the deployment guide asks for at least 16 characters.
        logger.warning('%s is shorter than the recommended %d characters',
                       PHONE_PEPPER_ENV, MIN_PHONE_PEPPER_LENGTH)
    return pepper


def phone_lookup_hash(value, *, pepper=None) -> str:
    """``HMAC-SHA256(pepper, normalize_iran_mobile(value))`` as lowercase hex."""
    canonical = normalize_iran_mobile(value)
    if not canonical:
        raise MachineApiValidationError('Not a valid Iranian mobile number',
                                        code='invalid_phone')
    key = (pepper or phone_pepper()).encode('utf-8')
    return hmac.new(key, canonical.encode('ascii'), hashlib.sha256).hexdigest()


def _valid_phone_hash(value) -> bool:
    return bool(_HEX_64_RE.fullmatch(str(value or '').strip().lower()))


def _hash_matches(canonical_phone, presented_hash, pepper) -> bool:
    if not canonical_phone:
        return False
    digest = hmac.new(pepper.encode('utf-8'), canonical_phone.encode('ascii'),
                      hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, presented_hash)


def lookup_customers_by_phone_hash(phone_hash, *, pepper=None):
    """Return 0..n customers whose normalized phone hashes to ``phone_hash``.

    ``match_basis`` names which stored identity matched. Multiple customers are a
    real answer (the contract requires explicit ambiguity), never an error.

    Only the two identity columns are read while hashing, then the matched
    customers are loaded by primary key. That keeps the unavoidable scan (a
    deterministic HMAC cannot be indexed) narrow without silently truncating the
    result set.
    """
    presented = str(phone_hash or '').strip().lower()
    if not presented:
        raise MachineApiValidationError('phone_hash is required', code='phone_hash_required')
    if not _valid_phone_hash(presented):
        raise MachineApiValidationError('phone_hash must be 64 hex characters',
                                        code='invalid_phone_hash')
    secret = pepper or phone_pepper()

    matched: dict[int, str] = {}
    accounts = (db.session.query(CustomerAccount.id, CustomerAccount.primary_phone)
                .filter(CustomerAccount.primary_phone.isnot(None)).all())
    for customer_id, primary_phone in accounts:
        if _hash_matches(normalize_iran_mobile(primary_phone), presented, secret):
            matched[customer_id] = 'customer_phone'

    identities = (db.session.query(
        TelegramIdentity.customer_id, TelegramIdentity.phone_normalized)
        .filter(TelegramIdentity.phone_normalized.isnot(None),
                TelegramIdentity.phone_verified_at.isnot(None),
                TelegramIdentity.customer_id.isnot(None)).all())
    for customer_id, phone_normalized in identities:
        if customer_id in matched:
            continue
        if _hash_matches(normalize_iran_mobile(phone_normalized), presented, secret):
            matched[customer_id] = 'telegram_verified_phone'

    if not matched:
        return []
    customers = {row.id: row for row in CustomerAccount.query.filter(
        CustomerAccount.id.in_(sorted(matched))).all()}
    return [{'customer': customers[customer_id], 'match_basis': matched[customer_id]}
            for customer_id in sorted(matched) if customer_id in customers]


# ---------------------------------------------------------------------------
# Service identity / snapshot helpers
# ---------------------------------------------------------------------------
def parse_service_key(value) -> tuple[int, str]:
    match = _SERVICE_KEY_RE.fullmatch(str(value or '').strip())
    if not match:
        raise MachineApiValidationError(
            'service_key must be eve:<server_id>:<client_uuid>',
            code='invalid_service_key')
    identity = match.group(2).strip()
    if not identity or '/' in identity or '\x00' in identity:
        raise MachineApiValidationError('Invalid service identity',
                                        code='invalid_service_key')
    return int(match.group(1)), identity


def _snapshot_clients():
    with GLOBAL_REFRESH_LOCK:
        inbounds = list(GLOBAL_SERVER_DATA.get('inbounds') or [])
    for inbound in inbounds:
        try:
            server_id = int(inbound.get('server_id') or 0)
        except (TypeError, ValueError):
            continue
        if not server_id:
            continue
        for client in (inbound.get('clients') or []):
            if isinstance(client, dict):
                yield server_id, client


def live_client(server_id, identity):
    """Best-effort live cached client row for a service identity.

    Reads the process-local shared snapshot only (no panel/Redis round trip): the
    machine API must stay a fast read-only surface.
    """
    wanted = str(identity or '').strip()
    wanted_l = wanted.lower()
    email_match = '@' in wanted or not re.fullmatch(r'[0-9a-fA-F-]{16,}', wanted)
    for sid, client in _snapshot_clients():
        if sid != int(server_id):
            continue
        uuid = resolve_client_uuid(client)
        if uuid and (uuid == wanted or uuid.lower() == wanted_l):
            return client
        if str(client.get('subId') or '').strip().lower() == wanted_l:
            return client
        if email_match and str(client.get('email') or '').strip().lower() == wanted_l:
            return client
    return None


def _server_status(server_id):
    with GLOBAL_REFRESH_LOCK:
        statuses = list(GLOBAL_SERVER_DATA.get('servers_status') or [])
    for status in statuses:
        try:
            if int(status.get('server_id')) != int(server_id):
                continue
        except (TypeError, ValueError, AttributeError):
            continue
        if 'success' in status:
            return 'online' if status.get('success') is True else 'offline'
        if 'reachable' in status:
            return 'online' if status.get('reachable') is True else 'offline'
        return 'unknown'
    return 'unknown'


def _server_projection(server_id):
    server = db.session.get(Server, int(server_id))
    return {
        'id': int(server_id),
        'label': server.name if server is not None else None,
        'status': _server_status(server_id),
    }


def _service_ownership(server_id, identity, email=None):
    row = ServiceOwnership.query.filter_by(
        server_id=int(server_id), client_uuid=str(identity)).first()
    if row is None:
        row = ServiceOwnership.query.filter(
            ServiceOwnership.server_id == int(server_id),
            func.lower(ServiceOwnership.client_uuid) == str(identity).lower(),
        ).first()
    if row is None and email:
        row = (ServiceOwnership.query
               .filter(ServiceOwnership.server_id == int(server_id),
                       func.lower(ServiceOwnership.client_email_snapshot) == str(email).lower())
               .order_by(ServiceOwnership.id.desc()).first())
    return row


def _ownership_owner_type(ownership, server_id, email):
    reseller_id = getattr(ownership, 'reseller_id', None)
    if reseller_id is None and email:
        legacy = (ClientOwnership.query
                  .filter(ClientOwnership.server_id == int(server_id),
                          func.lower(ClientOwnership.client_email) == str(email).lower())
                  .order_by(ClientOwnership.id.desc()).first())
        reseller_id = legacy.reseller_id if legacy is not None else None
    return ('reseller' if reseller_id else 'direct'), reseller_id


def _opt_out_flags(client):
    comment = ''
    if isinstance(client, dict):
        raw = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
        comment = str(client.get('comment') or raw.get('comment') or '')
    tags = {match.group(1).lower() for match in _OPT_OUT_RE.finditer(comment)}
    return {'nosms': 'nosms' in tags, 'nopm': 'nopm' in tags}


def _expiry_datetime(expiry_ms):
    try:
        value = int(expiry_ms or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000.0, timezone.utc).replace(tzinfo=None)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _quota_projection(client, observed):
    total_bytes = 0
    used_bytes = None
    remaining_bytes = None
    live_total_known = False
    if isinstance(client, dict):
        live_total_known = client.get('totalGB') is not None
        total_bytes = _as_int(client.get('totalGB'), 0)
        used_bytes = _as_int(client.get('up'), 0) + _as_int(client.get('down'), 0)
        raw_remaining = client.get('remaining_bytes')
        try:
            raw_remaining = int(raw_remaining)
        except (TypeError, ValueError):
            raw_remaining = None
        remaining_bytes = None if raw_remaining is None or raw_remaining < 0 else raw_remaining
    if not live_total_known and observed is not None:
        # A cached row that predates the quota fields must not hide the last
        # observed values: fall back to the durable observed-state ledger. A live
        # ``totalGB == 0`` is authoritative "unlimited" and is never overridden.
        observed_total = _as_int(observed.last_total_bytes, 0)
        if observed_total > 0:
            total_bytes = observed_total
            used_bytes = None
            remaining_bytes = observed.last_remaining_bytes

    if total_bytes <= 0:
        return {'total_gb': None, 'used_gb': None, 'remaining_gb': None,
                'unlimited': True}
    if remaining_bytes is None and used_bytes is not None:
        remaining_bytes = max(0, total_bytes - used_bytes)
    if used_bytes is None and remaining_bytes is not None:
        used_bytes = max(0, total_bytes - remaining_bytes)
    if remaining_bytes is None:
        remaining_bytes = max(0, total_bytes - (used_bytes or 0))
    return {
        'total_gb': round(total_bytes / GB_BYTES, 2),
        'used_gb': round((used_bytes or 0) / GB_BYTES, 2),
        'remaining_gb': round(max(0, remaining_bytes) / GB_BYTES, 2),
        'unlimited': False,
    }


def _latest_verified_renewal(server_id, identity, email):
    query = RenewalEvent.query.filter(
        RenewalEvent.server_id == int(server_id),
        RenewalEvent.verified.is_(True),
    )
    conditions = [RenewalEvent.client_uuid == str(identity)]
    if email:
        conditions.append(func.lower(RenewalEvent.client_email_snapshot) == str(email).lower())
        conditions.append(RenewalEvent.sub_id == str(email))
    return (query.filter(or_(*conditions))
            .order_by(RenewalEvent.renewed_at.desc(), RenewalEvent.id.desc()).first())


def _latest_package(server_id, email):
    if not email:
        return None
    row = (Transaction.query
           .filter(Transaction.server_id == int(server_id),
                   Transaction.package_name.isnot(None),
                   func.lower(Transaction.client_email) == str(email).lower())
           .order_by(Transaction.created_at.desc(), Transaction.id.desc()).first())
    if row is None or not str(row.package_name or '').strip():
        return None
    catalog = (Package.query
               .filter(func.lower(Package.name) == str(row.package_name).strip().lower())
               .order_by(Package.id.asc()).first())
    return {
        'id': catalog.id if catalog is not None else None,
        'name': str(row.package_name).strip(),
    }


def resolve_service_identity(service_key):
    """Resolve a requested service key to its canonical identity and email."""
    server_id, identity = parse_service_key(service_key)
    client = live_client(server_id, identity)
    email = ''
    if isinstance(client, dict):
        email = str(client.get('email') or '').strip()
    ownership = _service_ownership(server_id, identity, email or None)
    canonical_identity = str(identity)
    if ownership is not None and str(ownership.client_uuid or '').strip():
        canonical_identity = str(ownership.client_uuid).strip()
    if not email and ownership is not None:
        email = str(ownership.client_email_snapshot or '').strip()
    canonical_key = make_service_key(server_id, canonical_identity)
    return {
        'server_id': server_id,
        'identity': canonical_identity,
        'requested_identity': identity,
        'service_key': canonical_key,
        'email': email,
        'client': client,
        'ownership': ownership,
    }


def _lifecycle_state(resolved):
    """The lifecycle row for a resolved identity, tolerating legacy key shapes.

    New rows are keyed by ``eve:<sid>:<uuid>``; an older/email-keyed row must
    still answer with its generation instead of silently reporting zero.
    """
    key = resolved['service_key']
    row = ServiceLifecycleState.query.filter_by(service_key=key).first()
    if row is None:
        row = ServiceLifecycleState.query.filter_by(
            server_id=int(resolved['server_id']),
            client_uuid=resolved['identity']).first()
    if row is None and resolved['email']:
        row = (ServiceLifecycleState.query
               .filter(ServiceLifecycleState.server_id == int(resolved['server_id']),
                       func.lower(ServiceLifecycleState.client_email)
                       == resolved['email'].lower())
               .order_by(ServiceLifecycleState.id.desc()).first())
    return row


def _observed_state(resolved):
    key = resolved['service_key']
    row = ServiceObservedState.query.filter_by(service_key=key).first()
    if row is None:
        row = ServiceObservedState.query.filter_by(
            server_id=int(resolved['server_id']),
            client_uuid=resolved['identity']).first()
    if row is None and resolved['email']:
        row = (ServiceObservedState.query
               .filter(ServiceObservedState.server_id == int(resolved['server_id']),
                       func.lower(ServiceObservedState.client_email)
                       == resolved['email'].lower())
               .order_by(ServiceObservedState.id.desc()).first())
    return row


def service_context(service_key, *, now=None):
    """The versioned, masked projection of one service."""
    resolved = resolve_service_identity(service_key)
    server_id = resolved['server_id']
    identity = resolved['identity']
    email = resolved['email']
    client = resolved['client']
    ownership = resolved['ownership']

    lifecycle = _lifecycle_state(resolved)
    observed = _observed_state(resolved)
    if client is None and ownership is None and lifecycle is None and observed is None:
        raise MachineApiNotFound('Service not found')

    status = None
    if isinstance(client, dict):
        status = client.get('service_state')
        if not status and client.get('enable') is False:
            status = 'inactive'
    if not status and observed is not None:
        status = observed.last_state
    status = str(status or 'unknown')

    expiry_ms = None
    if isinstance(client, dict):
        expiry_ms = client.get('expiryTimestamp')
        if expiry_ms is None and observed is not None:
            expiry_ms = observed.last_expiry_ms
    elif observed is not None:
        expiry_ms = observed.last_expiry_ms
    expires_at = _expiry_datetime(expiry_ms)
    moment = now or utcnow()
    days_left = None
    if expires_at is not None:
        days_left = round(max(0.0, (expires_at - moment).total_seconds() / 86400.0), 1)

    renewal = _latest_verified_renewal(server_id, identity, email)
    owner_type, reseller_id = _ownership_owner_type(ownership, server_id, email)
    generation = int(lifecycle.generation or 0) if lifecycle is not None else 0

    return envelope(
        SCHEMA_SERVICE,
        service_key=resolved['service_key'],
        customer_id=ownership.customer_id if ownership is not None else None,
        client_email=mask_client_email(email),
        server=_server_projection(server_id),
        status=status,
        expires_at=iso_utc(expires_at),
        days_left=days_left,
        quota=_quota_projection(client, observed),
        lifecycle={'generation': generation, 'state': status},
        flags=_opt_out_flags(client),
        last_verified_renewal_at=iso_utc(renewal.renewed_at) if renewal is not None else None,
        package=_latest_package(server_id, email),
        owner_type=owner_type,
        reseller_id=reseller_id,
    )


def service_list_for_customer(customer_id, *, now=None):
    customer = db.session.get(CustomerAccount, int(customer_id))
    if customer is None:
        raise MachineApiNotFound('Customer not found')
    rows = (ServiceOwnership.query
            .filter(ServiceOwnership.customer_id == int(customer_id),
                    ServiceOwnership.revoked_at.is_(None))
            .order_by(ServiceOwnership.id.asc()).all())
    services = []
    for row in rows:
        service_key = make_service_key(row.server_id, row.client_uuid)
        try:
            services.append(service_context(service_key, now=now))
        except MachineApiNotFound:
            continue
    return envelope(
        SCHEMA_SERVICE_LIST,
        customer_id=int(customer_id),
        services=services,
        count=len(services),
    )


def lookup_services_by_email(client_email):
    email = str(client_email or '').strip()
    if not email:
        raise MachineApiValidationError('client_email is required',
                                        code='client_email_required')
    if len(email) > 255:
        raise MachineApiValidationError('client_email is too long',
                                        code='invalid_client_email')

    found: dict[str, dict] = {}

    def add(server_id, client_uuid, masked_email=None, customer_id=None):
        if not client_uuid:
            return
        key = make_service_key(server_id, client_uuid)
        found.setdefault(key, {
            'service_key': key,
            'server_id': int(server_id),
            'customer_id': customer_id,
            'client_email': masked_email,
        })

    for row in (ServiceOwnership.query
                .filter(func.lower(ServiceOwnership.client_email_snapshot) == email.lower())
                .order_by(ServiceOwnership.id.asc()).all()):
        add(row.server_id, row.client_uuid,
            mask_client_email(row.client_email_snapshot), row.customer_id)

    for sid, client in _snapshot_clients():
        if str(client.get('email') or '').strip().lower() != email.lower():
            continue
        uuid = resolve_client_uuid(client)
        ownership = _service_ownership(sid, uuid, email) if uuid else None
        add(sid, uuid, mask_client_email(email),
            ownership.customer_id if ownership is not None else None)

    for row in (ServiceLifecycleState.query
                .filter(func.lower(ServiceLifecycleState.client_email) == email.lower())
                .order_by(ServiceLifecycleState.id.asc()).all()):
        ownership = _service_ownership(row.server_id, row.client_uuid or '', email)
        add(row.server_id, row.client_uuid, mask_client_email(row.client_email),
            ownership.customer_id if ownership is not None else None)

    services = [found[key] for key in sorted(found)]
    return envelope(
        SCHEMA_SERVICE_LIST,
        client_email=mask_client_email(email),
        services=services,
        count=len(services),
    )


# ---------------------------------------------------------------------------
# Customer / renewals / payments / support / claims / knowledge / SMS
# ---------------------------------------------------------------------------
def customer_context(customer_id):
    customer = db.session.get(CustomerAccount, int(customer_id))
    if customer is None:
        raise MachineApiNotFound('Customer not found')
    return envelope(
        SCHEMA_CUSTOMER,
        id=customer.id,
        customer_id=customer.id,
        display_name=mask_person_name(customer.display_name),
        phone=mask_phone(customer.primary_phone),
        phone_verified=bool(customer.phone_verified_at),
        status=customer.status or 'unknown',
        risk_level=customer.risk_level or 'standard',
        preferred_language=customer.preferred_language or 'fa',
        credit=int(customer.credit or 0),
        created_at=iso_utc(customer.created_at),
        updated_at=iso_utc(customer.updated_at),
    )


def customer_lookup_projection(match):
    payload = customer_context(match['customer'].id)
    payload['schema_version'] = SCHEMA_CUSTOMER_LIST
    payload['match_basis'] = match['match_basis']
    return payload


def renewals_context(service_key, limit=DEFAULT_PAGE_LIMIT, *, now=None):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_PAGE_LIMIT
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    resolved = resolve_service_identity(service_key)
    server_id = resolved['server_id']
    identity = resolved['identity']
    email = resolved['email']

    query = RenewalEvent.query.filter(
        RenewalEvent.server_id == int(server_id),
        RenewalEvent.verified.is_(True),
    )
    conditions = [RenewalEvent.client_uuid == str(identity),
                  RenewalEvent.sub_id == str(identity)]
    if email:
        conditions.append(func.lower(RenewalEvent.client_email_snapshot) == email.lower())
        conditions.append(RenewalEvent.sub_id == email)
    rows = (query.filter(or_(*conditions))
            .order_by(RenewalEvent.renewed_at.desc(), RenewalEvent.id.desc())
            .limit(limit).all())
    if not rows and not _service_exists(resolved):
        raise MachineApiNotFound('Service not found')
    renewals = [{
        'id': row.id,
        'event_type': row.event_type,
        'source': row.source,
        'renewed_at': iso_utc(row.renewed_at),
        'days': row.days,
        'volume_bytes': row.volume_bytes,
        'granted_volume_bytes': row.granted_volume_bytes,
        'operation_id': row.operation_id,
        'verified': bool(row.verified),
        'verified_at': iso_utc(row.verified_at),
    } for row in rows]
    return envelope(
        SCHEMA_RENEWALS,
        service_key=resolved['service_key'],
        renewals=renewals,
        count=len(renewals),
    )


def _customer_service_emails(customer_id):
    emails = set()
    rows = ServiceOwnership.query.filter_by(customer_id=int(customer_id)).all()
    for row in rows:
        email = str(row.client_email_snapshot or '').strip().lower()
        if email:
            emails.add(email)
        client = live_client(row.server_id, row.client_uuid)
        if isinstance(client, dict):
            email = str(client.get('email') or '').strip().lower()
            if email:
                emails.add(email)
    return emails


def payments_context(customer_id, since=None, limit=DEFAULT_PAGE_LIMIT):
    customer = db.session.get(CustomerAccount, int(customer_id))
    if customer is None:
        raise MachineApiNotFound('Customer not found')

    since_dt = None
    if since not in (None, ''):
        since_dt = parse_since(since)

    emails = _customer_service_emails(customer_id)
    if not emails:
        return envelope(SCHEMA_PAYMENTS, customer_id=int(customer_id),
                        payments=[], count=0)
    query = Payment.query.filter(func.lower(Payment.client_email).in_(emails))
    if since_dt is not None:
        query = query.filter(Payment.payment_date >= since_dt)
    rows = (query.order_by(Payment.payment_date.desc(), Payment.id.desc())
            .limit(max(1, min(int(limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT))).all())

    payments = []
    for row in rows:
        card = row.card
        payments.append({
            'id': row.id,
            'amount': int(row.amount or 0),
            'payment_date': iso_utc(row.payment_date),
            'verified': bool(row.verified),
            'client_email': mask_client_email(row.client_email),
            'sender_name': mask_person_name(row.sender_name),
            # Masked only: the stored value is decrypted by the ORM, then masked
            # before it can ever reach the response. No token or full PAN.
            'sender_card': mask_card_number(row.sender_card),
            'destination_card': ({
                'id': card.id,
                'label': card.label,
                'bank_name': card.bank_name,
                'masked_card': card.masked_card(),
            } if card is not None else None),
        })
    return envelope(
        SCHEMA_PAYMENTS,
        customer_id=int(customer_id),
        since=iso_utc(since_dt),
        payments=payments,
        count=len(payments),
    )


def support_requests_context(customer_id, limit=DEFAULT_PAGE_LIMIT):
    customer = db.session.get(CustomerAccount, int(customer_id))
    if customer is None:
        raise MachineApiNotFound('Customer not found')
    rows = (TelegramServiceRequest.query
            .filter_by(customer_id=int(customer_id))
            .order_by(TelegramServiceRequest.created_at.desc(),
                      TelegramServiceRequest.id.desc())
            .limit(max(1, min(int(limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT))).all())
    requests = []
    for row in rows:
        ownership = row.ownership
        requests.append({
            'id': row.id,
            'request_type': row.request_type,
            'status': row.status,
            'channel': 'telegram',
            'priority': row.support_priority or 'normal',
            'amount': row.amount,
            'package': row.package.name if row.package is not None else None,
            'service_key': (make_service_key(ownership.server_id, ownership.client_uuid)
                            if ownership is not None else None),
            'created_at': iso_utc(row.created_at),
            'updated_at': iso_utc(row.updated_at),
        })
    return envelope(
        SCHEMA_SUPPORT,
        customer_id=int(customer_id),
        requests=requests,
        count=len(requests),
    )


def ownership_claims_context(status='pending', limit=MAX_LIST_ROWS):
    requested = str(status or 'pending').strip().lower()
    if requested != 'pending':
        raise MachineApiValidationError(
            'Only status=pending is supported', code='invalid_status')
    rows = (OwnershipClaim.query
            .filter_by(status='pending')
            .order_by(OwnershipClaim.created_at.asc(), OwnershipClaim.id.asc())
            .limit(max(1, min(int(limit or MAX_LIST_ROWS), MAX_LIST_ROWS))).all())
    claims = []
    for row in rows:
        items = []
        for item in row.items:
            items.append({
                'id': item.id,
                'service_key': make_service_key(item.server_id, item.client_uuid),
                'client_email': mask_client_email(item.client_email_snapshot),
                'match_reason': item.match_reason,
                'status': item.status,
                'match_score': int(item.match_score or 0),
                'created_at': iso_utc(item.created_at),
            })
        claims.append({
            'id': row.id,
            'customer_id': row.customer_id,
            'status': row.status,
            'claim_method': row.claim_method,
            'requested_reseller_id': row.requested_reseller_id,
            'verified_phone': mask_phone(row.verified_phone),
            'items': items,
            'created_at': iso_utc(row.created_at),
            'updated_at': iso_utc(row.updated_at),
        })
    return envelope(
        SCHEMA_CLAIMS,
        status='pending',
        claims=claims,
        count=len(claims),
    )


def knowledge_context():
    faqs = (FAQ.query.filter(FAQ.is_enabled.is_(True))
            .order_by(FAQ.id.asc()).limit(MAX_LIST_ROWS).all())
    renew_templates = (RenewTemplate.query.filter(RenewTemplate.is_active.is_(True))
                       .order_by(RenewTemplate.id.asc()).limit(MAX_LIST_ROWS).all())
    # A notification template owned by a reseller is not public knowledge; only
    # global (owner_id is NULL) active rows are exposed here.
    notification_templates = (
        NotificationTemplate.query
        .filter(NotificationTemplate.is_active.is_(True),
                NotificationTemplate.owner_id.is_(None))
        .order_by(NotificationTemplate.id.asc()).limit(MAX_LIST_ROWS).all())
    online_chat_scripts = (OnlineChatScript.query
                           .filter(OnlineChatScript.is_active.is_(True))
                           .order_by(OnlineChatScript.id.asc()).limit(MAX_LIST_ROWS).all())
    return envelope(
        SCHEMA_KNOWLEDGE,
        faqs=[{
            'id': row.id, 'title': row.title, 'content': row.content,
            'platform': row.platform or 'android',
            'image_url': row.image_url, 'video_url': row.video_url,
        } for row in faqs],
        renew_templates=[{
            'id': row.id, 'name': row.name, 'content': row.content,
        } for row in renew_templates],
        notification_templates=[{
            'id': row.id, 'name': row.name, 'content': row.content,
            'type': row.type,
        } for row in notification_templates],
        online_chat_scripts=[{
            'id': row.id, 'name': row.name, 'script_code': row.script_code,
        } for row in online_chat_scripts],
    )


def _service_exists(resolved):
    return (resolved['client'] is not None or resolved['ownership'] is not None
            or _lifecycle_state(resolved) is not None
            or _observed_state(resolved) is not None)


def sms_notifications_context(service_key, limit=DEFAULT_PAGE_LIMIT):
    resolved = resolve_service_identity(service_key)
    bound = max(1, min(int(limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT))
    key = resolved['service_key']
    # Include the requested (possibly legacy/email-shaped) key so rows written
    # before the canonical UUID key existed are still visible.
    keys = {key}
    requested_key = make_service_key(resolved['server_id'], resolved['requested_identity'])
    keys.add(requested_key)

    invalidation_rows = (ServiceNotificationOutbox.query
                         .filter(ServiceNotificationOutbox.service_key.in_(keys))
                         .order_by(ServiceNotificationOutbox.created_at.desc(),
                                   ServiceNotificationOutbox.id.desc())
                         .limit(bound).all())
    notification_rows = (ServiceNotificationEvent.query
                         .filter(ServiceNotificationEvent.service_key.in_(keys))
                         .order_by(ServiceNotificationEvent.created_at.desc(),
                                   ServiceNotificationEvent.id.desc())
                         .limit(bound).all())
    send_rows = (SmsSendLog.query
                 .filter(SmsSendLog.service_key.in_(keys))
                 .order_by(SmsSendLog.created_at.desc(), SmsSendLog.id.desc())
                 .limit(bound).all())
    if not (invalidation_rows or notification_rows or send_rows) and not _service_exists(resolved):
        raise MachineApiNotFound('Service not found')

    outbox = [{
        'event_id': row.event_id,
        'status': row.status,
        'reason': row.reason,
        'generation': int(row.generation or 0),
        'attempt_count': int(row.attempt_count or 0),
        'created_at': iso_utc(row.created_at),
    } for row in invalidation_rows]
    notifications = [{
        'event_id': row.event_id,
        'status': row.status,
        'state': row.state,
        'notification_kind': row.notification_kind,
        'generation': int(row.lifecycle_generation or 0),
        'attempt_count': int(row.attempt_count or 0),
        'created_at': iso_utc(row.created_at),
        'sent_at': iso_utc(row.sent_at),
    } for row in notification_rows]
    sends = [{
        'id': row.id,
        'email': mask_client_email(row.email),
        'status': row.status,
        'state': row.state,
        'recipient': mask_phone(row.recipient),
        'requested_to': mask_phone(row.requested_to),
        'sent_to': mask_phone(row.sent_to),
        'gateway_state': row.gateway_state,
        'gateway_outcome': row.gateway_outcome,
        'carrier_state': row.carrier_state or 'unavailable',
        'lifecycle_generation': row.lifecycle_generation,
        'created_at': iso_utc(row.created_at),
        'updated_at': iso_utc(row.updated_at),
    } for row in send_rows]
    return envelope(
        SCHEMA_SMS,
        service_key=key,
        outbox=outbox,
        notifications=notifications,
        sends=sends,
        count=len(outbox) + len(notifications) + len(sends),
    )


# ---------------------------------------------------------------------------
# Query-parameter parsing
# ---------------------------------------------------------------------------
def parse_since(value) -> datetime:
    """Parse an ISO-8601 date/datetime query parameter into naive UTC."""
    text = str(value or '').strip()
    if not text:
        raise MachineApiValidationError('since must be an ISO-8601 timestamp',
                                        code='invalid_since')
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (TypeError, ValueError) as exc:
        raise MachineApiValidationError('since must be an ISO-8601 timestamp',
                                        code='invalid_since') from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_limit(value, default=DEFAULT_PAGE_LIMIT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, MAX_PAGE_LIMIT))
