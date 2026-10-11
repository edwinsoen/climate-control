"""Durable rule execution, revisions, decisions and agent inboxes."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def device_key(primary=False):
    return sa.Column(
        "device_id",
        sa.String(36),
        sa.ForeignKey("devices.id", ondelete="CASCADE"),
        primary_key=primary,
        nullable=False,
    )


def upgrade():
    op.create_table(
        "device_automation",
        device_key(True),
        sa.Column("owner", sa.String(10), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("paused_until", sa.Float(), nullable=False),
        sa.Column("pause_seconds", sa.Integer(), nullable=False),
        sa.Column("runtime", sa.JSON(), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("next_review_at", sa.Float(), nullable=False),
        sa.Column("last_review_request_at", sa.Float(), nullable=False),
    )
    op.execute(
        sa.text("""INSERT INTO device_automation
        (device_id, owner, enabled, generation, revision, paused_until, pause_seconds,
         runtime, lease_until, next_review_at, last_review_request_at)
        SELECT id, 'manual', false, 0, 0, 0, 3600, '{}', 0, 0, 0 FROM devices""")
    )
    op.create_table(
        "rule_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        device_key(),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(10), nullable=False),
        sa.Column("ruleset", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("device_id", "revision"),
    )
    op.create_table(
        "automation_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        device_key(),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_index("ix_automation_events_device_id", "automation_events", ["device_id"])
    op.create_index("ix_automation_events_created_at", "automation_events", ["created_at"])
    op.create_table(
        "planning_jobs",
        device_key(True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_reasons", sa.Integer(), nullable=False),
    )


def downgrade():
    for table in ("planning_jobs", "automation_events", "rule_revisions", "device_automation"):
        op.drop_table(table)
