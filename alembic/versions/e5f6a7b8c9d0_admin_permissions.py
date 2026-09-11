"""Add per-admin permission overrides for permission-based RBAC.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-01-04 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


TABLE_NAME = 'admin_permissions'
INDEXES = (
    ('ix_admin_permissions_admin_id', ['admin_id'], False),
    ('ix_admin_permissions_permission', ['permission'], False),
    ('uq_admin_permission', ['admin_id', 'permission'], True),
)


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()
    if TABLE_NAME not in inspector.get_table_names():
        op.create_table(
            TABLE_NAME,
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('admin_id', sa.Integer(), sa.ForeignKey('admins.id', ondelete='CASCADE'), nullable=False),
            sa.Column('permission', sa.String(length=48), nullable=False),
            sa.Column('allowed', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        )
        existing_indexes = set()
    else:
        existing_indexes = {i['name'] for i in inspector.get_indexes(TABLE_NAME) if i.get('name')}
    for name, cols, unique in INDEXES:
        if name not in existing_indexes:
            op.create_index(name, TABLE_NAME, cols, unique=unique)


def downgrade():
    if TABLE_NAME in _inspector().get_table_names():
        op.drop_table(TABLE_NAME)
