"""store fractional renewal days and volume

Revision ID: a8b3c4d5e6f7
Revises: f7a2b3c4d5e6
Create Date: 2026-08-03 13:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a8b3c4d5e6f7'
down_revision = 'f7a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('transactions') as batch_op:
        batch_op.alter_column(
            'volume_gb', existing_type=sa.Integer(), type_=sa.Float(), existing_nullable=True
        )
        batch_op.alter_column(
            'days', existing_type=sa.Integer(), type_=sa.Float(), existing_nullable=True
        )


def downgrade():
    with op.batch_alter_table('transactions') as batch_op:
        batch_op.alter_column(
            'days', existing_type=sa.Float(), type_=sa.Integer(), existing_nullable=True
        )
        batch_op.alter_column(
            'volume_gb', existing_type=sa.Float(), type_=sa.Integer(), existing_nullable=True
        )
