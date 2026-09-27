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
    table = 'sms_gateway_events'
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        op.create_table(
            table,
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
        existing_indexes = set()
    else:
        columns = {column['name'] for column in inspector.get_columns(table)}
        required = {
            'event_id', 'trace_id', 'message_id', 'eve_notification_id',
            'event_type', 'occurred_at', 'received_at', 'attempt',
            'device_id', 'reason_code', 'stage',
        }
        missing = required - columns
        if missing:
            raise RuntimeError(f'{table} exists without required columns: {sorted(missing)}')
        existing_indexes = {
            index['name'] for index in inspector.get_indexes(table) if index.get('name')
        }
    for column in ('trace_id', 'message_id', 'eve_notification_id', 'event_type'):
        name = f'ix_sms_gateway_events_{column}'
        if name not in existing_indexes:
            op.create_index(name, table, [column])


def downgrade():
    if sa.inspect(op.get_bind()).has_table('sms_gateway_events'):
        op.drop_table('sms_gateway_events')
