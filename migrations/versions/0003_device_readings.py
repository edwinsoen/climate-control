"""Record air quality from successful device polls."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_readings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(36),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.Column("pm25", sa.Float(), nullable=True),
        sa.Column("pm1", sa.Float(), nullable=True),
        sa.Column("pm10", sa.Float(), nullable=True),
        sa.Column("air_quality", sa.Float(), nullable=True),
        sa.UniqueConstraint("device_id", "recorded_at"),
    )
    op.create_index("ix_device_readings_recorded_at", "device_readings", ["recorded_at"])


def downgrade():
    op.drop_table("device_readings")
