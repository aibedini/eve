"""Keep activation moving after the renewal request has already answered.

Inline convergence (three fast attempts inside the request) is a fast path, not a
guarantee: a 3x-ui node can take longer than any request should wait, and a
``nodePending`` write is by definition still synchronising. The renewal must
therefore be resumable AFTER the browser has been answered - without renewing
again, without charging again, and without a customer message that is not true yet.

This worker is that resumption, and it is deliberately narrow. For every operation
left in ``activation_pending`` it may:

* re-read the panel (client record, memberships, traffic) - authoritative reads;
* perform a capability-correct enable repair, at most once per tick;
* wait for ``nodePending`` to clear on a later tick;
* finish the business record if the original request died before it did;
* mark the operation completed once activation has converged.

It must never: add days, add volume, reset traffic, charge, create a second
renewal event, or send a renewal-success message. Those belong to the request that
already did them, and its result is durable on the operation row.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from panel.extensions import db
from panel.services import client_operations, renew_activation, renew_finalization
from panel.services.client_operations import (
    STATE_ACTIVATION_PENDING,
    STATE_COMPLETED,
    STATE_NEEDS_RECONCILIATION,
    STATE_PANEL_APPLIED,
)

logger = logging.getLogger(__name__)

#: How often the reconciler looks for pending operations.
INTERVAL_SECONDS = 30
#: An operation older than this is not retried: the account has almost certainly been
#: renewed again since, and a repair on a stale intent is worse than none.
MAX_AGE_HOURS = 48
#: Operations handled per tick (the tick is cheap: one panel read per operation).
BATCH = 10
#: Milliseconds of "settling" before the first reconciliation attempt, so the inline
#: attempts inside the request get their chance first.
MIN_AGE_SECONDS = 10


def pending_operations(*, limit: int = BATCH, now=None) -> list:
    """Operations whose panel write landed but whose activation has not converged."""
    from panel.models import ClientOperation  # deferred: model import at call time
    now = now or datetime.utcnow()
    newest = now - timedelta(seconds=MIN_AGE_SECONDS)
    oldest = now - timedelta(hours=MAX_AGE_HOURS)
    try:
        return (ClientOperation.query
                .filter(ClientOperation.action == 'renew')
                .filter(ClientOperation.state.in_((STATE_ACTIVATION_PENDING,
                                                   STATE_PANEL_APPLIED)))
                .filter(ClientOperation.updated_at <= newest)
                .filter(ClientOperation.updated_at >= oldest)
                .order_by(ClientOperation.updated_at.asc())
                .limit(limit).all())
    except Exception:
        return []


def reconcile_operation(operation, *, read_layers=None, repair=None,
                        finalize=None, limits=None) -> dict:
    """One activation-only reconciliation attempt for one renewal operation.

    Everything that touches the panel is injected, so the state machine is testable
    without a panel, a network or a sleep. Returns a small, non-secret summary.
    """
    result = {'operation_id': getattr(operation, 'idempotency_key', None),
              'action': None, 'final_state': None}
    if operation is None:
        result['action'] = 'no_operation'
        return result
    limits = limits or {}
    expected = client_operations.operation_expected(operation)
    if not expected:
        # Without a durable expectation there is nothing to converge towards; the
        # operation is closed as needing a human look rather than repaired blind.
        operation.state = STATE_NEEDS_RECONCILIATION
        operation.error = 'activation_pending without a recorded expectation'
        operation.updated_at = datetime.utcnow()
        db.session.commit()
        result['action'] = 'needs_reconciliation'
        return result

    read_layers = read_layers or _default_read_layers(operation)
    if read_layers is None:
        result['action'] = 'unreadable'
        return result

    # The request may have died before recording the business facts. Rebuild what the
    # stored context allows - exactly once - BEFORE any completion branch, because a
    # converged activation must not close an operation whose transaction/event/text
    # were never written.
    if not renew_finalization.business_is_finalized(operation):
        payload, already = renew_finalization.minimal_rebuild(
            operation,
            record_renewal_event=(finalize or {}).get('record_renewal_event'),
            record_transaction=(finalize or {}).get('record_transaction'))
        result['business_rebuilt'] = not already

    layers = read_layers()
    result['final_state'] = layers.final_state

    if layers.final_state == renew_activation.STATE_APPLIED_ACTIVE:
        renew_finalization.mark_activation_converged(
            operation, payload={'runtime_sync_state': layers.runtime_sync_state})
        result['action'] = 'completed'
        return result

    if not layers.config_applied:
        # The panel no longer holds the renewed config (a later change, a rollback, a
        # different renewal). Do not fight it: hand it to a human.
        operation.state = STATE_NEEDS_RECONCILIATION
        operation.error = 'activation_pending but the config is no longer applied'
        operation.updated_at = datetime.utcnow()
        db.session.commit()
        result['action'] = 'needs_reconciliation'
        return result

    if layers.node_pending is True:
        # The panel itself says its node is still synchronising: writing again would
        # not help and could restart the wait.
        result['action'] = 'waiting_for_node'
        operation.updated_at = datetime.utcnow()
        db.session.commit()
        return result

    depleted, reason = renew_activation.account_is_still_depleted(
        layers, {'expiryTime': expected.get('expiryTime'),
                 'totalGB': expected.get('totalGB'),
                 'now_ms': int(time.time() * 1000)})
    if depleted:
        result['action'] = 'still_depleted'
        result['reason'] = reason
        return result

    if repair is None:
        repair = _default_repair(operation, expected)
    if repair is None:
        result['action'] = 'unreadable'
        return result
    try:
        repair_result = repair()
    except Exception as exc:                                   # pragma: no cover
        result['action'] = 'repair_raised'
        result['error'] = str(exc)[:200]
        operation.updated_at = datetime.utcnow()
        db.session.commit()
        return result
    result['action'] = 'repaired'
    result['repair'] = renew_activation._summarise_mutation(repair_result)
    operation.updated_at = datetime.utcnow()
    db.session.commit()
    return result


def _default_read_layers(operation):
    """Build the real layer reader for this operation, or None when it cannot."""
    from panel.models import Server  # deferred
    from panel.adapters import xui as xui_adapter
    from panel.services import panel_capabilities

    server = db.session.get(Server, operation.server_id) if operation.server_id else None
    if server is None:
        return None
    session_obj, error = xui_adapter.get_xui_session(server)
    if error or session_obj is None:
        return None
    caps, _reason = panel_capabilities.capabilities_for(server, session_obj)
    email = (operation.client_email or '')

    def _read():
        details = xui_adapter.v3_get_client_details(server, session_obj, email) \
            if caps.first_class else {'ok': False}
        inbounds, _err, _dt = xui_adapter.fetch_inbounds(
            session_obj, server.host, server.panel_type, force_fresh=True)
        memberships = renew_activation.membership_map(
            inbounds or [], email, inbound_ids=(details.get('inbound_ids') or None))
        traffic = (xui_adapter.v3_client_traffic(server, session_obj, email)
                   if caps.client_traffic else None)
        return renew_activation.analyze_activation(
            expected=client_operations.operation_expected(operation),
            global_client=(details.get('client') if details.get('ok') else None),
            inbound_ids=(details.get('inbound_ids') or []),
            memberships=memberships, traffic=traffic)
    return _read


def _default_repair(operation, expected):
    """A capability-correct, activation-only enable for this operation."""
    from panel.models import Server  # deferred
    from panel.adapters import xui as xui_adapter
    from panel.services import panel_capabilities

    server = db.session.get(Server, operation.server_id) if operation.server_id else None
    if server is None:
        return None
    session_obj, error = xui_adapter.get_xui_session(server)
    if error or session_obj is None:
        return None
    caps, _reason = panel_capabilities.capabilities_for(server, session_obj)
    email = (operation.client_email or '')

    def _repair():
        # Activation only: the client object is re-read so nothing else is zeroed by
        # the full-replacement update, and no amount, expiry or traffic value is sent.
        ok, snapshot = xui_adapter.read_authoritative_client_settings(
            server, session_obj, email)
        client = dict(snapshot) if ok and isinstance(snapshot, dict) else {'email': email}
        client['enable'] = True
        result_ok, response, err = xui_adapter.v3_enable_client(
            server, session_obj, email, client, capabilities=caps,
            preserved_client=(snapshot if ok else None))
        return xui_adapter.classify_mutation_result(result_ok, response, err,
                                                    may_be_partial=not result_ok)
    return _repair


def run_reconciliation_once(*, limit: int = BATCH) -> dict:
    """One pass over the pending operations. Returns counters (for the doctor)."""
    counters = {'checked': 0, 'completed': 0, 'repaired': 0, 'waiting': 0,
                'needs_reconciliation': 0, 'errors': 0}
    for operation in pending_operations(limit=limit):
        counters['checked'] += 1
        try:
            result = reconcile_operation(operation)
        except Exception:
            counters['errors'] += 1
            db.session.rollback()
            logger.exception('[renew-activation] reconciliation failed for %s',
                             getattr(operation, 'idempotency_key', None))
            continue
        action = result.get('action')
        if action == 'completed':
            counters['completed'] += 1
        elif action == 'repaired':
            counters['repaired'] += 1
        elif action in ('waiting_for_node', 'still_depleted'):
            counters['waiting'] += 1
        elif action == 'needs_reconciliation':
            counters['needs_reconciliation'] += 1
        elif action == 'unreadable':
            counters['errors'] += 1
    return counters


def renew_activation_worker(interval_seconds: int = INTERVAL_SECONDS,
                            stop_event=None) -> None:
    """Singleton background worker: activation-only reconciliation."""
    from app import app  # deferred: app-level helper (avoids an import cycle)
    stop = stop_event or threading.Event()
    while not stop.is_set():
        try:
            with app.app_context():
                counters = run_reconciliation_once()
                if counters.get('checked'):
                    logger.info('[renew-activation] %s', counters)
        except Exception:
            logger.exception('[renew-activation] tick failed')
        stop.wait(max(5, int(interval_seconds or INTERVAL_SECONDS)))
