"""Record recent app setting commands to tell them apart from changes made outside the app."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "devices",
        sa.Column("control_intents", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade():
    op.drop_column("devices", "control_intents")
