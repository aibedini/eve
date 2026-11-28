"""Index the service inventory and SMS contact hashes for the machine API."""
from alembic import op
import sqlalchemy as sa

revision = '26713b0b96e2'
down_revision = '45170a8ab431'
branch_labels = None
depends_on = None


def upgrade():
    # The EVE migration runner calls create_all before Alembic.
    if sa.inspect(op.get_bind()).has_table('machine_service_index'):
        return
    op.create_table(
        'machine_service_index',
        sa.Column('server_id', sa.Integer(), sa.ForeignKey('servers.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('client_uuid', sa.String(100), primary_key=True),
        sa.Column('legacy_id', sa.Integer()),
        sa.Column('email_hash', sa.String(64)),
        sa.Column('phone_hash', sa.String(64)),
        sa.Column('contact_pepper', sa.String(64)),
        sa.Column('snapshot_revision', sa.String(64)),
        sa.Column('reseller_id', sa.Integer()),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_machine_service_cursor', 'machine_service_index', ['updated_at', 'server_id', 'client_uuid'])
    for column in ('legacy_id', 'email_hash', 'phone_hash'):
        op.create_index(f'ix_machine_service_index_{column}', 'machine_service_index', [column])


def downgrade():
    op.drop_table('machine_service_index')
