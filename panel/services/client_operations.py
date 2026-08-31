"""Durable client-mutation idempotency and reseller credit reservations."""
import hashlib
import json
from datetime import datetime

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import Admin, ClientOperation, Transaction


def _canonical_hash(payload) -> str:
    encoded = json.dumps(payload or {}, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def begin_client_operation(*, idempotency_key, action, admin, server_id=None,
                           inbound_id=None, client_email=None, amount=0, payload=None):
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
    reserve_amount = operation_amount if admin.role == 'reseller' else 0
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
                Admin.id == admin.id,
                (func.coalesce(Admin.credit, 0) - reserve_amount) >= minimum_balance,
            ).update(
                {Admin.credit: func.coalesce(Admin.credit, 0) - reserve_amount},
                synchronize_session=False,
            )
            if updated != 1:
                db.session.rollback()
                return None, 'insufficient_credit', {'error': 'Insufficient credit'}
            operation.credit_reserved = True
        db.session.commit()
        db.session.refresh(admin)
        return operation, 'new', {'remaining_credit': admin.credit}
    except IntegrityError:
        db.session.rollback()
        existing = ClientOperation.query.filter_by(idempotency_key=key).first()
        if existing and existing.request_hash == request_hash and existing.admin_id == admin.id:
            return existing, 'in_progress', {'error': 'Operation is already in progress'}
        return existing, 'conflict', {'error': 'Idempotency key conflict'}


def mark_client_operation_applied(operation, expected=None):
    operation.state = 'panel_applied'
    operation.expected_json = json.dumps(expected or {}, ensure_ascii=False, default=str)
    db.session.commit()


def complete_client_operation(operation, response, transaction=None):
    operation.state = 'completed'
    operation.response_json = json.dumps(response or {}, ensure_ascii=False, default=str)
    operation.error = None
    operation.completed_at = datetime.utcnow()
    if transaction is not None:
        db.session.flush()
        operation.transaction_id = transaction.id
    db.session.commit()


def fail_client_operation(operation, error, response=None, *, uncertain=False):
    if not operation:
        return
    operation_id = operation.id
    # Discard any uncommitted ownership/transaction work from the failed request,
    # then reload the durable reservation before deciding whether to refund it.
    db.session.rollback()
    operation = db.session.get(ClientOperation, operation_id)
    if not operation or operation.state in ('completed', 'failed'):
        return
    if uncertain or operation.state == 'panel_applied':
        operation.state = 'needs_reconciliation'
    else:
        if operation.credit_reserved and operation.amount:
            Admin.query.filter(Admin.id == operation.admin_id).update(
                {Admin.credit: func.coalesce(Admin.credit, 0) + operation.amount},
                synchronize_session=False,
            )
            operation.credit_reserved = False
        operation.state = 'failed'
        operation.completed_at = datetime.utcnow()
    operation.error = str(error or '')[:2000]
    operation.response_json = json.dumps(response or {'success': False, 'error': str(error or '')},
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
    operation = db.session.get(ClientOperation, int(operation_id))
    if not operation:
        return None, 'Operation not found'
    if operation.state not in ('needs_reconciliation', 'panel_applied'):
        return None, f'Operation is already {operation.state}'
    resolution = str(resolution or '').strip().lower()
    if resolution == 'refund':
        if operation.credit_reserved and operation.amount:
            Admin.query.filter(Admin.id == operation.admin_id).update(
                {Admin.credit: func.coalesce(Admin.credit, 0) + operation.amount},
                synchronize_session=False,
            )
        operation.credit_reserved = False
        operation.state = 'failed'
        operation.error = f'manually reconciled as not applied by admin#{resolver_id}'
        operation.completed_at = datetime.utcnow()
    elif resolution == 'complete':
        transaction = None
        owner = db.session.get(Admin, operation.admin_id)
        if operation.amount > 0 and not operation.transaction_id:
            is_reseller = bool(owner and owner.role == 'reseller')
            transaction = Transaction(
                admin_id=operation.admin_id,
                server_id=operation.server_id,
                client_email=operation.client_email,
                amount=(-operation.amount if is_reseller else operation.amount),
                type=operation.action[:20],
                category=('usage' if is_reseller else 'income'),
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
    else:
        return None, 'resolution must be complete or refund'
    db.session.commit()
    return operation, None
