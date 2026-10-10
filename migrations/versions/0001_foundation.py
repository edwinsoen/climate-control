"""Accounts, snapshots, authentication, and polling coordination."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("time_zone", sa.String(100), nullable=False),
        sa.Column("credentials", sa.String(), nullable=False),
        sa.Column("tokens", sa.String(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("last_attempt_at", sa.Float(), nullable=True),
        sa.Column("last_success_at", sa.Float(), nullable=True),
        sa.Column("next_poll_at", sa.Float(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("rate_limit_count", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("supported", sa.Boolean(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("retrieved_at", sa.Float(), nullable=True),
        sa.Column("error", sa.String(300), nullable=True),
        sa.UniqueConstraint("account_id", "provider_id"),
    )
    op.create_index("ix_devices_account_id", "devices", ["account_id"])
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("identity", sa.String(254), nullable=False),
        sa.Column("csrf_token", sa.String(100), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table(
        "login_throttles",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "oauth_flows",
        sa.Column("state", sa.String(100), primary_key=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )


def downgrade():
    for name in ("oauth_flows", "login_throttles", "sessions", "devices", "accounts"):
        op.drop_table(name)
