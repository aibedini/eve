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


def upgrade():
    op.create_table(
        'client_operations',
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
    for name, columns in (
        ('ix_client_operations_idempotency_key', ['idempotency_key']),
        ('ix_client_operations_action', ['action']),
        ('ix_client_operations_admin_id', ['admin_id']),
        ('ix_client_operations_server_id', ['server_id']),
        ('ix_client_operations_client_email', ['client_email']),
        ('ix_client_operations_state', ['state']),
        ('ix_client_operations_created_at', ['created_at']),
    ):
        op.create_index(name, 'client_operations', columns, unique=name.endswith('idempotency_key'))


def downgrade():
    op.drop_table('client_operations')
