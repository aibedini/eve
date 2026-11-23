"""Create machine integration tables."""
from alembic import op
import sqlalchemy as sa

revision = '45170a8ab431'
down_revision = '3a48bd75e9c1'
branch_labels = None
depends_on = None

INDEXES = {
    'ix_machine_customers_cursor': 'customer_accounts (updated_at, id)',
    'ix_machine_service_email': 'service_ownerships (lower(client_email_snapshot)) WHERE revoked_at IS NULL',
    'ix_machine_verified_renewals': 'renewal_events (server_id, client_uuid, renewed_at) WHERE verified',
}


def upgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if 'machine_api_keys' not in existing:
        op.create_table(
            'machine_api_keys',
            sa.Column('id', sa.String(32), primary_key=True),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('key_hash', sa.String(64), nullable=False),
            sa.Column('scopes', sa.JSON(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('last_used_at', sa.DateTime()),
            sa.Column('revoked_at', sa.DateTime()),
            sa.Column('created_by', sa.Integer(), nullable=False),
            sa.Column('rate_window', sa.Integer(), nullable=False),
            sa.Column('rate_count', sa.Integer(), nullable=False))
    if 'machine_proposals' not in existing:
        op.create_table(
            'machine_proposals',
            sa.Column('id', sa.String(36), primary_key=True),
            sa.Column('key_id', sa.String(32), nullable=False),
            sa.Column('payload', sa.Text(), nullable=False),
            sa.Column('status', sa.String(12), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('decided_at', sa.DateTime()),
            sa.Column('decided_by', sa.Integer()))
    if 'machine_phone_lookups' not in existing:
        op.create_table(
            'machine_phone_lookups',
            sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customer_accounts.id', ondelete='CASCADE'), primary_key=True),
            sa.Column('phone_hash', sa.String(64), nullable=False))
        op.create_index('ix_machine_phone_lookups_phone_hash', 'machine_phone_lookups', ['phone_hash'])
    if 'machine_events' not in existing:
        op.create_table(
            'machine_events',
            sa.Column('id', sa.String(40), primary_key=True),
            sa.Column('body', sa.Text(), nullable=False),
            sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False),
            sa.Column('delivered_at', sa.DateTime()))
        op.create_index('ix_machine_events_next_attempt_at', 'machine_events', ['next_attempt_at'])
        op.create_index('ix_machine_events_delivered_at', 'machine_events', ['delivered_at'])
    for name, expression in INDEXES.items():
        op.execute(sa.text(f'CREATE INDEX IF NOT EXISTS {name} ON {expression}'))


def downgrade():
    for name in INDEXES:
        op.execute(sa.text(f'DROP INDEX IF EXISTS {name}'))
    for table in ('machine_events', 'machine_phone_lookups', 'machine_proposals', 'machine_api_keys'):
        op.drop_table(table)
