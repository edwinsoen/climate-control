"""Add Xiaomi account uniqueness per provider/region and indoor temperature/humidity readings."""

from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _email_unique_constraint_name(bind) -> str:
    for constraint in sa.inspect(bind).get_unique_constraints("accounts"):
        if constraint.get("column_names") == ["email"]:
            return constraint.get("name") or "uq_accounts_email"
    return "uq_accounts_email"


@contextmanager
def _without_sqlite_foreign_keys(bind):
    if bind.dialect.name != "sqlite":
        yield
        return
    # pysqlite only starts transactions on DML; commit if open so PRAGMA foreign_keys=OFF
    # takes effect and batch_alter_table's DROP TABLE does not cascade ON DELETE to devices.
    raw = bind.connection.dbapi_connection
    if raw.in_transaction:
        bind.exec_driver_sql("COMMIT")
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        yield
        if raw.in_transaction:
            bind.exec_driver_sql("COMMIT")
    finally:
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade():
    op.add_column("device_readings", sa.Column("temperature", sa.Float(), nullable=True))
    op.add_column("device_readings", sa.Column("humidity", sa.Float(), nullable=True))
    bind = op.get_bind()
    email_uq = _email_unique_constraint_name(bind)
    with _without_sqlite_foreign_keys(bind):
        with op.batch_alter_table("accounts", naming_convention=NAMING_CONVENTION) as batch:
            batch.drop_constraint(email_uq, type_="unique")
            batch.create_unique_constraint(
                "uq_accounts_provider_email_country",
                ["provider", "email", "country_code"],
            )
    if bind.dialect.name != "sqlite":
        op.alter_column(
            "devices",
            "display_mode",
            existing_type=sa.String(20),
            type_=sa.String(30),
            existing_nullable=False,
            existing_server_default="manual",
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.alter_column(
            "devices",
            "display_mode",
            existing_type=sa.String(30),
            type_=sa.String(20),
            existing_nullable=False,
            existing_server_default="manual",
        )
    with _without_sqlite_foreign_keys(bind):
        with op.batch_alter_table("accounts", naming_convention=NAMING_CONVENTION) as batch:
            batch.drop_constraint("uq_accounts_provider_email_country", type_="unique")
            batch.create_unique_constraint("uq_accounts_email", ["email"])
    with op.batch_alter_table("device_readings") as batch:
        batch.drop_column("humidity")
        batch.drop_column("temperature")
