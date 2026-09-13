"""RenewalEvent v2: separate business events from inferred counter resets.

Additive only. Every new column is nullable (or carries a safe server default), so an
existing row stays valid and nothing is dropped:

* the business-event vocabulary (``event_type`` / ``source``) with fail-safe defaults -
  a legacy row becomes an unverified ``inferred_reset`` from ``counter_reset``, never a
  verified renewal, because none of the pre-v2 rows had a verified panel read-back;
* the rollover accounting (previous/new volume limit, remaining, carried-over, granted) so
  a rolled-over quota can never be mistaken for a bigger purchase;
* the idempotency link (``operation_id`` + a unique ``(operation_id, event_type)``
  constraint) so a retried renewal cannot open a second cycle;
* the verified flag/timestamp, true only after a panel read-back;
* the indexes that make "latest authoritative cycle boundary" an indexed lookup:
  ``(server_id, sub_id, renewed_at)`` and ``(server_id, sub_id, verified, renewed_at)``.

Revision ID: b8d2e3f4a5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-09-12 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b8d2e3f4a5c6'
down_revision = 'c9d8e7f6a5b4'
branch_labels = None
depends_on = None


_NEW_COLUMNS = (
    ('client_uuid', sa.Column('client_uuid', sa.String(64), nullable=True)),
    ('client_email_snapshot', sa.Column('client_email_snapshot', sa.String(255), nullable=True)),
    ('event_type', sa.Column('event_type', sa.String(32), nullable=False,
                             server_default='inferred_reset')),
    ('source', sa.Column('source', sa.String(32), nullable=False,
                         server_default='inferred')),
    ('previous_volume_limit_bytes', sa.Column('previous_volume_limit_bytes',
                                              sa.BigInteger(), nullable=True)),
    ('new_volume_limit_bytes', sa.Column('new_volume_limit_bytes',
                                         sa.BigInteger(), nullable=True)),
    ('previous_remaining_bytes', sa.Column('previous_remaining_bytes',
                                           sa.BigInteger(), nullable=True)),
    ('carried_over_bytes', sa.Column('carried_over_bytes', sa.BigInteger(), nullable=True)),
    ('granted_volume_bytes', sa.Column('granted_volume_bytes', sa.BigInteger(), nullable=True)),
    ('previous_expiry_at', sa.Column('previous_expiry_at', sa.DateTime(), nullable=True)),
    ('new_expiry_at', sa.Column('new_expiry_at', sa.DateTime(), nullable=True)),
    ('traffic_reset', sa.Column('traffic_reset', sa.Boolean(), nullable=False,
                                server_default=sa.text('false'))),
    ('operation_id', sa.Column('operation_id', sa.String(64), nullable=True)),
    ('verified', sa.Column('verified', sa.Boolean(), nullable=False,
                           server_default=sa.text('false'))),
    ('verified_at', sa.Column('verified_at', sa.DateTime(), nullable=True)),
    ('created_at', sa.Column('created_at', sa.DateTime(), nullable=True)),
)

_INDEXES = (
    ('ix_renewal_events_server_sub_renewed',
     ('server_id', 'sub_id', 'renewed_at')),
    ('ix_renewal_events_server_sub_verified_renewed',
     ('server_id', 'sub_id', 'verified', 'renewed_at')),
    ('ix_renewal_events_operation_id', ('operation_id',)),
)


def _columns(table):
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return None, inspector
    return {column['name'] for column in inspector.get_columns(table)}, inspector


def upgrade():
    existing, inspector = _columns('renewal_events')
    if existing is None:
        # A fresh database: db.create_all()/the baseline owns the table layout.
        return

    with op.batch_alter_table('renewal_events') as batch_op:
        for name, column in _NEW_COLUMNS:
            if name not in existing:
                batch_op.add_column(column)

    index_names = {index['name'] for index in inspector.get_indexes('renewal_events')}
    for name, columns in _INDEXES:
        if name not in index_names:
            op.create_index(name, 'renewal_events', list(columns), unique=False)

    unique_names = {constraint['name']
                    for constraint in inspector.get_unique_constraints('renewal_events')}
    if 'uq_renewal_events_operation_type' not in unique_names:
        with op.batch_alter_table('renewal_events') as batch_op:
            batch_op.create_unique_constraint(
                'uq_renewal_events_operation_type', ['operation_id', 'event_type'])

    # Legacy rows were produced by the counter-decrease collector: record that
    # provenance honestly and keep them out of the cycle-boundary query.
    op.execute(
        "UPDATE renewal_events SET event_type = 'inferred_reset', "
        "source = 'counter_reset', verified = false "
        "WHERE event_type = 'inferred_reset' AND source = 'inferred'"
    )
    op.execute(
        "UPDATE renewal_events SET created_at = renewed_at WHERE created_at IS NULL"
    )


def downgrade():
    existing, inspector = _columns('renewal_events')
    if existing is None:
        return
    index_names = {index['name'] for index in inspector.get_indexes('renewal_events')}
    for name, _columns_ in _INDEXES:
        if name in index_names:
            op.drop_index(name, table_name='renewal_events')
    unique_names = {constraint['name']
                    for constraint in inspector.get_unique_constraints('renewal_events')}
    with op.batch_alter_table('renewal_events') as batch_op:
        if 'uq_renewal_events_operation_type' in unique_names:
            batch_op.drop_constraint('uq_renewal_events_operation_type', type_='unique')
        for name, _column in reversed(_NEW_COLUMNS):
            if name in existing:
                batch_op.drop_column(name)
