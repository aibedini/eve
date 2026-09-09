"""Add durable client-operation idempotency and credit reservation ledger.

Revision ID: 04b9c0d1e2f3
Revises: f3a8b9c0d1e2
Create Date: 2026-08-31 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '04b9c0d1e2f3'
down_revision = 'f3a8b9c0d1e2'
branch_labels = None
depends_on = None


TABLE_NAME = 'client_operations'
INDEXES = (
    ('ix_client_operations_idempotency_key', ['idempotency_key'], True),
    ('ix_client_operations_action', ['action'], False),
    ('ix_client_operations_admin_id', ['admin_id'], False),
    ('ix_client_operations_server_id', ['server_id'], False),
    ('ix_client_operations_client_email', ['client_email'], False),
    ('ix_client_operations_state', ['state'], False),
    ('ix_client_operations_created_at', ['created_at'], False),
)


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    # panel/migrate.py intentionally runs db.create_all() before Alembic. An
    # older process can therefore create this model table while the durable
    # Alembic ledger still points at the preceding revision. Adopt that valid
    # state instead of failing with DuplicateTable, and fill any missing
    # indexes so an interrupted first attempt remains resumable.
    inspector = _inspector()
    if TABLE_NAME not in inspector.get_table_names():
        op.create_table(
            TABLE_NAME,
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('idempotency_key', sa.String(length=160), nullable=False),
            sa.Column('request_hash', sa.String(length=64), nullable=False),
            sa.Column('action', sa.String(length=32), nullable=False),
            sa.Column('admin_id', sa.Integer(), sa.ForeignKey('admins.id'), nullable=False),
            sa.Column('server_id', sa.Integer(), sa.ForeignKey('servers.id'), nullable=True),
            sa.Column('inbound_id', sa.Integer(), nullable=True),
            sa.Column('client_email', sa.String(length=100), nullable=True),
            sa.Column('amount', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('credit_reserved', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('state', sa.String(length=32), nullable=False, server_default='reserved'),
            sa.Column('expected_json', sa.Text(), nullable=True),
            sa.Column('response_json', sa.Text(), nullable=True),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('completed_at', sa.DateTime(), nullable=True),
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
