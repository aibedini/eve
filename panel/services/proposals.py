"""GOD AI proposals: submission, listing and the operator decision (XR-GA-019).

The machine API (``POST/GET /machine/v1/proposals``) writes and reads here; the
operator review page in ``panel/routes/proposals.py`` decides here. The decision
is the whole product: accepting a proposal records that EVE agreed with GOD AI's
suggestion. It deliberately does **not** send an SMS, renew a service or touch a
panel client -- execution is a separate, EVE-owned action.

A proposal that describes a service carries the ``generation`` it was computed
from. :func:`accept_proposal` revalidates that number against the current
``service_lifecycle_states`` row: if the service was renewed (or the row is gone)
after the proposal was written, the proposal is marked ``expired`` instead of
accepted, so a stale suggestion can never be applied.
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import (
    PROPOSAL_EXPIRED_GENERATION_STALE,
    PROPOSAL_EXPIRED_SERVICE_MISSING,
    PROPOSAL_STATUS_ACCEPTED,
    PROPOSAL_STATUS_EXPIRED,
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_REJECTED,
    PROPOSAL_STATUSES,
    PROPOSAL_TYPES,
    CustomerAccount,
    Proposal,
    ServiceLifecycleState,
)
from panel.services import audit as audit_service
from panel.services.machine_api import iso_utc, mask_person_name, utcnow

logger = logging.getLogger(__name__)

#: Request-size ceiling from the contract: the proposal payload may not exceed it.
MAX_PAYLOAD_BYTES = 8 * 1024
MAX_PROPOSAL_ID_LENGTH = 128
MAX_REJECT_REASON_LENGTH = 500
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
#: Longest payload preview rendered in the operator queue.
PAYLOAD_PREVIEW_LENGTH = 280

_PROPOSAL_ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,%d}$' % MAX_PROPOSAL_ID_LENGTH)
# Same shape as the machine API's canonical identity (``eve:<serverId>:<id>``).
_SERVICE_KEY_RE = re.compile(r'^eve:[0-9]{1,10}:.{1,200}$')

#: Payload fields the review queue shows verbatim before falling back to JSON.
_PREVIEW_TEXT_KEYS = (
    'text', 'message', 'body', 'content', 'draft', 'reply_text', 'reminder_text',
)


class ProposalError(Exception):
    """An HTTP-safe failure raised by this service.

    Mirrors :class:`panel.services.machine_api.MachineApiError` so the machine
    blueprint can translate both into the same contract-shaped JSON error.
    """

    code = 'invalid_request'
    status = 400

    def __init__(self, message=None, *, code=None, status=None, **extra):
        self.code = code or self.code
        self.status = status or self.status
        self.message = message or self.code
        self.extra = extra
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Payload / field validation
# ---------------------------------------------------------------------------
def normalize_payload(payload) -> str:
    """Validate and canonicalise the proposal payload as compact JSON text.

    A proposal payload is a JSON object (the contract's ``payload: {}``), never
    a raw string: it is the machine-facing structure the operator reviews.
    """
    if not isinstance(payload, dict):
        raise ProposalError('payload must be a JSON object', code='invalid_payload')
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ProposalError('payload must be JSON-serializable', code='invalid_payload') from exc
    size = len(encoded.encode('utf-8'))
    if size > MAX_PAYLOAD_BYTES:
        raise ProposalError(
            'payload exceeds the %d byte limit' % MAX_PAYLOAD_BYTES,
            code='payload_too_large', size=size, limit=MAX_PAYLOAD_BYTES)
    return encoded


def decode_payload(raw):
    """Return the stored payload as JSON, or ``None`` when it is unreadable."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _validated_proposal_id(value) -> str:
    proposal_id = str(value or '').strip()
    if not proposal_id:
        raise ProposalError('proposal_id is required', code='proposal_id_required')
    if not _PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise ProposalError(
            'proposal_id must be 1-%d URL-safe characters' % MAX_PROPOSAL_ID_LENGTH,
            code='invalid_proposal_id')
    return proposal_id


def _validated_type(value) -> str:
    proposal_type = str(value or '').strip().lower()
    if proposal_type not in PROPOSAL_TYPES:
        raise ProposalError(
            'type must be one of: %s' % ', '.join(PROPOSAL_TYPES),
            code='invalid_type', allowed=list(PROPOSAL_TYPES))
    return proposal_type


def _optional_int(value, field, *, minimum=0):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ProposalError('%s must be an integer' % field, code='invalid_%s' % field)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProposalError('%s must be an integer' % field,
                            code='invalid_%s' % field) from exc
    if parsed < minimum:
        raise ProposalError('%s must be >= %d' % (field, minimum),
                            code='invalid_%s' % field)
    return parsed


def _optional_service_key(value):
    if value is None or value == '':
        return None
    service_key = str(value).strip()
    if len(service_key) > 255 or not _SERVICE_KEY_RE.fullmatch(service_key):
        raise ProposalError('service_key must be eve:<server_id>:<client_uuid>',
                            code='invalid_service_key')
    return service_key


# ---------------------------------------------------------------------------
# Submission (machine API) -- idempotent on proposal_id
# ---------------------------------------------------------------------------
def _replay(existing, proposal_type, encoded_payload):
    """The same proposal_id may only ever describe the same proposal."""
    if existing.type != proposal_type or (existing.payload or '') != encoded_payload:
        raise ProposalError(
            'proposal_id already exists with a different type or payload',
            code='proposal_conflict', status=409)
    return existing, False


def create_proposal(proposal_id, proposal_type, payload, *, customer_id=None,
                    service_key=None, generation=None, created_by_key_id=None):
    """Store one pending proposal, or return the existing row on an idempotent retry.

    Returns ``(row, created)``. ``created`` is ``False`` when the identical
    proposal already existed (the machine endpoint answers 200, not 201).
    """
    pid = _validated_proposal_id(proposal_id)
    ptype = _validated_type(proposal_type)
    encoded = normalize_payload(payload)
    customer = _optional_int(customer_id, 'customer_id')
    service = _optional_service_key(service_key)
    generation_value = _optional_int(generation, 'generation')

    existing = Proposal.query.filter_by(proposal_id=pid).first()
    if existing is not None:
        return _replay(existing, ptype, encoded)

    row = Proposal(
        proposal_id=pid,
        type=ptype,
        customer_id=customer,
        service_key=service,
        generation=generation_value,
        payload=encoded,
        status=PROPOSAL_STATUS_PENDING,
        created_by_key_id=_optional_int(created_by_key_id, 'created_by_key_id'),
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent retry won the unique index: answer from its row.
        db.session.rollback()
        existing = Proposal.query.filter_by(proposal_id=pid).first()
        if existing is None:
            raise
        return _replay(existing, ptype, encoded)
    return row, True


# ---------------------------------------------------------------------------
# Listing / projections
# ---------------------------------------------------------------------------
def _bounded_limit(value, default=DEFAULT_LIST_LIMIT):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, MAX_LIST_LIMIT))


