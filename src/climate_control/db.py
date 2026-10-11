import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def new_id() -> str:
    return str(uuid4())


def now() -> float:
    return time.time()


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50), server_default="vesync")
    email: Mapped[str] = mapped_column(String(254), unique=True)
    country_code: Mapped[str] = mapped_column(String(2))
    time_zone: Mapped[str] = mapped_column(String(100))
    credentials: Mapped[str] = mapped_column(String)
    tokens: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_attempt_at: Mapped[float | None] = mapped_column(nullable=True)
    last_success_at: Mapped[float | None] = mapped_column(nullable=True)
    next_poll_at: Mapped[float] = mapped_column(default=0.0)
    next_control_at: Mapped[float] = mapped_column(default=0.0, server_default="0")
    failure_count: Mapped[int] = mapped_column(default=0)
    rate_limit_count: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[float] = mapped_column(default=now)


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("account_id", "provider_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    model: Mapped[str] = mapped_column(String(100))
    supported: Mapped[bool] = mapped_column(default=True)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    controls: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieved_at: Mapped[float | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class DeviceReading(Base):
    __tablename__ = "device_readings"
    __table_args__ = (
        UniqueConstraint("device_id", "recorded_at"),
        Index("ix_device_readings_recorded_at", "recorded_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    recorded_at: Mapped[float]
    pm25: Mapped[float | None] = mapped_column(nullable=True)
    pm1: Mapped[float | None] = mapped_column(nullable=True)
    pm10: Mapped[float | None] = mapped_column(nullable=True)
    air_quality: Mapped[float | None] = mapped_column(nullable=True)


class OutdoorConfiguration(Base):
    __tablename__ = "outdoor_configuration"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    location_id: Mapped[int | None] = mapped_column(nullable=True)
    latitude: Mapped[float | None] = mapped_column(nullable=True)
    longitude: Mapped[float | None] = mapped_column(nullable=True)
    discovery: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float] = mapped_column(default=0.0)
    next_request_at: Mapped[float] = mapped_column(default=0.0)
    retry_at: Mapped[float] = mapped_column(default=0.0)


class LegacyOutdoorStation(Base):
    __tablename__ = "outdoor_stations"
    location_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    sensors: Mapped[dict] = mapped_column(JSON, default=dict)
    readings: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_version: Mapped[str] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    backfill_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    auth_error: Mapped[bool] = mapped_column(default=False)
    failure_count: Mapped[int] = mapped_column(default=0)
    last_success_at: Mapped[float | None] = mapped_column(nullable=True)
    next_poll_at: Mapped[float] = mapped_column(default=0.0)
    next_metadata_at: Mapped[float] = mapped_column(default=0.0)
    next_request_at: Mapped[float] = mapped_column(default=0.0)
    retry_at: Mapped[float] = mapped_column(default=0.0)
    next_reconcile_at: Mapped[float] = mapped_column(default=0.0)
    backfill_cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float] = mapped_column(default=0.0)


class LegacyOutdoorReading(Base):
    __tablename__ = "outdoor_readings"
    __table_args__ = (
        UniqueConstraint("location_id", "sensor_id", "recorded_at"),
        Index("ix_outdoor_readings_series", "location_id", "sensor_id", "recorded_at"),
        Index("ix_outdoor_readings_recorded_at", "recorded_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("outdoor_stations.location_id", ondelete="CASCADE")
    )
    sensor_id: Mapped[int]
    metric: Mapped[str] = mapped_column(String(10))
    recorded_at: Mapped[float]
    retrieved_at: Mapped[float]
    value: Mapped[float]


class OutdoorStation(Base):
    __tablename__ = "aqicn_stations"
    location_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    readings: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_version: Mapped[str] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    auth_error: Mapped[bool] = mapped_column(default=False)
    failure_count: Mapped[int] = mapped_column(default=0)
    last_success_at: Mapped[float | None] = mapped_column(nullable=True)
    next_poll_at: Mapped[float] = mapped_column(default=0.0)
    next_request_at: Mapped[float] = mapped_column(default=0.0)
    retry_at: Mapped[float] = mapped_column(default=0.0)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float] = mapped_column(default=0.0)


class OutdoorReading(Base):
    __tablename__ = "aqicn_readings"
    __table_args__ = (
        UniqueConstraint("location_id", "metric", "recorded_at"),
        Index("ix_aqicn_readings_series", "location_id", "metric", "recorded_at"),
        Index("ix_aqicn_readings_recorded_at", "recorded_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("aqicn_stations.location_id", ondelete="CASCADE")
    )
    metric: Mapped[str] = mapped_column(String(10))
    recorded_at: Mapped[float]
    retrieved_at: Mapped[float]
    value: Mapped[float]


class LoginSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity: Mapped[str] = mapped_column(String(254))
    csrf_token: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[float] = mapped_column(index=True)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[float]


class OAuthFlow(Base):
    __tablename__ = "oauth_flows"
    state: Mapped[str] = mapped_column(String(100), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[float]


def database(database_url: str):
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    sqlite = database_url.startswith("sqlite")
    if sqlite and ":memory:" not in database_url:
        path = database_url.split("///", 1)[-1]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 10} if sqlite else {},
        pool_pre_ping=True,
    )
    if sqlite:

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine, sessionmaker(engine, expire_on_commit=False)
