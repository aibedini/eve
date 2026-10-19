"""Send the renewal confirmation exactly once, as soon as activation is real.

The production complaint this exists for: the confirmation SMS was fired from
inside the renewal request, immediately after a verification that ran a second
after the panel write. A v3 panel that had not finished activating yet produced a
"pending" result, the send was skipped, and nothing ever retried it - so a
customer whose renewal had in fact been applied never received the message,
purely because the operator closed the modal before the account turned green.

The rule now: the message follows the OPERATION, not the request or the modal.

* it is sent after activation has converged, from whatever path observes that
  first - the request, Re-check, Force Enable, or the background reconciler;
* a node that is still synchronising is not a failure: after two consecutive
  reads that show every enable layer active, the message is allowed through;
* it is sent at most once per operation, guarded by a durable marker on the
  operation row (``renew_sms_sent_at``), never by a time window or a cache;
* the sender is injected so tests never touch a gateway.
"""
from __future__ import annotations

import logging
from datetime import datetime

from panel.extensions import db
from panel.services import client_operations, renew_finalization

logger = logging.getLogger(__name__)

#: Reads of "config applied, every layer enabled, but the node is still syncing"
#: required before the customer is told the renewal is ready.
NODE_SYNC_CONFIRMATIONS = 2


def _stored(operation):
    return renew_finalization.stored_business(operation) or {}


def renewal_sms_was_sent(operation) -> bool:
    if operation is None:
        return False
    return bool(_stored(operation).get('renew_sms_sent_at'))


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
        send('renew', operation.server_id, operation.client_email,
             None, None, dict(tpl_vars), stored.get('client_comment') or '',
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
