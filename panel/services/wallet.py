"""Atomic wallet balance changes backed by an immutable ledger.

Phase 1 (financial correctness): every balance mutation is a single SQL
``SET credit = COALESCE(credit, 0) + delta`` so concurrent writers can never
lose an update (no read-modify-write). Debits add a ``credit + delta >= 0``
guard so a concurrent spend cannot drive the balance negative. Each mutation
appends a WalletLedger row; when an idempotency key is supplied the unique
index makes a duplicate (retry or race) roll the whole transaction back
instead of double-crediting.
"""
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import WalletLedger


_ACCOUNT_TABLES = {
    'admin': 'admins',
    'customer': 'customer_accounts',
}


def ledger_entry_exists(idempotency_key, session=None) -> bool:
    """Fast-path check; the unique index is the authoritative guard."""
    if not idempotency_key:
        return False
    session = session or db.session
    return session.query(WalletLedger.id).filter_by(
        idempotency_key=str(idempotency_key),
    ).first() is not None


def apply_balance_delta(account_type: str, owner_id, delta, *, entry_type: str,
                        reference_type: str | None = None, reference_id=None,
                        idempotency_key: str | None = None,
                        description: str | None = None,
                        transaction_row=None, session=None) -> tuple[bool, str | None]:
    """Atomically add ``delta`` (signed) to a wallet and append a ledger row.

    Returns ``(applied, reason)``. Never commits: the caller owns the
    transaction so a receipt approval / wallet debit is all-or-nothing. On a
    duplicate idempotency key the session is rolled back and
    ``(False, 'duplicate')`` is returned, so no partial balance change
    survives.
    """
    session = session or db.session
    table = _ACCOUNT_TABLES.get(account_type)
    if table is None:
        raise ValueError(f'Unknown wallet account type: {account_type!r}')
    try:
        owner_id = int(owner_id)
        delta = int(delta)
    except (TypeError, ValueError):
        return False, 'invalid_amount'
    if delta == 0:
        return False, 'zero_amount'
    key = str(idempotency_key) if idempotency_key else None
    if key and ledger_entry_exists(key, session):
        return False, 'duplicate'

    if delta > 0:
        result = session.execute(
            text(f'UPDATE {table} SET credit = COALESCE(credit, 0) + :delta WHERE id = :id'),
            {'delta': delta, 'id': owner_id},
        )
    else:
        result = session.execute(
            text(
                f'UPDATE {table} SET credit = COALESCE(credit, 0) + :delta '
                'WHERE id = :id AND COALESCE(credit, 0) + :delta >= 0'
            ),
            {'delta': delta, 'id': owner_id},
        )
    if result.rowcount != 1:
        return False, 'insufficient_balance'

    # Only a successful balance change gets ledger rows.
    if transaction_row is not None:
        session.add(transaction_row)
        session.flush()

    entry = WalletLedger(
        account_type=account_type,
        owner_id=owner_id,
        transaction_id=getattr(transaction_row, 'id', None),
        amount=delta,
        type=str(entry_type or 'adjust')[:32],
        reference_type=(str(reference_type)[:32] if reference_type else None),
        reference_id=(int(reference_id) if reference_id is not None else None),
        idempotency_key=key,
        description=(str(description)[:255] if description else None),
    )
    session.add(entry)
    try:
        session.flush()
    except IntegrityError:
        # Another transaction already posted this idempotency key: undo this
        # whole unit of work so the balance change cannot survive without it.
        session.rollback()
        return False, 'duplicate'
    # The credit UPDATE bypassed the ORM; drop stale cached balances.
    try:
        session.expire_all()
    except Exception:
        pass
    return True, None
