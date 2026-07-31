"""subscription inbound ordering

Revision ID: c4d8e1f2a6b9
Revises: b7e2c9a41d05
Create Date: 2026-07-31 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c4d8e1f2a6b9'
down_revision = 'b7e2c9a41d05'
branch_labels = None
depends_on = None


def _existing_columns() -> set:
    bind = op.get_bind()
    return {col['name'] for col in sa.inspect(bind).get_columns('servers')}


def upgrade() -> None:
    if 'subscription_inbound_order' not in _existing_columns():
        op.add_column(
            'servers',
            sa.Column(
                'subscription_inbound_order',
                sa.Text(),
                nullable=False,
                server_default='[]',
            ),
        )


def downgrade() -> None:
    if 'subscription_inbound_order' in _existing_columns():
        op.drop_column('servers', 'subscription_inbound_order')
