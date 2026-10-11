"""Replace the outdoor source with AQICN; retain separate legacy OpenAQ records."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def clear_selection():
    configuration = sa.table(
        "outdoor_configuration",
        sa.column("location_id", sa.Integer()),
        sa.column("latitude", sa.Float()),
        sa.column("longitude", sa.Float()),
        sa.column("discovery", sa.JSON()),
        sa.column("lease_token", sa.String()),
        sa.column("lease_until", sa.Float()),
        sa.column("next_request_at", sa.Float()),
        sa.column("retry_at", sa.Float()),
    )
    op.execute(
        configuration.update().values(
            location_id=None,
            latitude=None,
            longitude=None,
            discovery={},
            lease_token=None,
            lease_until=0,
            next_request_at=0,
            retry_at=0,
        )
    )


def upgrade():
    op.create_table(
        "aqicn_stations",
        sa.Column("location_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("readings", sa.JSON(), nullable=False),
        sa.Column("credential_version", sa.String(64), nullable=False),
        sa.Column("error", sa.String(300)),
        sa.Column("auth_error", sa.Boolean(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_success_at", sa.Float()),
        *(
            sa.Column(name, sa.Float(), nullable=False)
            for name in (
                "next_poll_at",
                "next_request_at",
                "retry_at",
                "lease_until",
            )
        ),
        sa.Column("lease_token", sa.String(36)),
    )
    op.create_table(
        "aqicn_readings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("aqicn_stations.location_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("metric", sa.String(10), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.Column("retrieved_at", sa.Float(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.UniqueConstraint("location_id", "metric", "recorded_at"),
    )
    op.create_index(
        "ix_aqicn_readings_series", "aqicn_readings", ["location_id", "metric", "recorded_at"]
    )
    op.create_index("ix_aqicn_readings_recorded_at", "aqicn_readings", ["recorded_at"])
    # IDs and units differ between providers. Never reuse an OpenAQ selection or series.
    clear_selection()


def downgrade():
    clear_selection()
    op.drop_table("aqicn_readings")
    op.drop_table("aqicn_stations")
