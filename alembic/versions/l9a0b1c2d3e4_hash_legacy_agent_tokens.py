"""Hash agent bearer tokens still stored in plaintext.

Agent auth used to fall back to a plaintext lookup and upgrade the row on the
agent's next request, so an agent that never reconnected kept a usable token in
the database. This hashes every remaining plaintext token once, so the
plaintext fallback can be removed. Idempotent: hashed values are skipped.

Revision ID: l9a0b1c2d3e4
Revises: k8f9a0b1c2d3
"""
from alembic import op
import sqlalchemy as sa

revision = 'l9a0b1c2d3e4'
down_revision = 'k8f9a0b1c2d3'
branch_labels = None
depends_on = None

# table -> hash purpose (must match the lookups in panel/routes/pulse.py and bnqo.py)
_TOKEN_TABLES = {
    'pulse_agents': 'pulse-agent',
    'bnqo_agents': 'bnqo-agent',
    'bnqo_enroll_tokens': 'bnqo-enroll',
}


def upgrade():
    from panel.security.secrets import hash_bearer_token, is_hashed_bearer_token

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table, purpose in _TOKEN_TABLES.items():
        if table not in tables:
            continue
        rows = bind.execute(sa.text(f'SELECT id, token FROM {table}')).fetchall()
        for row_id, token in rows:
            if not token or is_hashed_bearer_token(token):
                continue
            bind.execute(sa.text(f'UPDATE {table} SET token = :token WHERE id = :id'),
                         {'token': hash_bearer_token(token, purpose), 'id': row_id})


def downgrade():
    # One-way: the plaintext tokens cannot be recovered from their digests.
    pass
