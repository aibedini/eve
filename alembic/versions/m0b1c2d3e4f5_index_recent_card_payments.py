"""Index recent payment declarations by destination card.

Revision ID: m0b1c2d3e4f5
Revises: l9a0b1c2d3e4
"""
from alembic import op
import sqlalchemy as sa


revision = 'm0b1c2d3e4f5'
down_revision = 'l9a0b1c2d3e4'
branch_labels = None
depends_on = None


_INDEXES = (
    ('payments', 'ix_payments_card_created'),
    ('transactions', 'ix_transactions_card_created'),
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, name in _INDEXES:
        if name not in {index['name'] for index in inspector.get_indexes(table)}:
            op.create_index(name, table, ['card_id', 'created_at'])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, name in reversed(_INDEXES):
        if name in {index['name'] for index in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
