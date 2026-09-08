"""Protect custom subscription tokens and connection URIs at rest.

Revision ID: 26d1e2f3a4b5
Revises: 15c0d1e2f3a4
Create Date: 2026-09-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '26d1e2f3a4b5'
down_revision = '15c0d1e2f3a4'
branch_labels = None
depends_on = None


def _columns(inspector, table):
    return {column['name'] for column in inspector.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if 'custom_subscriptions' in tables:
        columns = _columns(inspector, 'custom_subscriptions')
        with op.batch_alter_table('custom_subscriptions') as batch_op:
            batch_op.alter_column(
                'token', existing_type=sa.String(length=48), type_=sa.Text(),
                existing_nullable=False,
            )
            if 'token_hash' not in columns:
                batch_op.add_column(sa.Column('token_hash', sa.String(length=64), nullable=True))
        indexes = {index['name'] for index in sa.inspect(bind).get_indexes('custom_subscriptions')}
        with op.batch_alter_table('custom_subscriptions') as batch_op:
            if 'ix_custom_subscriptions_token' in indexes:
                batch_op.drop_index('ix_custom_subscriptions_token')
            if 'ix_custom_subscriptions_token_hash' not in indexes:
                batch_op.create_index(
                    'ix_custom_subscriptions_token_hash', ['token_hash'], unique=True,
                )
    if 'custom_subscription_configs' in tables:
        columns = _columns(sa.inspect(bind), 'custom_subscription_configs')
        constraints = {
            item['name'] for item in sa.inspect(bind).get_unique_constraints(
                'custom_subscription_configs'
            )
        }
        with op.batch_alter_table('custom_subscription_configs') as batch_op:
            if 'uri_hash' not in columns:
                batch_op.add_column(sa.Column('uri_hash', sa.String(length=64), nullable=True))
            if 'uq_custom_subscription_uri' in constraints:
                batch_op.drop_constraint('uq_custom_subscription_uri', type_='unique')
            if 'uq_custom_subscription_uri_hash' not in constraints:
                batch_op.create_unique_constraint(
                    'uq_custom_subscription_uri_hash', ['subscription_id', 'uri_hash'],
                )


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if 'custom_subscription_configs' in tables:
        with op.batch_alter_table('custom_subscription_configs') as batch_op:
            batch_op.drop_constraint('uq_custom_subscription_uri_hash', type_='unique')
            batch_op.create_unique_constraint(
                'uq_custom_subscription_uri', ['subscription_id', 'uri'],
            )
            batch_op.drop_column('uri_hash')
    if 'custom_subscriptions' in tables:
        with op.batch_alter_table('custom_subscriptions') as batch_op:
            batch_op.drop_index('ix_custom_subscriptions_token_hash')
            batch_op.drop_column('token_hash')
            batch_op.alter_column(
                'token', existing_type=sa.Text(), type_=sa.String(length=48),
                existing_nullable=False,
            )
            batch_op.create_index('ix_custom_subscriptions_token', ['token'], unique=True)
