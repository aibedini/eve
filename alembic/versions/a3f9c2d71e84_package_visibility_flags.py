"""package visibility flags

Revision ID: a3f9c2d71e84
Revises: 11b7afcfe0ee
Create Date: 2026-07-27 20:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3f9c2d71e84'
down_revision = '11b7afcfe0ee'
branch_labels = None
depends_on = None

# Self-healing: the v2.5.44 runtime catch-up in panel/migrate.py briefly added
# these columns BEFORE Alembic ran, so databases that attempted that update
# already have them while their ledger still sits at the baseline. Skip any
# column that already exists instead of failing with DuplicateColumn.
_NEW_COLUMNS = (
    ('show_on_create', sa.Column('show_on_create', sa.Boolean(), nullable=False, server_default=sa.true())),
    ('show_on_renew', sa.Column('show_on_renew', sa.Boolean(), nullable=False, server_default=sa.true())),
)


def _existing_columns() -> set:
    bind = op.get_bind()
    return {col['name'] for col in sa.inspect(bind).get_columns('packages')}


def upgrade() -> None:
    existing = _existing_columns()
    for name, column in _NEW_COLUMNS:
        if name not in existing:
            op.add_column('packages', column)


def downgrade() -> None:
    existing = _existing_columns()
    for name, _column in reversed(_NEW_COLUMNS):
        if name in existing:
            op.drop_column('packages', name)
