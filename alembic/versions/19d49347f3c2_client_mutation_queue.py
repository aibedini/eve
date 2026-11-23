"""Add the durable client mutation queue."""
from alembic import op
import sqlalchemy as sa

revision = '19d49347f3c2'
down_revision = '8ae6d12c4b97'
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table('client_mutation_jobs'):
        return  # Fresh installations run create_all before Alembic.
    op.create_table(
        'client_mutation_jobs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('public_id', sa.String(64), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=False),
        sa.Column('admin_created_at', sa.DateTime()),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(64), nullable=False),
        sa.Column('target_scope', sa.String(64), nullable=False),
        sa.Column('action', sa.String(12), nullable=False),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('state', sa.String(12), nullable=False),
        sa.Column('ready_at', sa.DateTime(), nullable=False),
        sa.Column('request_data', sa.Text()),
        sa.Column('result_data', sa.Text()),
        sa.UniqueConstraint('admin_id', 'public_id', name='uq_client_mutation_job_admin_public'),
    )
    op.create_index('ix_client_mutation_jobs_ready', 'client_mutation_jobs', ['state', 'ready_at', 'id'])
    op.create_index('ix_client_mutation_jobs_server', 'client_mutation_jobs', ['server_id', 'state', 'id'])


def downgrade():
    op.drop_table('client_mutation_jobs')
