"""Persist the provider selected for each account."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # The default backfills existing accounts, all created with the sole VeSync adapter.
    # Adding directly preserves child snapshots without rebuilding SQLite's parent table.
    op.add_column(
        "accounts", sa.Column("provider", sa.String(50), nullable=False, server_default="vesync")
    )


def downgrade():
    op.drop_column("accounts", "provider")
