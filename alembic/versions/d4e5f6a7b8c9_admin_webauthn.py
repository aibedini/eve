"""Add registered WebAuthn/passkey credentials for admin accounts.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-01-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


TABLE_NAME = 'admin_webauthn_credentials'
INDEXES = (
    ('ix_admin_webauthn_credentials_admin_id', ['admin_id'], False),
    ('ix_admin_webauthn_credentials_credential_id', ['credential_id'], True),
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
            sa.Column('credential_id', sa.String(length=255), nullable=False),
            sa.Column('public_key_pem', sa.Text(), nullable=False),
            sa.Column('alg', sa.Integer(), nullable=False, server_default='-7'),
            sa.Column('sign_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('aaguid', sa.String(length=64), nullable=True),
            sa.Column('name', sa.String(length=120), nullable=True),
            sa.Column('transports', sa.String(length=64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
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
