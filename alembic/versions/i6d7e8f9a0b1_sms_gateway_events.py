"""Store signed GMweb SMS lifecycle evidence.

Revision ID: i6d7e8f9a0b1
Revises: h5c6d7e8f9a0
"""

from alembic import op
import sqlalchemy as sa


revision = 'i6d7e8f9a0b1'
down_revision = 'h5c6d7e8f9a0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sms_gateway_events',
        sa.Column('event_id', sa.String(160), primary_key=True),
        sa.Column('trace_id', sa.String(64), nullable=False),
        sa.Column('message_id', sa.String(128), nullable=False),
        sa.Column('eve_notification_id', sa.String(128)),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.Column('attempt', sa.Integer()),
        sa.Column('device_id', sa.String(64)),
        sa.Column('reason_code', sa.String(64)),
        sa.Column('stage', sa.String(64)),
    )
    for column in ('trace_id', 'message_id', 'eve_notification_id', 'event_type'):
        op.create_index(f'ix_sms_gateway_events_{column}', 'sms_gateway_events', [column])


def downgrade():
    op.drop_table('sms_gateway_events')
