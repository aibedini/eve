"""announcement failure tracking and manual resend generations

Revision ID: c0d5e6f7a8b9
Revises: b9c4d5e6f7a8
Create Date: 2026-08-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c0d5e6f7a8b9'
down_revision = 'b9c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('announcement_deliveries') as batch_op:
        batch_op.add_column(sa.Column(
            'resend_count', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('last_error_source', sa.String(length=24), nullable=True))
        batch_op.add_column(sa.Column('gateway_request_id', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('gateway_state', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('gateway_stage', sa.String(length=64), nullable=True))
        batch_op.create_index(
            'ix_announcement_deliveries_gateway_request_id', ['gateway_request_id'], unique=False)


def downgrade():
    with op.batch_alter_table('announcement_deliveries') as batch_op:
        batch_op.drop_index('ix_announcement_deliveries_gateway_request_id')
        batch_op.drop_column('gateway_stage')
        batch_op.drop_column('gateway_state')
        batch_op.drop_column('gateway_request_id')
        batch_op.drop_column('last_error_source')
        batch_op.drop_column('resend_count')
