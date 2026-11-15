"""Machine API (EVE Context API v1) mounted at ``/machine/v1``.

This is the only non-session data surface in EVE: GOD AI consumes versioned,
masked projections over a bearer machine key. Every read is ``GET``; the one
write is ``POST /machine/v1/proposals`` (XR-GA-019), which stores a *pending*
proposal and nothing else -- it never sends an SMS or mutates a customer or
service. The only other database write on the blueprint is
``machine_api_keys.last_used_at`` (see
:func:`panel.services.machine_api.touch_machine_key`).

Auth (contract section 2)::

    Authorization: Bearer eve_mk_<id>.<secret>
    X-Request-ID: <opaque id>          # echoed on every response

Negative contract: no/invalid/expired/revoked key -> 401, a key without the
required scope -> 403, ``customers:lookup`` without ``phone_hash`` -> 400.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from functools import wraps

from flask import Blueprint, g, jsonify, request
from sqlalchemy import text

from panel.extensions import db, limiter
from panel.services import machine_api
from panel.services import proposals as proposals_service

logger = logging.getLogger(__name__)

bp = Blueprint('machine_api', __name__)

_REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
_DISABLED_VALUES = frozenset({'0', 'false', 'off', 'no', 'disabled'})


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------
def machine_api_enabled() -> bool:
    """``EVE_MACHINE_API_ENABLED`` is the documented rollback switch (ADR-GA-009)."""
    raw = os.environ.get('EVE_MACHINE_API_ENABLED')
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED_VALUES


def _new_request_id() -> str:
    supplied = (request.headers.get('X-Request-ID') or '').strip()
    if supplied and _REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


def _request_id() -> str:
    return getattr(g, 'machine_request_id', None) or _new_request_id()


def _ok(payload, status=200):
    payload = dict(payload or {})
    payload['request_id'] = _request_id()
    response = jsonify(payload)
    response.status_code = status
    return response


def _error(message, status, code, **extra):
    payload = {
        'error': code,
        'message': message or code,
        'source': 'eve',
        'request_id': _request_id(),
    }
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = status
    return response


@bp.before_request
def _machine_before_request():
    g.machine_request_id = _new_request_id()
    if not machine_api_enabled():
        return _error('Machine API is disabled', 404, 'not_found')


@bp.after_request
def _machine_after_request(response):
    response.headers['X-Request-ID'] = _request_id()
    # Responses contain customer context: never cache them at any layer.
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Vary'] = 'Authorization'
    return response


# ---------------------------------------------------------------------------
# Auth + bounds
# ---------------------------------------------------------------------------
def _presented_token():
    header = request.headers.get('Authorization') or ''
    scheme, separator, value = header.partition(' ')
    if not separator or scheme.strip().lower() != 'bearer':
        return None
    token = value.strip()
    return token or None


def _rate_key():
    """Per-key rate limit without storing or logging the secret."""
    token = _presented_token()
    if token:
        return 'machine-key:' + hashlib.sha256(token.encode('utf-8')).hexdigest()[:32]
    return 'machine-ip:' + (request.remote_addr or 'unknown')


machine_rate_limit = limiter.limit('300 per minute', key_func=_rate_key)


def machine_scope_required(scope):
    """Authenticate the bearer key and enforce one contract scope."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            token = _presented_token()
            if token is None:
                logger.warning('machine API request without bearer token (request_id=%s)',
                               _request_id())
                return _error('A bearer machine key is required', 401, 'unauthorized')
            try:
                key = machine_api.authenticate_machine_token(token, scope)
            except machine_api.MachineApiError as exc:
                logger.warning('machine API auth rejected: %s (request_id=%s)',
                               exc.code, _request_id())
                return _error(exc.message, exc.status, exc.code, **exc.extra)
            g.machine_api_key = key
            machine_api.touch_machine_key(key)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def _guard(view):
    """Translate service exceptions into contract-shaped JSON errors."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except (machine_api.MachineApiError, proposals_service.ProposalError) as exc:
            return _error(exc.message, exc.status, exc.code, **exc.extra)
        except Exception:
            logger.exception('machine API request failed (request_id=%s)', _request_id())
            return _error('Internal error', 500, 'internal_error')
    return wrapper


# ---------------------------------------------------------------------------
# Health (no auth, contract table)
# ---------------------------------------------------------------------------
@bp.route('/machine/v1/health', methods=['GET'])
@limiter.limit('120 per minute', key_func=_rate_key)
@_guard
def machine_health():
    try:
        db.session.execute(text('SELECT 1'))
    except Exception:
        logger.exception('machine API health check could not reach the database')
        return _error('Database is unavailable', 503, 'degraded')
    return _ok(machine_api.envelope(
        machine_api.SCHEMA_HEALTH, status='ok', database='ok',
        machine_api_enabled=True,
    ))


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
@bp.route('/machine/v1/customers/<int:customer_id>', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_CUSTOMER_READ)
@_guard
def machine_customer(customer_id):
    return _ok(machine_api.customer_context(customer_id))


@bp.route('/machine/v1/customers:lookup', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_CUSTOMER_READ)
@_guard
def machine_customer_lookup():
    phone_hash = request.args.get('phone_hash')
    if not phone_hash and request.args.get('phone'):
        # Explicit: raw phone numbers are never accepted on this endpoint.
        return _error('Send phone_hash, never a raw phone number',
                      400, 'phone_hash_required')
    matches = machine_api.lookup_customers_by_phone_hash(phone_hash)
    customers = [machine_api.customer_lookup_projection(match) for match in matches]
    payload = machine_api.envelope(
        machine_api.SCHEMA_CUSTOMER_LIST,
        query={'match': 'phone_hash'},
        customers=customers,
        count=len(customers),
    )
    return _ok(payload)


@bp.route('/machine/v1/customers/<int:customer_id>/services', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_SERVICE_READ)
@_guard
def machine_customer_services(customer_id):
    return _ok(machine_api.service_list_for_customer(customer_id))


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
@bp.route('/machine/v1/services:lookup', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_SERVICE_READ)
@_guard
def machine_service_lookup():
    return _ok(machine_api.lookup_services_by_email(request.args.get('client_email')))


@bp.route('/machine/v1/services/<path:service_key>/renewals', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_RENEWAL_READ)
@_guard
def machine_service_renewals(service_key):
    return _ok(machine_api.renewals_context(
        service_key, limit=machine_api.parse_limit(request.args.get('limit'))))


@bp.route('/machine/v1/services/<path:service_key>', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_SERVICE_READ)
@_guard
def machine_service(service_key):
    return _ok(machine_api.service_context(service_key))


# ---------------------------------------------------------------------------
# Payments / support / identity review / knowledge
# ---------------------------------------------------------------------------
@bp.route('/machine/v1/customers/<int:customer_id>/payments', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_PAYMENT_READ_MASKED)
@_guard
def machine_customer_payments(customer_id):
    return _ok(machine_api.payments_context(
        customer_id, since=request.args.get('since'),
        limit=machine_api.parse_limit(request.args.get('limit'))))


@bp.route('/machine/v1/customers/<int:customer_id>/support-requests', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_SUPPORT_READ)
@_guard
def machine_customer_support_requests(customer_id):
    return _ok(machine_api.support_requests_context(
        customer_id, limit=machine_api.parse_limit(request.args.get('limit'))))


@bp.route('/machine/v1/ownership-claims', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_CUSTOMER_READ)
@_guard
def machine_ownership_claims():
    return _ok(machine_api.ownership_claims_context(
        status=request.args.get('status', 'pending')))


@bp.route('/machine/v1/knowledge/faqs', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_KNOWLEDGE_READ)
@_guard
def machine_knowledge_faqs():
    return _ok(machine_api.knowledge_context())


@bp.route('/machine/v1/sms/notifications/<path:service_key>', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_SERVICE_READ)
@_guard
def machine_sms_notifications(service_key):
    return _ok(machine_api.sms_notifications_context(
        service_key, limit=machine_api.parse_limit(request.args.get('limit'))))


# ---------------------------------------------------------------------------
# Proposals (XR-GA-019, contract section 6)
#
# GOD AI submits a *proposal*; EVE's operator accepts or rejects it in the panel.
# This POST therefore only stores a pending row: it has no side effect on a
# customer, a service or the SMS outbox.
# ---------------------------------------------------------------------------
#: Room for the JSON envelope around the payload when rejecting a huge body
#: before it is parsed (the service still enforces the exact 8 KB payload limit).
PROPOSAL_REQUEST_OVERHEAD_BYTES = 4 * 1024


@bp.route('/machine/v1/proposals', methods=['POST'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_PROPOSALS_WRITE)
@_guard
def machine_create_proposal():
    declared_length = request.content_length
    if (declared_length is not None
            and declared_length > proposals_service.MAX_PAYLOAD_BYTES
            + PROPOSAL_REQUEST_OVERHEAD_BYTES):
        return _error(
            'payload exceeds the %d byte limit' % proposals_service.MAX_PAYLOAD_BYTES,
            400, 'payload_too_large')
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error('A JSON object body is required', 400, 'invalid_request')
    row, created = proposals_service.create_proposal(
        body.get('proposal_id'),
        body.get('type'),
        body.get('payload'),
        customer_id=body.get('customer_id'),
        service_key=body.get('service_key'),
        generation=body.get('generation'),
        created_by_key_id=getattr(getattr(g, 'machine_api_key', None), 'id', None),
    )
    # 201 on a new proposal, 200 on an idempotent retry of the same proposal_id.
    return _ok({'proposal_id': row.proposal_id, 'status': row.status},
               201 if created else 200)


@bp.route('/machine/v1/proposals', methods=['GET'])
@machine_rate_limit
@machine_scope_required(machine_api.SCOPE_PROPOSALS_READ)
@_guard
def machine_proposals():
    requested_status = (request.args.get('status') or 'pending').strip().lower()
    rows = proposals_service.list_proposals(
        status=requested_status, limit=request.args.get('limit'))
    return _ok(machine_api.envelope(
        machine_api.SCHEMA_PROPOSALS,
        status=requested_status,
        proposals=[proposals_service.machine_projection(row) for row in rows],
        count=len(rows),
    ))
