"""Add immutable wallet ledger with unique idempotency keys.

Revision ID: b2c3d4e5f6a7
Revises: 26d1e2f3a4b5
Create Date: 2026-01-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = '26d1e2f3a4b5'
branch_labels = None
depends_on = None


TABLE_NAME = 'wallet_ledger'
INDEXES = (
    ('ix_wallet_ledger_account_type', ['account_type'], False),
    ('ix_wallet_ledger_owner_id', ['owner_id'], False),
    ('ix_wallet_ledger_type', ['type'], False),
    ('ix_wallet_ledger_reference_type', ['reference_type'], False),
    ('ix_wallet_ledger_reference_id', ['reference_id'], False),
    ('ix_wallet_ledger_created_at', ['created_at'], False),
    ('ix_wallet_ledger_idempotency_key', ['idempotency_key'], True),
)


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    # panel/migrate.py runs db.create_all() before Alembic, so a process running
    # the new code can create this table while the Alembic ledger still points at
    # the previous revision. Adopt that valid state and backfill missing indexes.
    inspector = _inspector()
    if TABLE_NAME not in inspector.get_table_names():
        op.create_table(
            TABLE_NAME,
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('account_type', sa.String(length=16), nullable=False),
            sa.Column('owner_id', sa.Integer(), nullable=False),
            sa.Column('transaction_id', sa.Integer(), nullable=True),
            sa.Column('amount', sa.Integer(), nullable=False),
            sa.Column('type', sa.String(length=32), nullable=False),
            sa.Column('reference_type', sa.String(length=32), nullable=True),
            sa.Column('reference_id', sa.Integer(), nullable=True),
            sa.Column('idempotency_key', sa.String(length=128), nullable=True),
            sa.Column('description', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        existing_indexes = set()
    else:
        existing_indexes = {
            index['name'] for index in inspector.get_indexes(TABLE_NAME)
            if index.get('name')
        }

    for name, columns, unique in INDEXES:
        if name not in existing_indexes:
            op.create_index(name, TABLE_NAME, columns, unique=unique)


def downgrade():
    if TABLE_NAME in _inspector().get_table_names():
        op.drop_table(TABLE_NAME)
