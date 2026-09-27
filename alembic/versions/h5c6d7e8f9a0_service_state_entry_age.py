"""Record trustworthy service state-entry time separately from observation time.

Revision ID: h5c6d7e8f9a0
Revises: g4b5c6d7e8f9
"""
from alembic import op
import sqlalchemy as sa


revision = 'h5c6d7e8f9a0'
down_revision = 'g4b5c6d7e8f9'
branch_labels = None
depends_on = None


def upgrade():
    columns = {column['name'] for column in
               sa.inspect(op.get_bind()).get_columns('service_observed_states')}
    if 'state_entered_at' not in columns:
        op.add_column('service_observed_states',
                      sa.Column('state_entered_at', sa.DateTime(), nullable=True))
    if 'state_entered_at_quality' not in columns:
        op.add_column('service_observed_states',
                      sa.Column('state_entered_at_quality', sa.String(length=32), nullable=True))


def downgrade():
    op.drop_column('service_observed_states', 'state_entered_at_quality')
    op.drop_column('service_observed_states', 'state_entered_at')
