"""Durable SMS scan runs and candidate decisions.

Revision ID: g4b5c6d7e8f9
Revises: f1d4a6b8c9e2
"""
from alembic import op
import sqlalchemy as sa

revision = 'g4b5c6d7e8f9'
down_revision = 'f1d4a6b8c9e2'
branch_labels = None
depends_on = None


def upgrade():
    # The migration runner calls db.create_all() before Alembic. On existing
    # installations the model may therefore have created both tables already.
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if 'sms_scan_runs' not in existing_tables:
        _create_runs_table()
    if 'sms_scan_decisions' not in existing_tables:
        _create_decisions_table()


def _create_runs_table():
    op.create_table(
        'sms_scan_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.String(64), nullable=False, unique=True),
        sa.Column('triggered_by', sa.String(24), nullable=False, server_default='manual'),
        sa.Column('selected_states', sa.Text()), sa.Column('priority_order', sa.Text()),
        sa.Column('snapshot_revision', sa.String(128)),
        sa.Column('started_by_admin_id', sa.Integer()),
        sa.Column('started_at', sa.DateTime(), nullable=False), sa.Column('finished_at', sa.DateTime()),
        sa.Column('status', sa.String(24), nullable=False, server_default='running'),
        *[sa.Column(n, sa.Integer(), nullable=False, server_default='0') for n in (
            'scanned_count','matched_count','eligible_count','submitted_count','confirmed_count',
            'inflight_count','deferred_count','suppressed_count','failed_count','cancelled_count','audit_gap_count')],
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    for name, cols in [('ix_sms_scan_runs_started_at', ['started_at']), ('ix_sms_scan_runs_status', ['status'])]:
        op.create_index(name, 'sms_scan_runs', cols)


def _create_decisions_table():
    op.create_table(
        'sms_scan_decisions',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('run_id', sa.String(64), nullable=False),
        sa.Column('service_key', sa.String(255), nullable=False), sa.Column('server_id', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('server_name', sa.String(255)), sa.Column('client_uuid', sa.String(100)),
        sa.Column('client_email', sa.String(255), nullable=False), sa.Column('state', sa.String(32), nullable=False),
        sa.Column('state_version', sa.Integer()), sa.Column('lifecycle_generation', sa.Integer()), sa.Column('candidate_observed_at', sa.DateTime()),
        sa.Column('recipient_masked', sa.String(32)), sa.Column('disposition', sa.String(32), nullable=False),
        sa.Column('reason_code', sa.String(64)), sa.Column('reason_detail_safe', sa.String(255)), sa.Column('next_attempt_at', sa.DateTime()),
        sa.Column('notification_event_id', sa.String(160)), sa.Column('sms_send_log_id', sa.Integer()),
        sa.Column('gateway_request_id', sa.String(128)), sa.Column('gateway_job_id', sa.String(64)),
        sa.Column('decision_at', sa.DateTime(), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('run_id', 'service_key', name='uq_sms_decision_run_service'),
    )
    for name, cols in [('ix_sms_decisions_run_id',['run_id']),('ix_sms_decisions_service_key',['service_key']),('ix_sms_decisions_client_email',['client_email']),('ix_sms_decisions_state',['state']),('ix_sms_decisions_disposition',['disposition']),('ix_sms_decisions_reason_code',['reason_code']),('ix_sms_decisions_gateway_request',['gateway_request_id']),('ix_sms_decisions_gateway_job',['gateway_job_id']),('ix_sms_decisions_created',['created_at'])]:
        op.create_index(name, 'sms_scan_decisions', cols)


def downgrade():
    op.drop_table('sms_scan_decisions')
    op.drop_table('sms_scan_runs')
