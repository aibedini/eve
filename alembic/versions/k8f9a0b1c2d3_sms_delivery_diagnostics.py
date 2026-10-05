"""Retain optional, privacy-safe GMweb carrier delivery diagnostics.

Revision ID: k8f9a0b1c2d3
Revises: j7e8f9a0b1c2
"""
from alembic import op
import sqlalchemy as sa

revision = 'k8f9a0b1c2d3'
down_revision = 'j7e8f9a0b1c2'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column['name'] for column in inspector.get_columns('sms_gateway_events')}
    if 'diagnostics_json' not in columns:
        op.add_column('sms_gateway_events', sa.Column('diagnostics_json', sa.Text(), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column['name'] for column in inspector.get_columns('sms_gateway_events')}
    if 'diagnostics_json' in columns:
        op.drop_column('sms_gateway_events', 'diagnostics_json')
