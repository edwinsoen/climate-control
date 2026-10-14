from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from climate_control.db import LegacyOutdoorReading, LegacyOutdoorStation, OutdoorStation


def test_provider_migration_preserves_existing_accounts(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "0001")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO accounts (
                id, label, email, country_code, time_zone, credentials, tokens, status,
                next_poll_at, failure_count, rate_limit_count, lease_until, created_at
            ) VALUES (
                'existing-account', 'Home', 'owner@example.com', 'SG', 'Asia/Singapore',
                'encrypted-credentials', 'encrypted-tokens', 'connected', 123, 0, 0, 0, 100
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO devices (
                id, account_id, provider_id, name, model, supported, capabilities, state
            ) VALUES (
                'existing-device', 'existing-account', 'device-1', 'Purifier', 'Core 400S',
                true, '[]', '{}'
            )
        """)
        )
    command.upgrade(config, "0003")
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO device_readings (device_id, recorded_at, pm25)
            VALUES ('existing-device', 123, 8)
        """)
        )
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
        automation = connection.execute(text("SELECT * FROM device_automation")).mappings().one()
        assert automation["device_id"] == "existing-device"
        assert automation["owner"] == "manual"
        assert not automation["enabled"]
        reading = connection.execute(text("SELECT * FROM device_readings")).mappings().one()
        assert reading["pm25"] == 8
        assert (
            reading["power"],
            reading["mode"],
            reading["fan_level"],
            reading["reported_fan_level"],
        ) == (None, None, None, None)
    columns = {column["name"]: column for column in inspect(engine).get_columns("accounts")}
    assert not columns["provider"]["nullable"]
    assert columns["provider"]["default"] == "'vesync'"
    command.downgrade(config, "0009")
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO rule_revisions (id, device_id, revision, owner, ruleset, reason, created_at)
            VALUES ('r1', 'existing-device', 1, 'manual', '{}', 'Saved', 1)
        """)
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT * FROM rule_revisions")).mappings().one()
        assert (revision["reason"], revision["author"]) == ("Saved", None)
    command.downgrade(config, "0007")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT pm25 FROM device_readings")).scalar_one() == 8
    engine.dispose()


def test_aqicn_migration_preserves_legacy_history_without_reusing_ids_or_selection(tmp_path):
    url = f"sqlite:///{tmp_path / 'outdoor-migration.db'}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "0006")
    engine = create_engine(url)
    with Session(engine) as db:
        db.add(LegacyOutdoorStation(location_id=1, credential_version="old", details={}))
        db.flush()
        db.add(
            LegacyOutdoorReading(
                location_id=1,
                sensor_id=11,
                metric="pm25",
                value=8,
                recorded_at=100,
                retrieved_at=101,
            )
        )
        db.execute(text("UPDATE outdoor_configuration SET location_id=1, latitude=1, longitude=2"))
        db.commit()
    command.upgrade(config, "head")
    with Session(engine) as db:
        assert db.get(LegacyOutdoorReading, 1).value == 8
        assert db.get(OutdoorStation, 1) is None
        assert (
            db.execute(text("SELECT location_id FROM outdoor_configuration")).scalar_one() is None
        )
        db.add(OutdoorStation(location_id=1, credential_version="new", details={}))
        db.commit()
        assert db.get(LegacyOutdoorStation, 1).credential_version == "old"
        assert db.get(OutdoorStation, 1).credential_version == "new"
    engine.dispose()
