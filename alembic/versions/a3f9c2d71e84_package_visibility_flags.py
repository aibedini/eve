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


def upgrade() -> None:
    op.add_column('packages', sa.Column('show_on_create', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('packages', sa.Column('show_on_renew', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('packages', 'show_on_renew')
    op.drop_column('packages', 'show_on_create')
