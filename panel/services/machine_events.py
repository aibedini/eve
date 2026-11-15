"""Signed event push from EVE to GOD AI (Context API v1, contract section 5).

The wire format mirrors the existing GMweb ingestion pattern in
``panel/routes/sms_gateway_events.py``:

* ``POST {GOD_AI_BASE_URL}/api/v1/integrations/eve/events``
* headers ``X-Eve-Delivery-Id``, ``X-Eve-Timestamp``, ``X-Eve-Signature``
* ``X-Eve-Signature = "sha256=" + HMAC-SHA256(secret, timestamp.deliveryId.body)``
  where ``body`` is the exact UTF-8 JSON bytes sent (compact, sorted keys).

The event envelope follows ``docs/contracts/events-catalog.md``: ``event_id``,
``type``, ``schema_version``, ``occurred_at``, ``producer``, ``tenant_scope``,
``subject_refs`` and ``payload``. Events carry references and masked facts only —
raw phone numbers, message text and card numbers are rejected by
:func:`build_event`.

This module is a service API, not an automation hook: nothing in the existing
renewal/SMS/support flows calls it yet (that wiring is a later feature). Tests
inject a fake ``post`` so no network is needed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

EVENT_SCHEMA_VERSION = 1
EVENT_PRODUCER = 'eve'
EVENT_TENANT_SCOPE = 'internal-only'
EVENT_SECRET_ENV = 'GOD_AI_EVENTS_SECRET'
#: Accepted aliases, so an operator does not have to rename an existing secret.
EVENT_SECRET_ENV_ALIASES = (
    'GOD_AI_EVENTS_SECRET', 'EVE_GOD_AI_EVENTS_SECRET', 'EVE_EVENTS_SECRET')
BASE_URL_ENV = 'GOD_AI_BASE_URL'
EVENT_PATH = '/api/v1/integrations/eve/events'
MIN_EVENT_SECRET_LENGTH = 32
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 64 * 1024

#: Event type -> payload keys that the contract requires.
EVE_EVENT_PAYLOAD_FIELDS = {
    'eve.service.lifecycle_changed.v1': ('service_key', 'generation', 'state'),
    'eve.renewal.verified.v1': ('service_key', 'operation_id', 'days', 'gb'),
    'eve.ownership.changed.v1': ('service_key', 'customer_id', 'relation', 'action'),
    'eve.ownership_claim.created.v1': ('claim_id', 'evidence_kind'),
    'eve.customer.opt_out_changed.v1': ('customer_id', 'channel', 'value'),
    'eve.support_request.created.v1': ('request_id', 'customer_id', 'channel'),
    'eve.payment.recorded.v1': (
        'payment_id', 'customer_id', 'amount', 'sender_card_hash'),
}

#: Payload keys that would leak raw PII or message text across the boundary.
FORBIDDEN_PAYLOAD_FIELDS = frozenset({
    'phone', 'mobile', 'phone_number', 'raw_phone',
    'card', 'card_number', 'sender_card', 'pan',
    'text', 'message', 'body', 'content',
})

_EVENT_ID_RE_MAX = 196
_EVENT_ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,196}$')


def encode_event_body(event) -> bytes:
    """The exact bytes that are signed and POSTed."""
    body = json.dumps(event, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')
    if len(body) > MAX_BODY_BYTES:
        raise ValueError('event body exceeds 64KB')
    return body


def _normalized_occurred_at(value) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    text = str(value).strip()
    if not text:
        raise ValueError('occurred_at must not be empty')
    return text


def build_event(event_type, payload, *, event_id=None, occurred_at=None,
                subject_refs=None, generation=None, trace_id=None,
                correlation_id=None) -> dict:
    """Build one versioned ``eve.*`` envelope, validating the contract fields.

    Raises ``ValueError`` for an unknown type, a missing required payload field,
    or an attempt to put raw PII/message text in the external envelope.
    """
    event_type = str(event_type or '').strip()
    required = EVE_EVENT_PAYLOAD_FIELDS.get(event_type)
    if required is None:
        raise ValueError('unknown_event_type:%s' % (event_type or 'missing'))
    if not isinstance(payload, dict):
        raise ValueError('payload_must_be_an_object')

    forbidden = FORBIDDEN_PAYLOAD_FIELDS.intersection(payload)
    if forbidden:
        raise ValueError('payload_contains_forbidden_field:%s'
                         % ','.join(sorted(forbidden)))
    missing = [field for field in required
               if field not in payload or payload[field] is None]
    if missing:
        raise ValueError('payload_missing_field:%s' % missing[0])

    delivery_id = str(event_id or ('evt_' + uuid.uuid4().hex))
    if not delivery_id or not _EVENT_ID_RE.fullmatch(delivery_id):
        raise ValueError('event_id must be 1-%d URL/header-safe characters'
                         % _EVENT_ID_RE_MAX)

    refs = dict(subject_refs) if isinstance(subject_refs, dict) else {}
    event = {
        'event_id': delivery_id,
        'type': event_type,
        'schema_version': EVENT_SCHEMA_VERSION,
        'occurred_at': _normalized_occurred_at(occurred_at),
        'producer': EVENT_PRODUCER,
        'tenant_scope': EVENT_TENANT_SCOPE,
        'subject_refs': refs,
        'payload': dict(payload),
    }
    if generation is not None:
        event['generation'] = int(generation)
    if trace_id:
        event['trace_id'] = str(trace_id)
    if correlation_id:
        event['correlation_id'] = str(correlation_id)
    # Fail here, before any HTTP call, if a payload is not JSON-serializable.
    encode_event_body(event)
    return event


def sign_event_body(secret, timestamp, delivery_id, body) -> str:
    """The documented header value: ``sha256=HMAC(secret, ts.deliveryId.body)``."""
    raw_secret = str(secret or '')
    if not raw_secret:
        raise ValueError('event secret is required')
    body_bytes = body.encode('utf-8') if isinstance(body, str) else bytes(body)
    signed = (str(timestamp).encode('ascii') + b'.'
              + str(delivery_id).encode('ascii') + b'.' + body_bytes)
    digest = hmac.new(raw_secret.encode('utf-8'), signed, hashlib.sha256).hexdigest()
    return 'sha256=' + digest


def _configured_secret():
    for name in EVENT_SECRET_ENV_ALIASES:
        value = (os.environ.get(name) or '').strip()
        if value:
            return value
    return ''


def _event_config(base_url=None, secret=None):
    resolved_base = (base_url if base_url is not None
                     else os.environ.get(BASE_URL_ENV) or '').strip().rstrip('/')
    resolved_secret = (secret if secret is not None else _configured_secret())
    if not resolved_base:
        return None, None, 'events_not_configured'
    if not resolved_base.startswith(('http://', 'https://')):
        return None, None, 'invalid_base_url'
    if not resolved_secret:
        return None, None, 'events_not_configured'
    if len(resolved_secret) < MIN_EVENT_SECRET_LENGTH:
        logger.warning('%s is shorter than the recommended %d characters',
                       EVENT_SECRET_ENV, MIN_EVENT_SECRET_LENGTH)
    return resolved_base, resolved_secret, None


def push_event(event, *, base_url=None, secret=None, timeout=DEFAULT_TIMEOUT_SECONDS,
               post=None, now=None):
    """POST one built event to GOD AI and return a transport result.

    Returns ``{'ok': bool, 'event_id': ..., 'delivery_id': ..., 'status_code':
    int|None, 'error': str|None}``. Configuration problems are reported, not
    raised, so a caller can keep its own retry/outbox policy.
    """
    if not isinstance(event, dict) or not event.get('event_id') or not event.get('type'):
        return {'ok': False, 'event_id': None, 'delivery_id': None,
                'status_code': None, 'error': 'invalid_event'}
    delivery_id = str(event['event_id'])
    base, resolved_secret, config_error = _event_config(base_url, secret)
    if config_error:
        return {'ok': False, 'event_id': delivery_id, 'delivery_id': delivery_id,
                'status_code': None, 'error': config_error}

    try:
        body = encode_event_body(event)
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'event_id': delivery_id, 'delivery_id': delivery_id,
                'status_code': None, 'error': str(exc)}

    timestamp = int(now if now is not None else time.time())
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'eve-machine-events/1',
        'X-Eve-Delivery-Id': delivery_id,
        'X-Eve-Timestamp': str(timestamp),
        'X-Eve-Signature': sign_event_body(resolved_secret, timestamp, delivery_id, body),
    }
    sender = post or requests.post
    try:
        response = sender(base + EVENT_PATH, data=body, headers=headers,
                          timeout=timeout)
    except Exception as exc:  # requests.RequestException and injected transports
        logger.warning('GOD AI event %s delivery failed: %s', delivery_id, exc)
        return {'ok': False, 'event_id': delivery_id, 'delivery_id': delivery_id,
                'status_code': None, 'error': 'transport_error'}

    status_code = getattr(response, 'status_code', None)
    ok = isinstance(status_code, int) and 200 <= status_code < 300
    result = {
        'ok': ok,
        'event_id': delivery_id,
        'delivery_id': delivery_id,
        'status_code': status_code,
        'error': None if ok else 'http_%s' % status_code,
    }
    if not ok:
        logger.warning('GOD AI event %s rejected with HTTP %s', delivery_id, status_code)
    return result


def push_event_type(event_type, payload, *, base_url=None, secret=None,
                    timeout=DEFAULT_TIMEOUT_SECONDS, post=None, now=None, **fields):
    """Convenience: build an envelope from a type/payload and push it."""
    event = build_event(event_type, payload, **fields)
    return push_event(event, base_url=base_url, secret=secret, timeout=timeout,
                      post=post, now=now)
