"""Persist SMS provider routing for custom HTTP gateways.

Revision ID: f3a8b9c0d1e2
Revises: e2f7a8b9c0d1
Create Date: 2026-08-23 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f3a8b9c0d1e2'
down_revision = 'e2f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sms_send_log') as batch_op:
        batch_op.add_column(sa.Column('gateway_provider', sa.String(length=24), nullable=False,
                                      server_default='gmweb'))
        batch_op.create_index('ix_sms_send_log_gateway_provider', ['gateway_provider'], unique=False)
    with op.batch_alter_table('announcement_deliveries') as batch_op:
        batch_op.add_column(sa.Column('gateway_provider', sa.String(length=24), nullable=False,
                                      server_default='gmweb'))
        batch_op.create_index('ix_announcement_deliveries_gateway_provider', ['gateway_provider'], unique=False)


def downgrade():
    with op.batch_alter_table('announcement_deliveries') as batch_op:
        batch_op.drop_index('ix_announcement_deliveries_gateway_provider')
        batch_op.drop_column('gateway_provider')
    with op.batch_alter_table('sms_send_log') as batch_op:
        batch_op.drop_index('ix_sms_send_log_gateway_provider')
        batch_op.drop_column('gateway_provider')
