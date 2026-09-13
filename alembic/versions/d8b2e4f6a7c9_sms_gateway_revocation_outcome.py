"""Gateway-side revocation outcome on the SMS send log.

Additive follow-up to c7a1f2e3d4b5. The gateway does not delete a queued reminder
when a lifecycle invalidation arrives: it marks the row terminal as `superseded`
and keeps it queryable, exposing `outcome`, `revokedAt` and `revocationReason` on
`GET /send/status/{requestId}`. EVE records those three so the operator sees the
reminder as revoked-by-renewal instead of `queued` for ever.

Every column is nullable: existing rows stay valid and simply have no gateway
verdict recorded.

Revision ID: d8b2e4f6a7c9
Revises: c7a1f2e3d4b5
Create Date: 2026-09-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd8b2e4f6a7c9'
down_revision = 'c7a1f2e3d4b5'
branch_labels = None
depends_on = None


_COLUMNS = (
    ('gateway_outcome', sa.Column('gateway_outcome', sa.String(24), nullable=True)),
    ('revocation_reason', sa.Column('revocation_reason', sa.String(120), nullable=True)),
    ('revoked_at', sa.Column('revoked_at', sa.String(64), nullable=True)),
)


def upgrade():
    bind = op.get_bind()
    if 'sms_send_log' not in set(sa.inspect(bind).get_table_names()):
        return
    existing = {c['name'] for c in sa.inspect(bind).get_columns('sms_send_log')}
    with op.batch_alter_table('sms_send_log') as batch_op:
        for name, column in _COLUMNS:
            if name not in existing:
                batch_op.add_column(column)


def downgrade():
    bind = op.get_bind()
    if 'sms_send_log' not in set(sa.inspect(bind).get_table_names()):
        return
    existing = {c['name'] for c in sa.inspect(bind).get_columns('sms_send_log')}
    with op.batch_alter_table('sms_send_log') as batch_op:
        for name, _column in reversed(_COLUMNS):
            if name in existing:
                batch_op.drop_column(name)
