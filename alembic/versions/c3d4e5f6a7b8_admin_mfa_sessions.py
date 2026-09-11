"""Add admin MFA settings, backup codes and the server-side session registry.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-01-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


TABLES = {
    'admin_mfa_settings': (
        (
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('admin_id', sa.Integer(), sa.ForeignKey('admins.id', ondelete='CASCADE'), nullable=False),
            sa.Column('totp_secret', sa.Text(), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('confirmed_at', sa.DateTime(), nullable=True),
            sa.Column('last_counter', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
        ),
        (
            ('ix_admin_mfa_settings_admin_id', ['admin_id'], True),
        ),
    ),
    'admin_mfa_backup_codes': (
        (
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('admin_id', sa.Integer(), sa.ForeignKey('admins.id', ondelete='CASCADE'), nullable=False),
            sa.Column('code_hash', sa.String(length=64), nullable=False),
            sa.Column('used_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
        ),
        (
            ('ix_admin_mfa_backup_codes_admin_id', ['admin_id'], False),
            ('ix_admin_mfa_backup_codes_code_hash', ['code_hash'], True),
        ),
    ),
    'admin_sessions': (
        (
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('admin_id', sa.Integer(), sa.ForeignKey('admins.id', ondelete='CASCADE'), nullable=False),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('ip', sa.String(length=64), nullable=True),
            sa.Column('user_agent', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('mfa_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('step_up_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
        ),
        (
            ('ix_admin_sessions_admin_id', ['admin_id'], False),
            ('ix_admin_sessions_token_hash', ['token_hash'], True),
            ('ix_admin_sessions_created_at', ['created_at'], False),
            ('ix_admin_sessions_revoked_at', ['revoked_at'], False),
        ),
    ),
}


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()
    existing_tables = set(inspector.get_table_names())
    for table, (columns, indexes) in TABLES.items():
        if table not in existing_tables:
            op.create_table(table, *columns)
            existing_indexes = set()
        else:
            existing_indexes = {i['name'] for i in inspector.get_indexes(table) if i.get('name')}
        for name, cols, unique in indexes:
            if name not in existing_indexes:
                op.create_index(name, table, cols, unique=unique)


def downgrade():
    existing = set(_inspector().get_table_names())
    for table in TABLES:
        if table in existing:
            op.drop_table(table)
