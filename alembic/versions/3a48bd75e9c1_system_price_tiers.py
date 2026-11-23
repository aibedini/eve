"""Let a price tier apply to system accounts and selected resellers."""
from alembic import op
import sqlalchemy as sa

revision = '3a48bd75e9c1'
down_revision = '19d49347f3c2'
branch_labels = None
depends_on = None


def upgrade():
    columns = sa.inspect(op.get_bind()).get_columns('price_tiers')
    if not any(column['name'] == 'applies_to_system' for column in columns):
        op.add_column('price_tiers', sa.Column(
            'applies_to_system', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('price_tiers') as table:
        table.drop_column('applies_to_system')
