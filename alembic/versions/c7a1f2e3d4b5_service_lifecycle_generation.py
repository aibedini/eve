"""Service lifecycle generation + notification invalidation outbox.

Additive only, and the single schema change behind the renewal/SMS consistency
fix:

* ``service_lifecycle_states`` -- the DURABLE per-service notification generation
  (``eve:<serverId>:<clientUuid>`` -> monotonic integer). It is the cross-worker
  barrier between a renewal and a depletion scan: worker A renews and bumps the
  generation, worker B must not submit a reminder classified before that bump.
  A unique constraint on ``service_key`` plus an ``(server_id, client_uuid)``
  index keep identity resolution an indexed lookup.
* ``service_notification_outbox`` -- the durable invalidation queue. One row per
  lifecycle change, committed with the generation bump, retried with bounded
  backoff. ``(status, next_attempt_at)`` is the resumable cursor and ``event_id``
  is unique so a replayed renewal cannot enqueue the same invalidation twice.
* ``sms_send_log`` gains the audit columns that answer, after a customer
  complaint, which service generation produced a message, whether the renewal
  revoked it, and how long the gateway took to confirm.

Every new ``sms_send_log`` column is nullable: existing rows stay valid and are
simply never matched by a lifecycle query.

Revision ID: c7a1f2e3d4b5
Revises: b8d2e3f4a5c6
Create Date: 2026-09-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c7a1f2e3d4b5'
down_revision = 'b8d2e3f4a5c6'
branch_labels = None
depends_on = None


_SMS_AUDIT_COLUMNS = (
    ('service_key', sa.Column('service_key', sa.String(255), nullable=True)),
    ('lifecycle_generation', sa.Column('lifecycle_generation', sa.Integer(), nullable=True)),
    ('correlation_id', sa.Column('correlation_id', sa.String(64), nullable=True)),
    ('idempotency_key', sa.Column('idempotency_key', sa.String(200), nullable=True)),
    ('candidate_observed_at', sa.Column('candidate_observed_at', sa.DateTime(), nullable=True)),
    ('last_lifecycle_change_at', sa.Column('last_lifecycle_change_at', sa.DateTime(), nullable=True)),
    ('lifecycle_event_id', sa.Column('lifecycle_event_id', sa.String(128), nullable=True)),
    ('invalidated_at', sa.Column('invalidated_at', sa.DateTime(), nullable=True)),
    ('invalidation_reason', sa.Column('invalidation_reason', sa.String(64), nullable=True)),
)

_SMS_AUDIT_INDEXES = (
    ('ix_sms_send_log_service_key', ('service_key',)),
    ('ix_sms_send_log_correlation_id', ('correlation_id',)),
    ('ix_sms_send_log_invalidated_at', ('invalidated_at',)),
)


def _has_table(name: str) -> bool:
    return name in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    if not _has_table('service_lifecycle_states'):
        op.create_table(
            'service_lifecycle_states',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('service_key', sa.String(255), nullable=False),
            sa.Column('server_id', sa.Integer(), nullable=False),
            sa.Column('client_uuid', sa.String(100), nullable=True),
            sa.Column('client_email', sa.String(255), nullable=True),
            sa.Column('generation', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_lifecycle_change_at', sa.DateTime(), nullable=True),
            sa.Column('last_renewed_at', sa.DateTime(), nullable=True),
            sa.Column('last_event_type', sa.String(32), nullable=True),
            sa.Column('last_operation_id', sa.String(128), nullable=True),
            sa.Column('last_correlation_id', sa.String(64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('service_key', name='uq_service_lifecycle_service_key'),
        )
        op.create_index('ix_service_lifecycle_states_service_key',
                        'service_lifecycle_states', ['service_key'])
        op.create_index('ix_service_lifecycle_states_server_id',
                        'service_lifecycle_states', ['server_id'])
        op.create_index('ix_service_lifecycle_states_client_email',
                        'service_lifecycle_states', ['client_email'])
        op.create_index('ix_service_lifecycle_states_last_lifecycle_change_at',
                        'service_lifecycle_states', ['last_lifecycle_change_at'])
        op.create_index('ix_service_lifecycle_identity',
                        'service_lifecycle_states', ['server_id', 'client_uuid'])

    if not _has_table('service_notification_outbox'):
        op.create_table(
            'service_notification_outbox',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('event_id', sa.String(128), nullable=False),
            sa.Column('service_key', sa.String(255), nullable=False),
            sa.Column('server_id', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('client_uuid', sa.String(100), nullable=True),
            sa.Column('generation', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('reason', sa.String(64), nullable=True),
            sa.Column('correlation_id', sa.String(64), nullable=True),
            sa.Column('invalidate_kinds', sa.String(255), nullable=False, server_default=''),
            sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
            sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
            sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
            sa.Column('last_error', sa.String(255), nullable=True),
            sa.Column('last_status_code', sa.Integer(), nullable=True),
            sa.Column('cancelled_pending', sa.Integer(), nullable=True),
            sa.Column('revoked_active', sa.Integer(), nullable=True),
            sa.Column('revoked_inflight', sa.Integer(), nullable=True),
            sa.Column('already_terminal', sa.Integer(), nullable=True),
            sa.Column('response_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('event_id', name='uq_service_outbox_event_id'),
        )
        op.create_index('ix_service_notification_outbox_service_key',
                        'service_notification_outbox', ['service_key'])
        op.create_index('ix_service_notification_outbox_server_id',
                        'service_notification_outbox', ['server_id'])
        op.create_index('ix_service_notification_outbox_status',
                        'service_notification_outbox', ['status'])
        op.create_index('ix_service_notification_outbox_next_attempt_at',
                        'service_notification_outbox', ['next_attempt_at'])
        op.create_index('ix_service_notification_outbox_created_at',
                        'service_notification_outbox', ['created_at'])
        op.create_index('ix_service_outbox_due', 'service_notification_outbox',
                        ['status', 'next_attempt_at'])

    if _has_table('sms_send_log'):
        inspector = sa.inspect(op.get_bind())
        existing = {column['name'] for column in inspector.get_columns('sms_send_log')}
        with op.batch_alter_table('sms_send_log') as batch_op:
            for name, column in _SMS_AUDIT_COLUMNS:
                if name not in existing:
                    batch_op.add_column(column)
        index_names = {index['name'] for index in inspector.get_indexes('sms_send_log')}
        for name, columns in _SMS_AUDIT_INDEXES:
            if name not in index_names:
                op.create_index(name, 'sms_send_log', list(columns), unique=False)


def downgrade():
    if _has_table('sms_send_log'):
        inspector = sa.inspect(op.get_bind())
        index_names = {index['name'] for index in inspector.get_indexes('sms_send_log')}
        existing = {column['name'] for column in inspector.get_columns('sms_send_log')}
        for name, _columns in _SMS_AUDIT_INDEXES:
            if name in index_names:
                op.drop_index(name, table_name='sms_send_log')
        with op.batch_alter_table('sms_send_log') as batch_op:
            for name, _column in reversed(_SMS_AUDIT_COLUMNS):
                if name in existing:
                    batch_op.drop_column(name)

    if _has_table('service_notification_outbox'):
        op.drop_table('service_notification_outbox')
    if _has_table('service_lifecycle_states'):
        op.drop_table('service_lifecycle_states')
