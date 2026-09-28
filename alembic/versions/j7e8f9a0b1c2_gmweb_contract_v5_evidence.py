"""Add GMweb contract v5 SMS evidence correlation.

Revision ID: j7e8f9a0b1c2
Revises: i6d7e8f9a0b1
"""

from alembic import op
import sqlalchemy as sa


revision = 'j7e8f9a0b1c2'
down_revision = 'i6d7e8f9a0b1'
branch_labels = None
depends_on = None


def _columns(inspector, table):
    return {column['name'] for column in inspector.get_columns(table)}


def _indexes(inspector, table):
    return {index['name'] for index in inspector.get_indexes(table) if index.get('name')}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    event_table = 'sms_gateway_events'
    event_columns = _columns(inspector, event_table)
    with op.batch_alter_table(event_table) as batch:
        batch.alter_column('event_id', existing_type=sa.String(160),
                           type_=sa.String(196), existing_nullable=False)
        batch.alter_column('eve_notification_id', existing_type=sa.String(128),
                           type_=sa.String(120), existing_nullable=True)
    for column in (
        sa.Column('request_id', sa.String(120), nullable=True),
        sa.Column('gateway_request_id', sa.String(120), nullable=True),
        sa.Column('carrier_status', sa.String(16), nullable=True),
        sa.Column('evidence', sa.String(64), nullable=True),
    ):
        if column.name not in event_columns:
            op.add_column(event_table, column)
    inspector = sa.inspect(bind)
    event_indexes = _indexes(inspector, event_table)
    for name, column in (
        ('ix_sms_gateway_events_request_id', 'request_id'),
        ('ix_sms_gateway_events_gateway_request_id', 'gateway_request_id'),
        ('ix_sms_gateway_events_occurred_at', 'occurred_at'),
    ):
        if name not in event_indexes:
            op.create_index(name, event_table, [column])

    log_table = 'sms_send_log'
    log_columns = _columns(sa.inspect(bind), log_table)
    for column in (
        sa.Column('eve_notification_id', sa.String(120), nullable=True),
        sa.Column('gateway_request_id', sa.String(120), nullable=True),
        sa.Column('carrier_state', sa.String(16), nullable=True),
        sa.Column('carrier_occurred_at', sa.String(64), nullable=True),
        sa.Column('carrier_evidence', sa.String(64), nullable=True),
    ):
        if column.name not in log_columns:
            op.add_column(log_table, column)
    log_indexes = _indexes(sa.inspect(bind), log_table)
    for name, column in (
        ('ix_sms_send_log_eve_notification_id', 'eve_notification_id'),
        ('ix_sms_send_log_gateway_request_id', 'gateway_request_id'),
    ):
        if name not in log_indexes:
            op.create_index(name, log_table, [column])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table('sms_send_log'):
        indexes = _indexes(inspector, 'sms_send_log')
        for name in ('ix_sms_send_log_gateway_request_id',
                     'ix_sms_send_log_eve_notification_id'):
            if name in indexes:
                op.drop_index(name, table_name='sms_send_log')
        columns = _columns(sa.inspect(bind), 'sms_send_log')
        for name in ('carrier_evidence', 'carrier_occurred_at', 'carrier_state',
                     'gateway_request_id', 'eve_notification_id'):
            if name in columns:
                op.drop_column('sms_send_log', name)
    inspector = sa.inspect(bind)
    if inspector.has_table('sms_gateway_events'):
        indexes = _indexes(inspector, 'sms_gateway_events')
        for name in ('ix_sms_gateway_events_occurred_at',
                     'ix_sms_gateway_events_gateway_request_id',
                     'ix_sms_gateway_events_request_id'):
            if name in indexes:
                op.drop_index(name, table_name='sms_gateway_events')
        columns = _columns(sa.inspect(bind), 'sms_gateway_events')
        for name in ('evidence', 'carrier_status', 'gateway_request_id', 'request_id'):
            if name in columns:
                op.drop_column('sms_gateway_events', name)
        with op.batch_alter_table('sms_gateway_events') as batch:
            batch.alter_column('eve_notification_id', existing_type=sa.String(120),
                               type_=sa.String(128), existing_nullable=True)
            batch.alter_column('event_id', existing_type=sa.String(196),
                               type_=sa.String(160), existing_nullable=False)
