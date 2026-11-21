"""Durable client-mutation idempotency and reseller credit reservations."""
import hashlib
import json
from datetime import datetime

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import Admin, ClientOperation, ResellerContract, Transaction, WalletLedger

# ── operation states ─────────────────────────────────────────────────────────
# A renewal is not one boolean. These states separate "the panel has the config"
# from "the customer is active" from "EVE has finished its own bookkeeping", which
# is what an operator needs to know before touching the account again.
STATE_RESERVED = 'reserved'
STATE_PREPARED = 'prepared'
STATE_PANEL_APPLIED = 'panel_applied'
STATE_ACTIVATION_PENDING = 'activation_pending'
STATE_COMPLETED = 'completed'
STATE_NEEDS_RECONCILIATION = 'needs_reconciliation'
STATE_FAILED = 'failed'

#: States whose business fact is already recorded; a second attempt must never
#: create a second transaction or renewal event.
FINAL_BUSINESS_STATES = (STATE_COMPLETED, STATE_ACTIVATION_PENDING)

#: Fields of the renewal intent that may be persisted in ``expected_json``. Only
#: non-secret values: an amount, a date, a package label - never a credential, a
#: subscription id or a recipient phone number.
SAFE_EXPECTED_CONTEXT_KEYS = (
    'server_id', 'inbound_id', 'client_email', 'mode', 'days', 'volume_gb',
    'reset_traffic', 'start_after_first_use', 'is_free', 'price', 'description',
    'package_name', 'source', 'renewal_trace_id', 'before_evidence', 'mutation_evidence',
)


def _canonical_hash(payload) -> str:
    encoded = json.dumps(payload or {}, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _dumps(payload) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, default=str)


def begin_client_operation(*, idempotency_key, action, admin, server_id=None,
                           inbound_id=None, client_email=None, amount=0, payload=None,
                           quote=None):
    """Create/reserve once; return ``(operation, disposition, data)``.

    Disposition is ``new``, ``replay``, ``in_progress``, ``failed``,
    ``conflict``, or ``insufficient_credit``.
    """
    key = str(idempotency_key or '').strip()
    if not key:
        return None, 'conflict', {'error': 'Idempotency key is required'}
    if len(key) > 160:
        return None, 'conflict', {'error': 'Idempotency key is too long'}
    request_hash = _canonical_hash(payload)
    existing = ClientOperation.query.filter_by(idempotency_key=key).first()
    if existing:
        if existing.admin_id != admin.id or existing.action != action \
                or existing.request_hash != request_hash:
            return existing, 'conflict', {'error': 'Idempotency key was reused for another request'}
        if existing.state == 'completed':
            try:
                response = json.loads(existing.response_json or '{}')
            except Exception:
                response = {}
            return existing, 'replay', response
        if existing.state == 'failed':
            try:
                response = json.loads(existing.response_json or '{}')
            except Exception:
                response = {}
            return existing, 'failed', response or {'error': existing.error or 'Operation failed'}
        return existing, 'in_progress', {'error': 'Operation is already in progress'}

    operation_amount = max(0, int(amount or 0))
    payer_id = quote.get('reseller_id') if quote is not None else (admin.id if admin.role == 'reseller' else None)
    reserve_amount = operation_amount if payer_id else 0
    quota_mib = quote.get('quota_mib', 0) if quote else 0
    operation = ClientOperation(
        idempotency_key=key,
        request_hash=request_hash,
        action=action,
        admin_id=admin.id,
        server_id=server_id,
        inbound_id=inbound_id,
        client_email=client_email,
        amount=operation_amount,
        credit_reserved=False,
        billing_admin_id=payer_id,
        quota_mib=quota_mib,
        pricing_quote=quote,
        state='reserved',
    )
    db.session.add(operation)
    try:
        db.session.flush()
        if reserve_amount:
            minimum_balance = case(
                (Admin.allow_negative_credit.is_(True),
                 -func.abs(func.coalesce(Admin.negative_credit_limit, 0))),
                else_=0,
            )
            updated = Admin.query.filter(
                Admin.id == payer_id, Admin.role == 'reseller', Admin.enabled.is_(True),
                (func.coalesce(Admin.credit, 0) - reserve_amount) >= minimum_balance,
            ).update(
                {Admin.credit: func.coalesce(Admin.credit, 0) - reserve_amount},
                synchronize_session=False,
            )
            if updated != 1:
                db.session.rollback()
                return None, 'insufficient_credit', {'error': 'Insufficient credit'}
            operation.credit_reserved = True
        if quota_mib:
            updated = ResellerContract.query.filter(
                ResellerContract.reseller_id == payer_id,
                ResellerContract.quota_mib >= quota_mib,
            ).update({ResellerContract.quota_mib: ResellerContract.quota_mib - quota_mib},
                     synchronize_session=False)
            if updated != 1:
                db.session.rollback()
                return None, 'insufficient_credit', {'error': 'Insufficient reseller traffic quota'}
            _quota_entry(operation, -quota_mib)
        db.session.commit()
        db.session.refresh(admin)
        return operation, 'new', {'remaining_credit': admin.credit}
    except IntegrityError:
        db.session.rollback()
        existing = ClientOperation.query.filter_by(idempotency_key=key).first()
        if existing and existing.request_hash == request_hash and existing.admin_id == admin.id:
            return existing, 'in_progress', {'error': 'Operation is already in progress'}
        return existing, 'conflict', {'error': 'Idempotency key conflict'}


