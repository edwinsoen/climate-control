from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_provider_migration_preserves_existing_accounts(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0001")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO accounts (
                id, label, email, country_code, time_zone, credentials, tokens, status,
                next_poll_at, failure_count, rate_limit_count, lease_until, created_at
            ) VALUES (
                'existing-account', 'Home', 'owner@example.com', 'SG', 'Asia/Singapore',
                'encrypted-credentials', 'encrypted-tokens', 'connected', 123, 0, 0, 0, 100
            )
        """))
        connection.execute(text("""
            INSERT INTO devices (
                id, account_id, provider_id, name, model, supported, capabilities, state
            ) VALUES (
                'existing-device', 'existing-account', 'device-1', 'Purifier', 'Core 400S',
                true, '[]', '{}'
            )
        """))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        account = connection.execute(text("SELECT * FROM accounts")).mappings().one()
        assert account["provider"] == "vesync"
        assert account["credentials"] == "encrypted-credentials"
        assert account["tokens"] == "encrypted-tokens"
        assert account["next_poll_at"] == 123
        assert account["next_control_at"] == 0
        assert connection.execute(text("SELECT controls FROM devices")).scalar_one() == "{}"
        assert connection.execute(text("SELECT id FROM devices")).scalar_one() == "existing-device"
    columns = {column["name"]: column for column in inspect(engine).get_columns("accounts")}
    assert not columns["provider"]["nullable"]
    assert columns["provider"]["default"] == "'vesync'"
    engine.dispose()
