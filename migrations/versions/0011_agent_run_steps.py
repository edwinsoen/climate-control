"""Record what the planner did during each run."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("steps", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("agent_runs", "steps")