def _quota_entry(operation, amount):
    db.session.add(WalletLedger(
        account_type='reseller_quota', owner_id=operation.billing_admin_id,
        amount=amount, type='quota_refund' if amount > 0 else 'quota_reservation',
        idempotency_key=f'quota:{operation.id}:{"refund" if amount > 0 else "reserve"}',
        reference_type='client_operation', reference_id=operation.id,
    ))


def _refund_operation(operation):
    if operation.credit_reserved and operation.amount:
        Admin.query.filter_by(id=operation.billing_admin_id or operation.admin_id).update(
            {Admin.credit: func.coalesce(Admin.credit, 0) + operation.amount},
            synchronize_session=False,
        )
    operation.credit_reserved = False
    if operation.quota_mib:
        ResellerContract.query.filter_by(reseller_id=operation.billing_admin_id).update(
            {ResellerContract.quota_mib: ResellerContract.quota_mib + operation.quota_mib},
            synchronize_session=False,
        )
        _quota_entry(operation, operation.quota_mib)


def mark_client_operation_applied(operation, expected=None):
    """The panel accepted the write; the intent stays durable for later readers.

    The wrapped shape written by :func:`prepare_client_operation_expected` is
    preserved (its ``context`` is what lets a later reconciler rebuild the business
    facts after a crash), so this is a state transition, not a payload replacement.
    """
    if operation is None:
        return None
    if expected:
        wrapped = {}
        try:
            existing = json.loads(operation.expected_json or '{}')
            if isinstance(existing, dict) and isinstance(existing.get('expected'), dict):
                wrapped = existing
        except Exception:
            wrapped = {}
        wrapped['expected'] = dict(expected)
        wrapped.setdefault('prepared_at', datetime.utcnow().isoformat())
        operation.expected_json = _dumps(wrapped)
    operation.state = 'panel_applied'
    db.session.commit()
    return operation