def list_proposals(status=PROPOSAL_STATUS_PENDING, limit=DEFAULT_LIST_LIMIT):
    """Return proposals with ``status``, newest first (bounded)."""
    requested = str(status or PROPOSAL_STATUS_PENDING).strip().lower()
    if requested not in PROPOSAL_STATUSES:
        raise ProposalError(
            'status must be one of: %s' % ', '.join(PROPOSAL_STATUSES),
            code='invalid_status', allowed=list(PROPOSAL_STATUSES))
    return (Proposal.query
            .filter_by(status=requested)
            .order_by(Proposal.created_at.desc(), Proposal.id.desc())
            .limit(_bounded_limit(limit)).all())


def machine_projection(row) -> dict:
    """Masked, stable projection of one proposal for the machine API.

    ``decided_by`` is an EVE operator identity, so it is masked like every other
    person field on this surface. Internal ids and key material are never part of
    the projection.
    """
    return {
        'proposal_id': row.proposal_id,
        'type': row.type,
        'status': row.status,
        'customer_id': row.customer_id,
        'service_key': row.service_key,
        'generation': int(row.generation) if row.generation is not None else None,
        'payload': decode_payload(row.payload),
        'created_at': iso_utc(row.created_at),
        'decided_at': iso_utc(row.decided_at),
        'decided_by': mask_person_name(row.decided_by),
        'reason': row.reason,
    }


def pending_proposals(limit=MAX_LIST_LIMIT):
    """The operator review queue: pending rows only, newest first."""
    return list_proposals(PROPOSAL_STATUS_PENDING, limit)


def customer_map(rows) -> dict:
    """Customers referenced by ``rows`` keyed by id (one query for the page)."""
    ids = sorted({int(row.customer_id) for row in rows if row.customer_id})
    if not ids:
        return {}
    return {customer.id: customer
            for customer in CustomerAccount.query.filter(
                CustomerAccount.id.in_(ids)).all()}


