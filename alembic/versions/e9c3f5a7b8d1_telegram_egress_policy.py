"""Telegram egress policy storage

Widens `telegram_bot_instances.connection_mode` from VARCHAR(24) to VARCHAR(40)
so a canonical egress policy name fits whole: PROXY_PREFERRED,
PANEL_ACCOUNT_REQUIRED, NEVER_DIRECT, ...

No data rewrite and no new column: the legacy mode names already stored
(`auto`, `direct_only`, `proxy_first`, `proxy_only`) stay valid and are mapped to
the policy that matches what they actually did by
`panel/telegram_egress.normalize_policy`, so an upgrade cannot tighten a live
deployment's egress by surprise. Save-path input is normalized to the new
vocabulary, so the column converges without a backfill.

Reversible: the downgrade narrows the column again. Rows longer than 24
characters are truncated by the database, which is why the application never
writes anything but a valid policy name.

Revision ID: e9c3f5a7b8d1
Revises: d8b2e4f6a7c9
Create Date: 2026-09-14 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e9c3f5a7b8d1'
down_revision = 'd8b2e4f6a7c9'
branch_labels = None
depends_on = None


_TABLE = "telegram_bot_instances"
_COLUMN = "connection_mode"


def _current_length(bind):
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return None
    for column in inspector.get_columns(_TABLE):
        if column["name"] == _COLUMN:
            return getattr(column["type"], "length", None)
    return None


def upgrade():
    bind = op.get_bind()
    if _current_length(bind) is None:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.String(length=24),
            type_=sa.String(length=40),
            existing_nullable=False,
        )


def downgrade():
    bind = op.get_bind()
    length = _current_length(bind)
    if length is None:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.String(length=40),
            type_=sa.String(length=24),
            existing_nullable=False,
        )
