"""Operation-scoped renewal notification dispatch.

New requests start the gateway worker after the panel accepts its write, in
parallel with activation verification. Scheduling never finalizes the business
record or implies gateway acceptance/delivery. The durable marker and stable
gateway identity prevent a later verification/recheck from dispatching twice.
Legacy operations without an early dispatch keep their existing recovery path.
"""
from __future__ import annotations

import logging
import hashlib
from datetime import datetime

from panel.extensions import db
from panel.services import client_operations, renew_finalization

logger = logging.getLogger(__name__)

#: Reads of "config applied, every layer enabled, but the node is still syncing"
#: required before the customer is told the renewal is ready.
NODE_SYNC_CONFIRMATIONS = 2


def notification_key(operation):
    """One opaque gateway identity per operation and explicit failed-SMS retry."""
    suffix = hashlib.sha256(operation.idempotency_key.encode()).hexdigest()[:32]
    attempt = int(_stored(operation).get('renew_sms_attempt') or 0)
    return f'tx-renew-op-{suffix}-{attempt}'


def _stored(operation):
    return renew_finalization.stored_business(operation) or {}


def renewal_sms_was_sent(operation) -> bool:
    if operation is None:
        return False
    return bool(renewal_sms_scheduled_at(operation))


def renewal_sms_scheduled_at(operation):
    if operation is None:
        return None
    return client_operations.operation_response(operation).get('renew_sms_sent_at')


def start_renewal_notification(operation, tpl_vars, comment='', *, sender=None):
    """Dispatch after an accepted panel write, independently of activation reads.

    Only the scheduling marker is written: starting an SMS must never finalize
    a charge or claim that the account has been verified.
    The gateway receives the same operation identity on every recovery path.
    """
    if operation is None or renewal_sms_was_sent(operation):
        return {'sent': False, 'reason': 'already_sent' if operation else 'no_operation'}
    key = notification_key(operation)
    result = client_operations.operation_response(operation)
    result['renew_sms_sent_at'] = datetime.utcnow().isoformat()
    # Commit before starting the independent worker so other processes see it.
    operation.response_json = client_operations._dumps(result)
    db.session.commit()
    try:
        (sender or _default_sender)(
            'renew', operation.server_id, operation.client_email, None, None,
            dict(tpl_vars, _renew_notification_key=key, _renew_inbound_id=operation.inbound_id), comment,
            _server_name(operation.server_id))
    except Exception:
        result = client_operations.operation_response(operation)
        result.pop('renew_sms_sent_at', None)
        operation.response_json = client_operations._dumps(result)
        db.session.commit()
        raise
    return {'sent': True, 'reason': 'scheduled'}


def _persist(operation, updates: dict) -> None:
    """Merge ``updates`` into the durable business payload of an operation."""
    if operation is None or not updates:
        return
    stored = _stored(operation)
    stored.update(updates)
    stored['business_finalized'] = True
    stored['operation_id'] = operation.idempotency_key
    operation.response_json = client_operations._dumps(
        {key: value for key, value in stored.items()
         if key in renew_finalization.BUSINESS_PAYLOAD_KEYS or key == 'durable'})
    operation.updated_at = datetime.utcnow()
    db.session.commit()


def _default_sender(event_name, server_id, email, template_type, default_template,
                    tpl_vars, comment, server_name):
    from app import (  # deferred: app-level helper, avoids a circular import
        DEFAULT_RENEW_SMS_TEMPLATE, RENEW_SMS_TEMPLATE_TYPE, _fire_automation_sms,
    )
    _fire_automation_sms(event_name, server_id, email,
                         template_type or RENEW_SMS_TEMPLATE_TYPE,
                         default_template or DEFAULT_RENEW_SMS_TEMPLATE,
                         tpl_vars, comment, server_name=server_name)


def _server_name(server_id) -> str:
    try:
        from panel.models import Server  # deferred: model import at call time
        server = db.session.get(Server, server_id) if server_id else None
        return (getattr(server, 'name', '') or '') if server is not None else ''
    except Exception:
        return ''


def send_renewal_confirmation_once(operation, *, activation_converged,
                                   node_pending=None, sender=None) -> dict:
    """Fire the renewal confirmation for this operation, at most once, ever.

    ``activation_converged`` is the caller's measurement of the ENABLE layers
    (global client plus every membership). ``node_pending`` is the panel's own
    "my node has not synchronised" flag; while that is true the message waits for
    ``NODE_SYNC_CONFIRMATIONS`` consecutive positive reads, because the panel has
    already committed the change and only the node is behind.

    Returns a small summary: ``{'sent': bool, 'reason': str, 'confirmations': int}``.
    """
    result = {'sent': False, 'reason': None, 'confirmations': 0}
    if operation is None:
        result['reason'] = 'no_operation'
        return result
    if renewal_sms_was_sent(operation):
        result['reason'] = 'already_sent'
        return result
    stored = _stored(operation)
    if not stored:
        # Nothing durable to message from: the request never recorded the customer
        # text (a rebuild after a crash stores copy_text=None, tpl_vars={}).
        result['reason'] = 'no_business_record'
        return result
    tpl_vars = stored.get('tpl_vars') or {}
    if not tpl_vars:
        result['reason'] = 'no_text'
        return result
    if not activation_converged:
        result['reason'] = 'not_active'
        return result

    confirms = int(stored.get('node_sync_confirms') or 0)
    if node_pending:
        confirms += 1
        result['confirmations'] = confirms
        _persist(operation, {'node_sync_confirms': confirms})
        if confirms < NODE_SYNC_CONFIRMATIONS:
            result['reason'] = 'awaiting_node_sync_confirmation'
            return result

    send = sender or _default_sender
    try:
        message_vars = dict(tpl_vars, _renew_notification_key=notification_key(operation),
                            _renew_inbound_id=operation.inbound_id)
        send('renew', operation.server_id, operation.client_email,
             None, None, message_vars, stored.get('client_comment') or '',
             _server_name(operation.server_id))
    except Exception as exc:                            # pragma: no cover - defensive
        logger.exception('[renew-notify] confirmation send failed for %s',
                         getattr(operation, 'idempotency_key', None))
        result['reason'] = 'send_raised'
        result['error'] = str(exc)[:200]
        return result
    # Marked only after the send was accepted: a refused/failed send stays
    # retryable instead of being burned by a false "sent" marker.
    _persist(operation, {'renew_sms_sent_at': datetime.utcnow().isoformat()})
    result['sent'] = True
    return result