def payload_preview(row, limit=PAYLOAD_PREVIEW_LENGTH) -> str:
    """One-line, HTML-safe preview of a stored payload for the review page."""
    data = decode_payload(getattr(row, 'payload', None))
    text = ''
    if isinstance(data, dict):
        for key in _PREVIEW_TEXT_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        else:
            text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    elif data is not None:
        text = json.dumps(data, ensure_ascii=False)
    text = ' '.join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + '...'
    return text


# ---------------------------------------------------------------------------
# Operator decision
# ---------------------------------------------------------------------------
def generation_conflict(proposal):
    """Why an accept cannot be applied, or ``None`` when the generation is current.

    A proposal that is not bound to a service has nothing to revalidate. A bound
    proposal must match the current durable generation exactly; a missing row or
    a different generation means the world moved on since the proposal was made.
    """
    if not proposal.service_key:
        return None
    row = ServiceLifecycleState.query.filter_by(
        service_key=proposal.service_key).first()
    if row is None:
        return PROPOSAL_EXPIRED_SERVICE_MISSING
    if proposal.generation is None:
        return PROPOSAL_EXPIRED_GENERATION_STALE
    if int(proposal.generation) != int(row.generation or 0):
        return PROPOSAL_EXPIRED_GENERATION_STALE
    return None


def _actor_label(actor) -> str:
    username = str(getattr(actor, 'username', '') or '').strip()
    return (username or 'unknown')[:100]


def _claim(proposal, *, status, reason, decided_by, moment) -> bool:
    """Guard the pending -> decided transition atomically.

    A second reviewer (or a double submit) must not overwrite the first decision;
    the guarded UPDATE is what makes that impossible across processes.
    """
    result = db.session.execute(
        update(Proposal)
        .where(Proposal.id == proposal.id,
               Proposal.status == PROPOSAL_STATUS_PENDING)
        .values(status=status, reason=reason, decided_by=decided_by,
                decided_at=moment))
    if result.rowcount != 1:
        db.session.rollback()
        return False
    return True


def accept_proposal(proposal, *, actor=None, now=None):
    """Record an accept, or expire the proposal when its generation is stale.

    Returns ``{'status': ..., 'conflict': reason|None, 'proposal': row}``. The
    only writes are the proposal decision and the audit row: no SMS, no client
    mutation, no lifecycle bump.
    """
    if proposal is None:
        raise ProposalError('Proposal not found', code='not_found', status=404)
    conflict = generation_conflict(proposal)
    target_status = PROPOSAL_STATUS_EXPIRED if conflict else PROPOSAL_STATUS_ACCEPTED
    if not _claim(proposal, status=target_status, reason=conflict,
                  decided_by=_actor_label(actor), moment=now or utcnow()):
        raise ProposalError('This proposal has already been decided',
                            code='already_decided', status=409)
    audit_service.record(
        'proposal.expire' if conflict else 'proposal.accept',
        ('Proposal', proposal.proposal_id), actor=actor,
        meta={'type': proposal.type, 'service_key': proposal.service_key,
              'generation': (int(proposal.generation)
                             if proposal.generation is not None else None),
              'conflict': conflict})
    db.session.commit()
    return {'status': target_status, 'conflict': conflict, 'proposal': proposal}


def reject_proposal(proposal, reason, *, actor=None, now=None):
    """Record a rejection. The reason is required and is shown to the proposer."""
    if proposal is None:
        raise ProposalError('Proposal not found', code='not_found', status=404)
    clean_reason = ' '.join(str(reason or '').split())
    if not clean_reason:
        raise ProposalError('A rejection reason is required',
                            code='reason_required')
    if len(clean_reason) > MAX_REJECT_REASON_LENGTH:
        raise ProposalError(
            'The rejection reason must be at most %d characters'
            % MAX_REJECT_REASON_LENGTH, code='reason_too_long')
    if not _claim(proposal, status=PROPOSAL_STATUS_REJECTED, reason=clean_reason,
                  decided_by=_actor_label(actor), moment=now or utcnow()):
        raise ProposalError('This proposal has already been decided',
                            code='already_decided', status=409)
    audit_service.record(
        'proposal.reject', ('Proposal', proposal.proposal_id), actor=actor,
        meta={'type': proposal.type, 'reason': clean_reason})
    db.session.commit()
    return {'status': PROPOSAL_STATUS_REJECTED, 'conflict': None,
            'proposal': proposal}
