"""Add the tamper-evident hash chain to audit_logs.

Revision ID: a1b2c3d4e5f6
Revises: e5f6a7b8c9d0
Create Date: 2026-09-12 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


COLUMNS = (
    ('request_id', sa.String(length=64)),
    ('source_ip', sa.String(length=64)),
    ('user_agent', sa.String(length=200)),
    ('prev_hash', sa.String(length=64)),
    ('entry_hash', sa.String(length=64)),
)


def upgrade():
    with op.batch_alter_table('audit_logs') as batch_op:
        for name, column_type in COLUMNS:
            batch_op.add_column(sa.Column(name, column_type, nullable=True))


def downgrade():
    with op.batch_alter_table('audit_logs') as batch_op:
        for name, _column_type in reversed(COLUMNS):
            batch_op.drop_column(name)
