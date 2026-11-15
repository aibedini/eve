"""machine API keys for the read-only EVE Context API v1

Revision ID: f4c1a9b2d3e5
Revises: m0b1c2d3e4f5
Create Date: 2026-11-15 10:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4c1a9b2d3e5'
down_revision = 'm0b1c2d3e4f5'
branch_labels = None
depends_on = None

_TABLE = 'machine_api_keys'
_KEY_HASH_INDEX = 'ix_machine_api_keys_key_hash'


def _table_exists(inspector) -> bool:
    return _TABLE in set(inspector.get_table_names())


def _index_names(inspector) -> set:
    if not _table_exists(inspector):
        return set()
    return {index.get('name') for index in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    # Self-healing: panel/migrate.py runs db.create_all() before `upgrade head`,
    # so a database that booted the app once already has this table and its
    # unique index while its Alembic ledger is still behind. Skip whatever
    # already exists instead of failing with DuplicateTable/DuplicateIndex.
    inspector = sa.inspect(op.get_bind())
    if not _table_exists(inspector):
        op.create_table(
            _TABLE,
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('key_hash', sa.String(length=64), nullable=False),
            sa.Column('scopes', sa.Text(), nullable=False, server_default='[]'),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('created_by', sa.String(length=100), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        inspector = sa.inspect(op.get_bind())
    if _KEY_HASH_INDEX not in _index_names(inspector):
        op.create_index(_KEY_HASH_INDEX, _TABLE, ['key_hash'], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _KEY_HASH_INDEX in _index_names(inspector):
        op.drop_index(_KEY_HASH_INDEX, table_name=_TABLE)
    if _table_exists(inspector):
        op.drop_table(_TABLE)
