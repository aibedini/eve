"""Support roles, activity, budgets and payroll.

Revision ID: 8ae6d12c4b97
Revises: 6c2b98d107ef
"""

from alembic import op
import sqlalchemy as sa

revision = '8ae6d12c4b97'
down_revision = '6c2b98d107ef'
branch_labels = None
depends_on = None

TABLES = ('support_roles', 'support_operators', 'support_contracts', 'support_activity',
          'support_budgets', 'support_payroll', 'support_chats', 'support_chat_messages')


def _create(table, *schema, indexes=()):
    # The application migration runner may have already adopted the current schema.
    if sa.inspect(op.get_bind()).has_table(table):
        return
    op.create_table(table, *schema)
    for indexed in indexes:
        op.create_index(f'ix_{table}_{indexed}', table, [indexed])


def upgrade():
    _create('support_roles',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(80), nullable=False, unique=True),
            sa.Column('permissions', sa.JSON(), nullable=False))
    _create('support_operators',
            sa.Column('admin_id', sa.Integer(), sa.ForeignKey('admins.id', ondelete='CASCADE'), primary_key=True),
            sa.Column('owner_id', sa.Integer(), sa.ForeignKey('admins.id')),
            sa.Column('role_id', sa.Integer(), sa.ForeignKey('support_roles.id')),
            sa.Column('overrides', sa.JSON(), nullable=False),
            sa.Column('locations', sa.JSON(), nullable=False),
            sa.Column('account_filter', sa.JSON(), nullable=False),
            sa.Column('sms_limit', sa.Integer(), nullable=False),
            sa.Column('free_limit', sa.Integer(), nullable=False),
            sa.Column('free_max_gb', sa.Integer(), nullable=False),
            indexes=('owner_id',))
    _create('support_contracts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('operator_id', sa.Integer(), nullable=False),
            sa.Column('owner_id', sa.Integer()),
            sa.Column('terms', sa.JSON(), nullable=False),
            sa.Column('starts_at', sa.DateTime(), nullable=False),
            indexes=('operator_id',))
    _create('support_activity',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('operator_id', sa.Integer(), nullable=False),
            sa.Column('owner_id', sa.Integer()),
            sa.Column('kind', sa.String(32), nullable=False),
            sa.Column('reference', sa.String(196), nullable=False, unique=True),
            sa.Column('customer_id', sa.Integer()),
            sa.Column('server_id', sa.Integer()),
            sa.Column('client_uuid', sa.String(100)),
            sa.Column('gateway_request_id', sa.String(120)),
            sa.Column('state', sa.String(24), nullable=False),
            sa.Column('details', sa.JSON(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            indexes=('operator_id', 'owner_id', 'created_at', 'gateway_request_id'))
    _create('support_budgets',
            sa.Column('scope', sa.String(64), primary_key=True),
            sa.Column('day', sa.Date(), primary_key=True),
            sa.Column('used', sa.Integer(), nullable=False))
    _create('support_payroll',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('operator_id', sa.Integer(), nullable=False),
            sa.Column('owner_id', sa.Integer()),
            sa.Column('period', sa.Date(), nullable=False),
            sa.Column('amount', sa.BigInteger(), nullable=False),
            sa.Column('breakdown', sa.JSON(), nullable=False),
            sa.Column('approved_by', sa.Integer(), nullable=False),
            sa.Column('approved_at', sa.DateTime(), nullable=False),
            sa.Column('paid_at', sa.DateTime()),
            sa.Column('paid_by', sa.Integer()),
            sa.UniqueConstraint('operator_id', 'period', name='uq_support_payroll_period'),
            indexes=('operator_id', 'owner_id'))
    _create('support_chats',
            sa.Column('service_id', sa.Integer(), sa.ForeignKey('service_ownerships.id', ondelete='CASCADE'),
                      primary_key=True),
            sa.Column('assigned_to', sa.Integer()),
            sa.Column('open', sa.Boolean(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            indexes=('updated_at',))
    _create('support_chat_messages',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('service_id', sa.Integer(), sa.ForeignKey('support_chats.service_id', ondelete='CASCADE'),
                      nullable=False),
            sa.Column('operator_id', sa.Integer()),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            indexes=('service_id',))


def downgrade():
    for table in reversed(TABLES):
        op.drop_table(table)
