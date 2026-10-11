"""Pair observed purifier settings with air-quality readings."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("device_readings", sa.Column("power", sa.String(10), nullable=True))
    op.add_column("device_readings", sa.Column("mode", sa.String(50), nullable=True))
    op.add_column("device_readings", sa.Column("fan_level", sa.Integer(), nullable=True))
    op.add_column("device_readings", sa.Column("reported_fan_level", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("device_readings") as batch:
        batch.drop_column("reported_fan_level")
        batch.drop_column("fan_level")
        batch.drop_column("mode")
        batch.drop_column("power")
