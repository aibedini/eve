"""announcement action buttons

Revision ID: e6f1a2b3c4d5
Revises: d5e9f3a7b2c1
Create Date: 2026-08-02 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e6f1a2b3c4d5'
down_revision = 'd5e9f3a7b2c1'
branch_labels = None
depends_on = None


def _existing_columns() -> set:
    bind = op.get_bind()
    return {col['name'] for col in sa.inspect(bind).get_columns('announcements')}


def upgrade() -> None:
    columns = _existing_columns()
    if 'action_buttons' not in columns:
        op.add_column(
            'announcements',
            sa.Column('action_buttons', sa.Text(), nullable=True),
        )
    if 'button_columns' not in columns:
        op.add_column(
            'announcements',
            sa.Column('button_columns', sa.Integer(), nullable=False, server_default='1'),
        )


def downgrade() -> None:
    columns = _existing_columns()
    if 'button_columns' in columns:
        op.drop_column('announcements', 'button_columns')
    if 'action_buttons' in columns:
        op.drop_column('announcements', 'action_buttons')
