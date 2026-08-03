"""announcement outbound campaigns

Revision ID: f7a2b3c4d5e6
Revises: e6f1a2b3c4d5
Create Date: 2026-08-03 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f7a2b3c4d5e6'
down_revision = 'e6f1a2b3c4d5'
branch_labels = None
depends_on = None


def _columns(table_name):
    return {col['name'] for col in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade():
    columns = _columns('announcements')
    additions = (
        ('channel', sa.Column('channel', sa.String(24), nullable=False, server_default='subscription')),
        ('delivery_mode', sa.Column('delivery_mode', sa.String(24), nullable=False, server_default='all')),
        ('daily_limit', sa.Column('daily_limit', sa.Integer(), nullable=True)),
        ('status', sa.Column('status', sa.String(24), nullable=False, server_default='draft')),
        ('total_count', sa.Column('total_count', sa.Integer(), nullable=False, server_default='0')),
        ('sent_count', sa.Column('sent_count', sa.Integer(), nullable=False, server_default='0')),
        ('failed_count', sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0')),
        ('skipped_count', sa.Column('skipped_count', sa.Integer(), nullable=False, server_default='0')),
        ('started_at', sa.Column('started_at', sa.DateTime(), nullable=True)),
        ('finished_at', sa.Column('finished_at', sa.DateTime(), nullable=True)),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column('announcements', column)
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'announcement_deliveries' not in inspector.get_table_names():
        op.create_table(
            'announcement_deliveries',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('announcement_id', sa.Integer(), sa.ForeignKey('announcements.id', ondelete='CASCADE'), nullable=False),
            sa.Column('recipient_key', sa.String(160), nullable=False),
            sa.Column('recipient', sa.String(160), nullable=False),
            sa.Column('email', sa.String(255), nullable=True),
            sa.Column('server_id', sa.Integer(), nullable=True),
            sa.Column('inbound_id', sa.Integer(), nullable=True),
            sa.Column('bot_instance_id', sa.Integer(), sa.ForeignKey('telegram_bot_instances.id'), nullable=True),
            sa.Column('context_json', sa.Text(), nullable=False, server_default='{}'),
            sa.Column('status', sa.String(24), nullable=False, server_default='pending'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_error', sa.String(500), nullable=True),
            sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
            sa.Column('processed_at', sa.DateTime(), nullable=True),
            sa.Column('sent_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.UniqueConstraint('announcement_id', 'recipient_key', name='uq_announcement_delivery_recipient'),
        )
        op.create_index('ix_announcement_deliveries_announcement_id', 'announcement_deliveries', ['announcement_id'])
        op.create_index('ix_announcement_deliveries_status', 'announcement_deliveries', ['status'])
        op.create_index('ix_announcement_deliveries_next_attempt_at', 'announcement_deliveries', ['next_attempt_at'])
        op.create_index('ix_announcement_deliveries_processed_at', 'announcement_deliveries', ['processed_at'])
    announcement_indexes = {index['name'] for index in sa.inspect(op.get_bind()).get_indexes('announcements')}
    if 'ix_announcements_channel' not in announcement_indexes:
        op.create_index('ix_announcements_channel', 'announcements', ['channel'])
    if 'ix_announcements_status' not in announcement_indexes:
        op.create_index('ix_announcements_status', 'announcements', ['status'])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if 'announcement_deliveries' in inspector.get_table_names():
        op.drop_table('announcement_deliveries')
    columns = _columns('announcements')
    for name in ('finished_at', 'started_at', 'skipped_count', 'failed_count', 'sent_count',
                 'total_count', 'status', 'daily_limit', 'delivery_mode', 'channel'):
        if name in columns:
            op.drop_column('announcements', name)
