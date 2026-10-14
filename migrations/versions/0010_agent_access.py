"""Agent access tokens, planner run records and rule revision authors."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("rule_revisions") as batch:
        batch.add_column(sa.Column("author", sa.String(120)))
    op.create_table(
        "access_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.Float()),
        sa.Column("expires_at", sa.Float()),
        sa.Column("revoked_at", sa.Float()),
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(36),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_kind", sa.String(40)),
        sa.Column("revision_created", sa.Integer()),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float()),
    )
    op.create_index("ix_agent_runs_device_id", "agent_runs", ["device_id"])
    op.create_index("ix_agent_runs_started_at", "agent_runs", ["started_at"])


def downgrade():
    op.drop_table("agent_runs")
    op.drop_table("access_tokens")
    with op.batch_alter_table("rule_revisions") as batch:
        batch.drop_column("author")
