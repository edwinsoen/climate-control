"""User-selected household outdoor location and shared request scheduling."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "outdoor_configuration",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("location_id", sa.Integer()),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("discovery", sa.JSON(), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("next_request_at", sa.Float(), nullable=False),
        sa.Column("retry_at", sa.Float(), nullable=False),
    )
    op.bulk_insert(
        table,
        [
            {
                "id": 1,
                "discovery": {},
                "lease_until": 0,
                "next_request_at": 0,
                "retry_at": 0,
            }
        ],
    )


def downgrade():
    op.drop_table("outdoor_configuration")
