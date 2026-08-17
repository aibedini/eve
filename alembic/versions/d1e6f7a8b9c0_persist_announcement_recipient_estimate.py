"""persist announcement recipient estimates

Revision ID: d1e6f7a8b9c0
Revises: c0d5e6f7a8b9
Create Date: 2026-08-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd1e6f7a8b9c0'
down_revision = 'c0d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('announcements') as batch_op:
        batch_op.add_column(sa.Column('recipient_estimate', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('recipient_estimated_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('announcements') as batch_op:
        batch_op.drop_column('recipient_estimated_at')
        batch_op.drop_column('recipient_estimate')
