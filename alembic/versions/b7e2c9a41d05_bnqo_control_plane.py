"""bnqo control-plane tables

Revision ID: b7e2c9a41d05
Revises: a3f9c2d71e84
Create Date: 2026-07-29 03:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e2c9a41d05'
down_revision = 'a3f9c2d71e84'
branch_labels = None
depends_on = None

# Self-healing: panel/migrate.py runs `db.create_all()` before the Alembic
# upgrade, so databases that booted the app once already have these tables
# while their ledger still sits at an older revision. Skip any table/index
# that already exists instead of failing with DuplicateTable (same pattern
# as a3f9c2d71e84_package_visibility_flags).
_TABLES = (
    'bnqo_agents',
    'bnqo_enroll_tokens',
    'bnqo_links',
    'bnqo_measurements',
    'bnqo_service_probes',
    'bnqo_routes',
    'bnqo_route_hops',
    'bnqo_incidents',
    'bnqo_rollups_hourly',
    'bnqo_jobs',
)


def _existing_tables() -> set:
    bind = op.get_bind()
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if 'bnqo_agents' not in existing:
        op.create_table('bnqo_agents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('address', sa.String(length=64), nullable=True),
        sa.Column('port', sa.Integer(), nullable=True),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('pubkey', sa.String(length=64), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=True),
        sa.Column('version', sa.String(length=32), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('last_ip', sa.String(length=64), nullable=True),
        sa.Column('config_version', sa.Integer(), nullable=True),
        sa.Column('last_seq', sa.Integer(), nullable=True),
        sa.Column('host_json', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
        )

    if 'bnqo_enroll_tokens' not in existing:
        op.create_table('bnqo_enroll_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('used_by_agent_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token')
        )

    if 'bnqo_links' not in existing:
        op.create_table('bnqo_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('agent_a_id', sa.Integer(), nullable=False),
        sa.Column('agent_b_id', sa.Integer(), nullable=False),
        sa.Column('profile_json', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.Column('status_json', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=True),
        sa.Column('last_data_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['agent_a_id'], ['bnqo_agents.id'], ),
        sa.ForeignKeyConstraint(['agent_b_id'], ['bnqo_agents.id'], ),
        sa.PrimaryKeyConstraint('id')
        )

    if 'bnqo_measurements' not in existing:
        op.create_table('bnqo_measurements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('link_id', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('source', sa.String(length=8), nullable=False),
        sa.Column('window_start', sa.DateTime(), nullable=False),
        sa.Column('window_end', sa.DateTime(), nullable=False),
        sa.Column('sent', sa.Integer(), nullable=True),
        sa.Column('received', sa.Integer(), nullable=True),
        sa.Column('loss_pct', sa.Float(), nullable=True),
        sa.Column('rtt_min_ms', sa.Float(), nullable=True),
        sa.Column('rtt_avg_ms', sa.Float(), nullable=True),
        sa.Column('rtt_p95_ms', sa.Float(), nullable=True),
        sa.Column('rtt_max_ms', sa.Float(), nullable=True),
        sa.Column('owd_ms', sa.Float(), nullable=True),
        sa.Column('clock_quality', sa.String(length=16), nullable=True),
        sa.Column('jitter_ms', sa.Float(), nullable=True),
        sa.Column('reordered', sa.Integer(), nullable=True),
        sa.Column('duplicated', sa.Integer(), nullable=True),
        sa.Column('corrupted', sa.Integer(), nullable=True),
        sa.Column('burst_max', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['link_id'], ['bnqo_links.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_bnqo_measurements_created_at', 'bnqo_measurements', ['created_at'])
        op.create_index('ix_bnqo_measurements_link_id', 'bnqo_measurements', ['link_id'])
        op.create_index('ix_bnqo_measurements_link_window', 'bnqo_measurements', ['link_id', 'window_start'])

    if 'bnqo_service_probes' not in existing:
        op.create_table('bnqo_service_probes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('link_id', sa.Integer(), nullable=False),
        sa.Column('target_name', sa.String(length=64), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=True),
        sa.Column('tcp_ms', sa.Float(), nullable=True),
        sa.Column('tls_ms', sa.Float(), nullable=True),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('error_class', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['link_id'], ['bnqo_links.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_bnqo_service_probes_created_at', 'bnqo_service_probes', ['created_at'])
        op.create_index('ix_bnqo_service_probes_link_id', 'bnqo_service_probes', ['link_id'])

    if 'bnqo_routes' not in existing:
        op.create_table('bnqo_routes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('link_id', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('route_hash', sa.String(length=16), nullable=True),
        sa.Column('destination_reached', sa.Boolean(), nullable=True),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['link_id'], ['bnqo_links.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_bnqo_routes_created_at', 'bnqo_routes', ['created_at'])
        op.create_index('ix_bnqo_routes_link_id', 'bnqo_routes', ['link_id'])
        op.create_index('ix_bnqo_routes_route_hash', 'bnqo_routes', ['route_hash'])

    if 'bnqo_route_hops' not in existing:
        op.create_table('bnqo_route_hops',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('route_id', sa.Integer(), nullable=False),
        sa.Column('hop_number', sa.Integer(), nullable=False),
        sa.Column('address', sa.String(length=64), nullable=True),
        sa.Column('loss_pct', sa.Float(), nullable=True),
        sa.Column('rtt_avg_ms', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['route_id'], ['bnqo_routes.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_bnqo_route_hops_route_id', 'bnqo_route_hops', ['route_id'])

    if 'bnqo_incidents' not in existing:
        op.create_table('bnqo_incidents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('link_id', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=True),
        sa.Column('kind', sa.String(length=48), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=True),
        sa.Column('evidence_json', sa.Text(), nullable=True),
        sa.Column('opened_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['link_id'], ['bnqo_links.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index('ix_bnqo_incidents_link_id', 'bnqo_incidents', ['link_id'])
        op.create_index('ix_bnqo_incidents_opened_at', 'bnqo_incidents', ['opened_at'])

    if 'bnqo_rollups_hourly' not in existing:
        op.create_table('bnqo_rollups_hourly',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('link_id', sa.Integer(), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('hour', sa.DateTime(), nullable=False),
        sa.Column('samples', sa.Integer(), nullable=True),
        sa.Column('loss_avg', sa.Float(), nullable=True),
        sa.Column('rtt_p95', sa.Float(), nullable=True),
        sa.Column('jitter_avg', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['link_id'], ['bnqo_links.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('link_id', 'direction', 'hour', name='uq_bnqo_rollup_link_dir_hour')
        )
        op.create_index('ix_bnqo_rollups_hourly_hour', 'bnqo_rollups_hourly', ['hour'])
        op.create_index('ix_bnqo_rollups_hourly_link_id', 'bnqo_rollups_hourly', ['link_id'])

    if 'bnqo_jobs' not in existing:
        op.create_table('bnqo_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('job_id', sa.String(length=40), nullable=False),
        sa.Column('agent_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=32), nullable=False),
        sa.Column('params_json', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('config_version', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=True),
        sa.Column('error_class', sa.String(length=64), nullable=True),
        sa.Column('result_received_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['agent_id'], ['bnqo_agents.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id')
        )
        op.create_index('ix_bnqo_jobs_agent_id', 'bnqo_jobs', ['agent_id'])


def downgrade() -> None:
    existing = _existing_tables()
    for table in reversed(_TABLES):
        if table in existing:
            op.drop_table(table)
