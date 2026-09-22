"""Finalize a renewal's BUSINESS facts exactly once, independently of activation.

The production failure this exists for: the renewal route returned as soon as the
panel read-back showed the client inactive, *before* the code that creates the
transaction, records the renewal event, builds the customer text and completes the
durable operation. So an account whose quota and expiry had genuinely been applied
was left in EVE with no financial record, no renewal fact, no message for the
customer and an operation stuck open - while the panel showed the renewal applied.
An operator could not tell whether to charge, to message, or to renew again.

The rule now: **config applied is a business fact**. Once the panel holds the
intended expiry/quota, the business side is finalized once - whether or not the
customer is active yet - and activation is a separate, resumable process.

``finalize_renewal_business_once`` is the one place that decides. It is callable
from the original request, from Re-check and from the background activation
reconciler, and it is idempotent through the operation row itself (not through a
cache): the second and third callers get the stored payload back and build nothing.
"""
from __future__ import annotations

from datetime import datetime

from panel.extensions import db
from panel.services import client_operations
from panel.services.client_operations import (  # re-exported vocabulary
    STATE_ACTIVATION_PENDING,
    STATE_COMPLETED,
    STATE_NEEDS_RECONCILIATION,
)

#: Keys of the durable business payload that may be returned to a client. Everything
#: here is either an id, a machine state, or text the customer is meant to see.
BUSINESS_PAYLOAD_KEYS = (
    'business_finalized', 'business_finalized_at', 'final_state', 'config_applied',
    'activation_config_converged', 'runtime_sync_state', 'transaction_id',
    'renewal_event_id', 'copy_text', 'tpl_vars', 'client_comment', 'was_reactivated',
    'expected', 'operation_id', 'price', 'is_free',
    # Set when the reconciler had to finish the record the request never wrote: the
    # operator must be able to see that the exact templated text was not available.
    'rebuilt_after_restart', 'transaction_error', 'renewal_event_error',
)


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def stored_business(operation) -> dict | None:
    """The durable business payload, or None when this operation never built one."""
    response = client_operations.operation_response(operation)
    if response.get('business_finalized') is True:
        return response
    return None


def business_is_finalized(operation) -> bool:
    return stored_business(operation) is not None


def finalize_renewal_business_once(operation, build, *, final_state=None,
                                   config_applied=True, activation_converged=False,
                                   runtime_sync_state=None):
    """Run ``build`` once and persist its result durably. Returns ``(payload, already)``.

    ``build`` is a zero-argument callable that performs the business work (records
    the transaction and the renewal event, builds the customer text) and returns a
    dict to persist. It is called at most once per operation, ever.

    When ``operation`` is None the build still runs - a caller without a durable
    operation (an older client, a direct API call) must not lose its renewal - but
    nothing can be deduplicated for it, and that is reported honestly.
    """
    if operation is None:
        payload = dict(build() or {})
        payload.setdefault('business_finalized', True)
        payload.setdefault('business_finalized_at', _now_iso())
        payload['durable'] = False
        return payload, False

    existing = stored_business(operation)
    if existing is not None:
        # A second caller (Re-check, the reconciler, a retried request) reuses the
        # recorded fact instead of creating a second transaction or event.
        return existing, True

    # Re-read the row so two concurrent finalizers cannot both build: the winner is
    # whoever sees the flag first, and the loser re-reads it after its build attempt.
    db.session.refresh(operation) if operation in db.session else None
    existing = stored_business(operation)
    if existing is not None:
        return existing, True

    payload = dict(build() or {})
    payload['business_finalized'] = True
    payload['business_finalized_at'] = _now_iso()
    payload['durable'] = True
    expected = client_operations.operation_expected(operation)
    if expected:
        payload.setdefault('expected', expected)
    payload['final_state'] = final_state
    payload['config_applied'] = bool(config_applied)
    payload['activation_config_converged'] = bool(activation_converged)
    if runtime_sync_state:
        payload['runtime_sync_state'] = runtime_sync_state
    payload['operation_id'] = operation.idempotency_key

    operation.response_json = client_operations._dumps(
        {key: value for key, value in payload.items() if key in BUSINESS_PAYLOAD_KEYS
         or key in ('durable',)})
    operation.error = None
    operation.updated_at = datetime.utcnow()
    if final_state == 'APPLIED_ACTIVE':
        operation.state = STATE_COMPLETED
        operation.completed_at = operation.completed_at or datetime.utcnow()
    else:
        operation.state = STATE_ACTIVATION_PENDING
    db.session.commit()
    return payload, False


