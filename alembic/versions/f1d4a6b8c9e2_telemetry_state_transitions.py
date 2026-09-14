"""Durable observed service state + depletion notification outbox

Additive, and the schema half of the telemetry-transition pipeline:

* ``service_observed_states`` -- the last state Eve actually OBSERVED for a
  service (never what a mutation intended). A transition is computed by comparing
  a fresh panel read against this row, so detection no longer depends on when an
  SMS scan happened to run. ``state_version`` is monotonic and is one half of a
  notification event deduplication key.
* ``service_notification_events`` -- the durable outbox a transition writes into.
  Delivery takes a lease (``claimed_by``/``claimed_at``) so two workers can never
  send the same event, and a crashed worker's lease is reclaimable.
  The unique ``event_id`` is the cross-worker race barrier: two pollers observing
  the same transition converge on one row through a constraint, not through a
  check-then-insert. The remaining indexes cover the access patterns that matter:
  claiming due work, and looking up what was already notified for a
  (service, generation).

No backfill: an existing service simply has no observed row yet, and the first
observation establishes a baseline WITHOUT emitting a notification (documented in
docs/TELEMETRY_STATE_TRANSITIONS.md), which is what keeps a deploy from producing
an SMS storm.

Revision ID: f1d4a6b8c9e2
Revises: e9c3f5a7b8d1
Create Date: 2026-09-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f1d4a6b8c9e2'
down_revision = 'e9c3f5a7b8d1'
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    if not _has_table('service_observed_states'):
        op.create_table(
            'service_observed_states',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('service_key', sa.String(255), nullable=False),
            sa.Column('server_id', sa.Integer(), nullable=False),
            sa.Column('client_uuid', sa.String(100), nullable=True),
            sa.Column('client_email', sa.String(255), nullable=True),
            sa.Column('last_state', sa.String(32), nullable=True),
            sa.Column('last_state_tag', sa.String(32), nullable=True),
            sa.Column('last_remaining_bytes', sa.BigInteger(), nullable=True),
            sa.Column('last_total_bytes', sa.BigInteger(), nullable=True),
            sa.Column('last_expiry_ms', sa.BigInteger(), nullable=True),
            sa.Column('last_observed_at', sa.DateTime(), nullable=True),
            sa.Column('last_telemetry_updated_at', sa.DateTime(), nullable=True),
            sa.Column('state_version', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('service_key', name='uq_service_observed_service_key'),
        )
        op.create_index('ix_service_observed_states_service_key',
                        'service_observed_states', ['service_key'])
        op.create_index('ix_service_observed_states_server_id',
                        'service_observed_states', ['server_id'])
        op.create_index('ix_service_observed_states_client_email',
                        'service_observed_states', ['client_email'])
        op.create_index('ix_service_observed_states_last_observed_at',
                        'service_observed_states', ['last_observed_at'])
        op.create_index('ix_service_observed_identity',
                        'service_observed_states', ['server_id', 'client_uuid'])

    if not _has_table('service_notification_events'):
        op.create_table(
            'service_notification_events',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('event_id', sa.String(160), nullable=False),
            sa.Column('service_key', sa.String(255), nullable=False),
            sa.Column('server_id', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('client_uuid', sa.String(100), nullable=True),
            sa.Column('client_email', sa.String(255), nullable=True),
            sa.Column('state', sa.String(32), nullable=False),
            sa.Column('previous_state', sa.String(32), nullable=True),
            sa.Column('notification_kind', sa.String(32), nullable=False),
            sa.Column('state_version', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('lifecycle_generation', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('observed_at', sa.DateTime(), nullable=True),
            sa.Column('telemetry_updated_at', sa.DateTime(), nullable=True),
            sa.Column('source', sa.String(24), nullable=False, server_default='transition'),
            sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
            sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
            sa.Column('idempotency_key', sa.String(160), nullable=True),
            sa.Column('correlation_id', sa.String(64), nullable=True),
            sa.Column('gateway_request_id', sa.String(128), nullable=True),
            sa.Column('last_error', sa.String(255), nullable=True),
            sa.Column('last_status_code', sa.Integer(), nullable=True),
            sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
            sa.Column('claimed_by', sa.String(64), nullable=True),
            sa.Column('claimed_at', sa.DateTime(), nullable=True),
            sa.Column('superseded_reason', sa.String(64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('sent_at', sa.DateTime(), nullable=True),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('event_id', name='uq_service_notification_event_id'),
        )
        op.create_index('ix_service_notification_events_event_id',
                        'service_notification_events', ['event_id'])
        op.create_index('ix_service_notification_events_service_key',
                        'service_notification_events', ['service_key'])
        op.create_index('ix_service_notification_events_server_id',
                        'service_notification_events', ['server_id'])
        op.create_index('ix_service_notification_events_status',
                        'service_notification_events', ['status'])
        op.create_index('ix_service_notification_events_created_at',
                        'service_notification_events', ['created_at'])
        op.create_index('ix_service_notification_due', 'service_notification_events',
                        ['status', 'next_attempt_at'])
        op.create_index('ix_service_notification_service_generation',
                        'service_notification_events',
                        ['service_key', 'lifecycle_generation'])
        op.create_index('ix_service_notification_service_state',
                        'service_notification_events',
                        ['service_key', 'state', 'state_version'])
        op.create_index('ix_service_notification_identity',
                        'service_notification_events', ['server_id', 'client_uuid'])


def downgrade():
    if _has_table('service_notification_events'):
        op.drop_table('service_notification_events')
    if _has_table('service_observed_states'):
        op.drop_table('service_observed_states')