def prepare_client_operation_expected(operation, expected, *, context=None):
    """Persist the INTENDED result durably, before the panel write.

    Redis used to be the only place the expected values lived (a 180-second cache),
    which is why a Re-check a few minutes later could answer
    ``renew_result_unavailable`` for a perfectly healthy renewal: EVE had forgotten
    what it asked the panel for. The operation row survives a dead worker, a Redis
    restart, a different gunicorn worker and any amount of time.

    The write is deliberately NOT ``mark_client_operation_applied``: nothing has been
    applied yet, and the state says so.
    """
    if operation is None:
        return None
    payload = {'expected': dict(expected or {})}
    if context:
        payload['context'] = {key: context.get(key) for key in SAFE_EXPECTED_CONTEXT_KEYS
                              if key in context}
    payload['prepared_at'] = datetime.utcnow().isoformat()
    operation.expected_json = _dumps(payload)
    if operation.state in (None, '', STATE_RESERVED):
        operation.state = STATE_PREPARED
    operation.updated_at = datetime.utcnow()
    db.session.commit()
    return operation


def operation_expected(operation) -> dict:
    """The expected state of an operation, tolerating both stored shapes.

    New rows store ``{"expected": {...}, "context": {...}}``; rows written by older
    builds store the bare field mapping. A read must never fail because of the shape.
    """
    if operation is None or not operation.expected_json:
        return {}
    try:
        data = json.loads(operation.expected_json)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    expected = data.get('expected')
    if isinstance(expected, dict):
        return expected
    return {key: value for key, value in data.items()
            if key not in ('context', 'prepared_at')}


def operation_context(operation) -> dict:
    """The non-secret renewal intent stored beside the expected values."""
    if operation is None or not operation.expected_json:
        return {}
    try:
        data = json.loads(operation.expected_json)
    except Exception:
        return {}
    context = data.get('context') if isinstance(data, dict) else None
    return context if isinstance(context, dict) else {}


def operation_response(operation) -> dict:
    """The durable business result of a completed (or pending) renewal."""
    if operation is None or not operation.response_json:
        return {}
    try:
        data = json.loads(operation.response_json)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def record_client_operation_verification(operation, payload):
    """Keep read-back evidence without replacing already finalized business facts."""
    if operation is None:
        return
    stored = operation_response(operation)
    stored.update(payload)
    operation.response_json = _dumps(stored)
    operation.updated_at = datetime.utcnow()
    db.session.commit()


def mark_client_operation_activation_pending(operation, *, expected=None, response=None):
    """The config is on the panel; activation has not converged yet.

    This is NOT a failure and NOT a completed operation: the activation reconciler
    keeps working on it, and the business fact (if it was recorded) stays recorded.
    """
    if operation is None:
        return None
    if expected:
        mark_client_operation_applied(operation, expected)
    operation.state = STATE_ACTIVATION_PENDING
    operation.error = None
    if response is not None:
        operation.response_json = _dumps(response)
    operation.updated_at = datetime.utcnow()
    db.session.commit()
    return operation


def load_client_operation(idempotency_key, *, action=None, admin_id=None):
    """Load one operation by its idempotency key (the public ``operation_id``)."""
    key = str(idempotency_key or '').strip()
    if not key:
        return None
    query = ClientOperation.query.filter_by(idempotency_key=key)
    if action:
        query = query.filter_by(action=action)
    if admin_id is not None:
        query = query.filter_by(admin_id=admin_id)
    try:
        return query.first()
    except Exception:
        return None


def complete_client_operation(operation, response, transaction=None, *, state=None):
    """Close an operation, or park it in a resumable state.

    ``state`` is what keeps "the panel has the config" from being flattened into
    "everything is done": a renewal whose activation has not converged closes as
    ``activation_pending`` (the reconciler keeps working on it) instead of
    ``completed``, while its business facts stay recorded either way.
    """
    if operation is None:
        return None
    operation.state = state or STATE_COMPLETED
    if operation.action == 'renew':
        # The notification worker may have persisted its marker after finalization.
        response = dict(operation_response(operation), **(response or {}))
    operation.response_json = json.dumps(response or {}, ensure_ascii=False, default=str)
    operation.error = None
    if operation.state == STATE_COMPLETED:
        operation.completed_at = datetime.utcnow()
    if transaction is not None:
        db.session.flush()
        operation.transaction_id = transaction.id
    from panel.services.support import report_client_operation
    report_client_operation(operation, response)
    db.session.commit()
    return operation


