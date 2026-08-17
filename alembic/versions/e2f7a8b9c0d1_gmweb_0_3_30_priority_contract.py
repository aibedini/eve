"""GMweb 0.3.30 priority and verification audit fields

Revision ID: e2f7a8b9c0d1
Revises: d1e6f7a8b9c0
Create Date: 2026-08-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e2f7a8b9c0d1'
down_revision = 'd1e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sms_send_log') as batch_op:
        batch_op.add_column(sa.Column('priority', sa.String(length=24), nullable=True))
        batch_op.add_column(sa.Column('priority_level', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('queue_position', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('last_http_status', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('submitted_once', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('verification_status', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('verification_attempts', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('requested_to', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('sent_to', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('recipient_evidence', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('conversation_url', sa.Text(), nullable=True))

    with op.batch_alter_table('announcement_deliveries') as batch_op:
        batch_op.add_column(sa.Column('gateway_priority', sa.String(length=24), nullable=True))
        batch_op.add_column(sa.Column('gateway_priority_level', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('gateway_submitted_once', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('gateway_verification_status', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('gateway_sent_to', sa.String(length=32), nullable=True))


def downgrade():
    with op.batch_alter_table('announcement_deliveries') as batch_op:
        batch_op.drop_column('gateway_sent_to')
        batch_op.drop_column('gateway_verification_status')
        batch_op.drop_column('gateway_submitted_once')
        batch_op.drop_column('gateway_priority_level')
        batch_op.drop_column('gateway_priority')

    with op.batch_alter_table('sms_send_log') as batch_op:
        batch_op.drop_column('conversation_url')
        batch_op.drop_column('recipient_evidence')
        batch_op.drop_column('sent_to')
        batch_op.drop_column('requested_to')
        batch_op.drop_column('verification_attempts')
        batch_op.drop_column('verification_status')
        batch_op.drop_column('submitted_once')
        batch_op.drop_column('last_http_status')
        batch_op.drop_column('queue_position')
        batch_op.drop_column('priority_level')
        batch_op.drop_column('priority')
