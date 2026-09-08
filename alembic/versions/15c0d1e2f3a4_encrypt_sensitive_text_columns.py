"""Widen sensitive columns for versioned encrypted envelopes.

Revision ID: 15c0d1e2f3a4
Revises: 04b9c0d1e2f3
Create Date: 2026-09-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '15c0d1e2f3a4'
down_revision = '04b9c0d1e2f3'
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if 'bank_cards' in tables:
        with op.batch_alter_table('bank_cards') as batch_op:
            batch_op.alter_column('card_number', existing_type=sa.String(length=32), type_=sa.Text(), existing_nullable=True)
            batch_op.alter_column('iban', existing_type=sa.String(length=34), type_=sa.Text(), existing_nullable=True)
            batch_op.alter_column('account_number', existing_type=sa.String(length=64), type_=sa.Text(), existing_nullable=True)
    if 'payments' in tables:
        with op.batch_alter_table('payments') as batch_op:
            batch_op.alter_column('sender_card', existing_type=sa.String(length=32), type_=sa.Text(), existing_nullable=True)
    if 'transactions' in tables:
        with op.batch_alter_table('transactions') as batch_op:
            batch_op.alter_column('sender_card', existing_type=sa.String(length=32), type_=sa.Text(), existing_nullable=True)


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if 'transactions' in tables:
        with op.batch_alter_table('transactions') as batch_op:
            batch_op.alter_column('sender_card', existing_type=sa.Text(), type_=sa.String(length=32), existing_nullable=True)
    if 'payments' in tables:
        with op.batch_alter_table('payments') as batch_op:
            batch_op.alter_column('sender_card', existing_type=sa.Text(), type_=sa.String(length=32), existing_nullable=True)
    if 'bank_cards' in tables:
        with op.batch_alter_table('bank_cards') as batch_op:
            batch_op.alter_column('account_number', existing_type=sa.Text(), type_=sa.String(length=64), existing_nullable=True)
            batch_op.alter_column('iban', existing_type=sa.Text(), type_=sa.String(length=34), existing_nullable=True)
            batch_op.alter_column('card_number', existing_type=sa.Text(), type_=sa.String(length=32), existing_nullable=True)
