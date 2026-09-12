"""Add the per-server allow_insecure transport opt-in.

Every existing server keeps full verification: the column is added NOT NULL with
a server default of false, so the migration backfills current rows with false.

Revision ID: c9d8e7f6a5b4
Revises: a1b2c3d4e5f6
Create Date: 2026-09-12 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c9d8e7f6a5b4'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('servers') as batch_op:
        batch_op.add_column(sa.Column('allow_insecure', sa.Boolean(),
                                      nullable=False,
                                      server_default=sa.text('false')))


def downgrade():
    with op.batch_alter_table('servers') as batch_op:
        batch_op.drop_column('allow_insecure')
