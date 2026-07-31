"""sub app action buttons

Revision ID: d5e9f3a7b2c1
Revises: c4d8e1f2a6b9
Create Date: 2026-07-31 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd5e9f3a7b2c1'
down_revision = 'c4d8e1f2a6b9'
branch_labels = None
depends_on = None


def _existing_columns() -> set:
    bind = op.get_bind()
    return {col['name'] for col in sa.inspect(bind).get_columns('sub_app_configs')}


def upgrade() -> None:
    if 'action_buttons' not in _existing_columns():
        op.add_column(
            'sub_app_configs',
            sa.Column('action_buttons', sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if 'action_buttons' in _existing_columns():
        op.drop_column('sub_app_configs', 'action_buttons')