def fail_client_operation(operation, error, response=None, *, uncertain=False):
    if not operation:
        return
    operation_id = operation.id
    # Discard any uncommitted ownership/transaction work from the failed request,
    # then reload the durable reservation before deciding whether to refund it.
    db.session.rollback()
    operation = ClientOperation.query.filter_by(id=operation_id).with_for_update().first()
    if not operation or operation.state in ('completed', 'failed'):
        return
    if uncertain or operation.state == 'panel_applied':
        operation.state = 'needs_reconciliation'
    else:
        _refund_operation(operation)
        operation.state = 'failed'
        operation.completed_at = datetime.utcnow()
    operation.error = str(error or '')[:2000]
    result = dict(response or {'success': False, 'error': str(error or '')})
    if operation.action == 'renew':
        scheduled = operation_response(operation).get('renew_sms_sent_at')
        if scheduled:
            result['renew_sms_sent_at'] = scheduled
    operation.response_json = json.dumps(result,
                                         ensure_ascii=False, default=str)
    db.session.commit()


def install_client_operation_response_guard(operation):
    """Finalize abandoned route exits without requiring every return to clean up."""
    from flask import after_this_request

    operation_id = operation.id

    @after_this_request
    def _finalize_abandoned(response):
        current = db.session.get(ClientOperation, operation_id)
        if current and current.state not in ('completed', 'failed', 'needs_reconciliation'):
            payload = response.get_json(silent=True) if response.is_json else None
            error = (payload or {}).get('error') or f'HTTP {response.status_code}'
            fail_client_operation(
                current, error, payload,
                uncertain=current.state == 'panel_applied',
            )
        return response

    return operation


def resolve_client_operation(operation_id, resolution, resolver_id):
    """Explicitly reconcile an ambiguous panel-applied operation."""
    db.session.rollback()
    operation = ClientOperation.query.filter_by(id=int(operation_id)).with_for_update().first()
    if not operation:
        return None, 'Operation not found'
    if operation.state not in ('needs_reconciliation', 'panel_applied'):
        return None, f'Operation is already {operation.state}'
    resolution = str(resolution or '').strip().lower()
    if resolution == 'refund':
        _refund_operation(operation)
        operation.state = 'failed'
        operation.error = f'manually reconciled as not applied by admin#{resolver_id}'
        operation.completed_at = datetime.utcnow()
    elif resolution == 'complete':
        transaction = None
        payer_id = operation.billing_admin_id or operation.admin_id
        owner = db.session.get(Admin, payer_id)
        if (operation.amount > 0 or operation.quota_mib) and not operation.transaction_id:
            is_reseller = bool(operation.billing_admin_id or (owner and owner.role == 'reseller'))
            transaction = Transaction(
                admin_id=payer_id,
                server_id=operation.server_id,
                client_email=operation.client_email,
                amount=(-operation.amount if is_reseller else operation.amount),
                type=operation.action[:20],
                category=('usage' if is_reseller else 'income'),
                package_name=(operation.pricing_quote or {}).get('package_name'),
                volume_gb=(operation.pricing_quote or {}).get('volume_gb'),
                days=(operation.pricing_quote or {}).get('days'),
                description=(
                    f'Reconciled {operation.action} operation #{operation.id} '
                    f'by admin#{resolver_id}'
                )[:255],
            )
            db.session.add(transaction)
            db.session.flush()
            operation.transaction_id = transaction.id
        operation.state = 'completed'
        operation.error = None
        operation.response_json = json.dumps({
            'success': True, 'reconciled': True, 'operation_id': operation.id,
        })
        operation.completed_at = datetime.utcnow()
        from panel.services.support import report_client_operation
        report_client_operation(operation, operation_response(operation))
    else:
        return None, 'resolution must be complete or refund'
    db.session.commit()
    return operation, None
