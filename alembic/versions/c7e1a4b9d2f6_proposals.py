"""proposals inbox for GOD AI submissions (XR-GA-019)

Revision ID: c7e1a4b9d2f6
Revises: f4c1a9b2d3e5
Create Date: 2026-11-15 13:15:00.000000

The machine API's proposal endpoints arrived with XR-GA-009's ``/machine/v1``
blueprint; this revision adds the durable store behind them and behind the
operator review page.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7e1a4b9d2f6'
down_revision = 'f4c1a9b2d3e5'
branch_labels = None
depends_on = None

_TABLE = 'proposals'
_UNIQUE_INDEX = 'ix_proposals_proposal_id'
_STATUS_INDEX = 'ix_proposals_status_created'


def _table_exists(inspector) -> bool:
    return _TABLE in set(inspector.get_table_names())


def _index_names(inspector) -> set:
    if not _table_exists(inspector):
        return set()
    return {index.get('name') for index in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    # Self-healing: panel/migrate.py runs db.create_all() before `upgrade head`,
    # so a database that booted the app once already has this table and its
    # indexes while its Alembic ledger is still behind. Skip whatever already
    # exists instead of failing with DuplicateTable/DuplicateIndex.
    inspector = sa.inspect(op.get_bind())
    if not _table_exists(inspector):
        op.create_table(
            _TABLE,
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('proposal_id', sa.String(length=128), nullable=False),
            sa.Column('type', sa.String(length=32), nullable=False),
            sa.Column('customer_id', sa.Integer(), nullable=True),
            sa.Column('service_key', sa.String(length=255), nullable=True),
            sa.Column('generation', sa.BigInteger(), nullable=True),
            sa.Column('payload', sa.Text(), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False,
                      server_default='pending'),
            sa.Column('reason', sa.String(length=500), nullable=True),
            sa.Column('created_by_key_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('decided_by', sa.String(length=100), nullable=True),
            sa.Column('decided_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        inspector = sa.inspect(op.get_bind())
    indexes = _index_names(inspector)
    if _UNIQUE_INDEX not in indexes:
        op.create_index(_UNIQUE_INDEX, _TABLE, ['proposal_id'], unique=True)
    if _STATUS_INDEX not in indexes:
        op.create_index(_STATUS_INDEX, _TABLE, ['status', 'created_at'])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = _index_names(inspector)
    for name in (_STATUS_INDEX, _UNIQUE_INDEX):
        if name in indexes:
            op.drop_index(name, table_name=_TABLE)
    if _table_exists(inspector):
        op.drop_table(_TABLE)
