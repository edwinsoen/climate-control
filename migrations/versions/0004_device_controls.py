"""Store supported device controls and shared command cooldowns."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("devices", sa.Column("controls", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column(
        "accounts", sa.Column("next_control_at", sa.Float(), nullable=False, server_default="0")
    )


def downgrade():
    op.drop_column("accounts", "next_control_at")
    op.drop_column("devices", "controls")