def mark_activation_converged(operation, *, payload=None):
    """Move a pending operation to completed once activation has converged."""
    if operation is None:
        return None
    stored = stored_business(operation) or {}
    if payload:
        stored.update({key: value for key, value in payload.items()
                       if key in BUSINESS_PAYLOAD_KEYS})
    stored['business_finalized'] = True
    stored['activation_config_converged'] = True
    stored['final_state'] = 'APPLIED_ACTIVE'
    stored['runtime_sync_state'] = (payload or {}).get('runtime_sync_state',
                                                       stored.get('runtime_sync_state'))
    stored.setdefault('business_finalized_at', _now_iso())
    stored['operation_id'] = operation.idempotency_key
    operation.response_json = client_operations._dumps(stored)
    operation.state = STATE_COMPLETED
    operation.error = None
    operation.completed_at = operation.completed_at or datetime.utcnow()
    operation.updated_at = datetime.utcnow()
    db.session.commit()
    return stored


def record_business_result(operation, payload, *, final_state=None,
                           config_applied=True, activation_converged=False,
                           runtime_sync_state=None):
    """Persist the business payload the request just built. Idempotent on the flag.

    Returns ``(payload, already)``: ``already`` is True when a payload was already on
    the row, in which case the STORED one wins and the freshly built one is discarded
    (so a retry can never overwrite the recorded facts with a second transaction id).
    """
    if operation is None:
        payload = dict(payload or {})
        payload.setdefault('business_finalized', True)
        payload['durable'] = False
        return payload, False
    existing = stored_business(operation)
    if existing is not None:
        return existing, True
    payload = dict(payload or {})
    payload['business_finalized'] = True
    payload.setdefault('business_finalized_at', _now_iso())
    payload['durable'] = True
    payload['final_state'] = final_state
    payload['config_applied'] = bool(config_applied)
    payload['activation_config_converged'] = bool(activation_converged)
    if runtime_sync_state:
        payload['runtime_sync_state'] = runtime_sync_state
    payload['operation_id'] = operation.idempotency_key
    expected = client_operations.operation_expected(operation)
    if expected:
        payload.setdefault('expected', expected)
    operation.response_json = client_operations._dumps(
        {key: value for key, value in payload.items()
         if key in BUSINESS_PAYLOAD_KEYS or key == 'durable'})
    operation.error = None
    operation.updated_at = datetime.utcnow()
    if final_state == 'APPLIED_ACTIVE':
        operation.state = STATE_COMPLETED
        operation.completed_at = operation.completed_at or datetime.utcnow()
    elif final_state:
        operation.state = STATE_ACTIVATION_PENDING
    db.session.commit()
    return payload, False


def minimal_rebuild(operation, *, record_renewal_event=None, record_transaction=None):
    """Rebuild the durable business facts when the request died before doing so.

    Used by the activation reconciler: an operation whose panel write landed but
    whose request vanished must not stay in limbo. Only what can be reconstructed
    from the stored non-secret context is recorded, and the payload is flagged so an
    operator knows the exact templated message was not available.
    """
    if operation is None:
        return {}, False
    existing = stored_business(operation)
    if existing is not None:
        return existing, True
    context = client_operations.operation_context(operation)
    expected = client_operations.operation_expected(operation)
    payload = {
        'rebuilt_after_restart': True,
        'expected': expected,
        'copy_text': None,
        'tpl_vars': {},
        'client_comment': None,
    }
    try:
        if record_transaction is not None:
            tx = record_transaction(context, expected)
            payload['transaction_id'] = getattr(tx, 'id', None)
    except Exception:
        payload['transaction_error'] = 'transaction rebuild failed'
    try:
        if record_renewal_event is not None:
            event = record_renewal_event(context, expected)
            payload['renewal_event_id'] = (event or {}).get('id') if isinstance(event, dict) \
                else getattr(event, 'id', None)
    except Exception:
        payload['renewal_event_error'] = 'renewal event rebuild failed'
    return record_business_result(
        operation, payload,
        final_state=(stored_business(operation) or {}).get('final_state'),
        config_applied=True, activation_converged=False)


def public_business_view(operation) -> dict:
    """The durable renewal facts a Re-check may return to an operator."""
    stored = stored_business(operation)
    if stored is None:
        return {}
    return {key: stored.get(key) for key in BUSINESS_PAYLOAD_KEYS if key in stored}
