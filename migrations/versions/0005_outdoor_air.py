"""Outdoor air measurements and durable collection progress."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outdoor_stations",
        sa.Column("location_id", sa.Integer(), primary_key=True, autoincrement=False),
        *(
            sa.Column(name, sa.JSON(), nullable=False)
            for name in ("details", "sensors", "readings", "backfill_cursor")
        ),
        sa.Column("credential_version", sa.String(64), nullable=False),
        sa.Column("error", sa.String(300)),
        sa.Column("backfill_error", sa.String(300)),
        sa.Column("auth_error", sa.Boolean(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_success_at", sa.Float()),
        *(
            sa.Column(name, sa.Float(), nullable=False)
            for name in (
                "next_poll_at",
                "next_metadata_at",
                "next_request_at",
                "retry_at",
                "next_reconcile_at",
                "lease_until",
            )
        ),
        sa.Column("lease_token", sa.String(36)),
    )
    op.create_table(
        "outdoor_readings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("outdoor_stations.location_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sensor_id", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(10), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.Column("retrieved_at", sa.Float(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.UniqueConstraint("location_id", "sensor_id", "recorded_at"),
    )
    op.create_index(
        "ix_outdoor_readings_series",
        "outdoor_readings",
        ["location_id", "sensor_id", "recorded_at"],
    )
    op.create_index("ix_outdoor_readings_recorded_at", "outdoor_readings", ["recorded_at"])


def downgrade():
    op.drop_table("outdoor_readings")
    op.drop_table("outdoor_stations")
