"""announcement audience filters and delivery segment estimates

Revision ID: b9c4d5e6f7a8
Revises: a8b3c4d5e6f7
Create Date: 2026-08-15 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b9c4d5e6f7a8'
down_revision = 'a8b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('announcements') as batch_op:
        batch_op.add_column(sa.Column(
            'audience_owner_types', sa.Text(), nullable=False,
            server_default='["system","unowned"]'))
        batch_op.add_column(sa.Column(
            'audience_statuses', sa.Text(), nullable=False,
            server_default='["other","expired","volume_ended","expiring_soon","volume_low"]'))
    with op.batch_alter_table('announcement_deliveries') as batch_op:
        batch_op.add_column(sa.Column(
            'segment_count', sa.Integer(), nullable=False, server_default='1'))


def downgrade():
    with op.batch_alter_table('announcement_deliveries') as batch_op:
        batch_op.drop_column('segment_count')
    with op.batch_alter_table('announcements') as batch_op:
        batch_op.drop_column('audience_statuses')
        batch_op.drop_column('audience_owner_types')
