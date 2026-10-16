"""Add device display mode and WHO warning threshold configuration."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "display_configuration",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column(
            "warning_threshold",
            sa.String(30),
            nullable=False,
            server_default="who_2021_24h",
        ),
    )
    op.add_column(
        "devices",
        sa.Column(
            "display_mode",
            sa.String(20),
            nullable=False,
            server_default="manual",
        ),
    )
    op.add_column("devices", sa.Column("warning_threshold", sa.String(30), nullable=True))


def downgrade():
    op.drop_column("devices", "warning_threshold")
    op.drop_column("devices", "display_mode")
    op.drop_table("display_configuration")
