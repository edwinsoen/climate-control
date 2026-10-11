"""Cached AQICN views and durable collection, independent of device accounts."""

import asyncio
import logging
import math
import time
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Integer, case, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from climate_control.db import (
    LegacyOutdoorReading,
    OutdoorConfiguration,
    OutdoorReading,
    OutdoorStation,
)
from climate_control.history import RETENTION_SECONDS
from climate_control.providers.aqicn import METRICS, OutdoorError, OutdoorProvider
from climate_control.security import digest

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120
TICK_TIMEOUT_SECONDS = 90
REQUEST_SPACING_SECONDS = 2
SEARCH_QUEUE_SECONDS = 30


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def insert(db, model):
    return (sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert)(model)


class LostLease(Exception):
    pass


class OutdoorBusy(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, math.ceil(retry_after))


class OutdoorCollector:
    def __init__(self, sessions, settings, provider: OutdoorProvider):
        self.sessions = sessions
        self.settings = settings
        self.provider = provider
        self.credential_version = digest(settings.aqicn_api_token.get_secret_value())

    def claim(self):
        current = time.time()
        with self.sessions() as db:
            db.execute(
                delete(OutdoorReading).where(
                    OutdoorReading.recorded_at < current - RETENTION_SECONDS,
                )
            )
            db.execute(
                delete(LegacyOutdoorReading).where(
                    LegacyOutdoorReading.recorded_at < current - RETENTION_SECONDS
                )
            )
            configuration = db.get(OutdoorConfiguration, 1)
            location_id = configuration.location_id
            if location_id is None or not self.settings.aqicn_api_token.get_secret_value():
                db.commit()
                return None
            token = str(uuid4())
            claimed = db.execute(
                update(OutdoorConfiguration)
                .where(
                    OutdoorConfiguration.id == 1,
                    OutdoorConfiguration.location_id == location_id,
                    OutdoorConfiguration.lease_until <= current,
                    OutdoorConfiguration.retry_at <= current,
                )
                .values(lease_token=token, lease_until=current + LEASE_SECONDS)
            ).rowcount
            if not claimed:
                db.rollback()
                return None
            db.execute(
                insert(db, OutdoorStation)
                .values(
                    location_id=location_id,
                    credential_version=self.credential_version,
                    details={},
                    readings={},
                    auth_error=False,
                    failure_count=0,
                    next_poll_at=0,
                    next_request_at=0,
                    retry_at=0,
                    lease_until=0,
                )
                .on_conflict_do_nothing(index_elements=["location_id"])
            )
            # A new key revokes the old credential failure only when no worker owns the row.
            db.execute(
                update(OutdoorStation)
                .where(
                    OutdoorStation.location_id == location_id,
                    OutdoorStation.lease_until <= current,
                    OutdoorStation.credential_version != self.credential_version,
                )
                .values(
                    credential_version=self.credential_version,
                    auth_error=False,
                    error=None,
                    retry_at=0,
                    failure_count=0,
                    next_poll_at=0,
                )
            )
            claimed = db.execute(
                update(OutdoorStation)
                .where(
                    OutdoorStation.location_id == location_id,
                    OutdoorStation.credential_version == self.credential_version,
                    OutdoorStation.auth_error.is_(False),
                    OutdoorStation.lease_until <= current,
                    OutdoorStation.retry_at <= current,
                    OutdoorStation.next_poll_at <= current,
                )
                .values(lease_token=token, lease_until=current + LEASE_SECONDS)
            ).rowcount
            if not claimed:
                configuration.lease_token = None
                configuration.lease_until = 0
            db.commit()
            return db.get(OutdoorStation, location_id) if claimed else None

    def owned(self, db, station):
        # Obtain a write lock and verify ownership in the same transaction as persistence.
        self.configuration_owned(db, station.lease_token, station.location_id)
        claimed = db.execute(
            update(OutdoorStation)
            .where(
                OutdoorStation.location_id == station.location_id,
                OutdoorStation.lease_token == station.lease_token,
                OutdoorStation.lease_until > time.time(),
                OutdoorStation.credential_version == self.credential_version,
            )
            .values(lease_token=station.lease_token)
        ).rowcount
        if not claimed:
            raise LostLease()
        return db.get(OutdoorStation, station.location_id)

    def configuration_owned(self, db, token, location_id=None):
        conditions = [
            OutdoorConfiguration.id == 1,
            OutdoorConfiguration.lease_token == token,
            OutdoorConfiguration.lease_until > time.time(),
        ]
        if location_id is not None:
            conditions.append(OutdoorConfiguration.location_id == location_id)
        if not db.execute(
            update(OutdoorConfiguration)
            .where(*conditions)
            .values(
                lease_token=token,
            )
        ).rowcount:
            raise LostLease()
        return db.get(OutdoorConfiguration, 1)

    def save(self, station, samples=(), **changes):
        current = time.time()
        with self.sessions() as db:
            owned = self.owned(db, station)
            cached = dict(owned.readings)
            for reading in samples:
                if (
                    reading.metric not in METRICS
                    or isinstance(reading.value, bool)
                    or not math.isfinite(reading.value)
                    or reading.value < 0
                    or not math.isfinite(reading.recorded_at)
                    or not current - RETENTION_SECONDS <= reading.recorded_at <= current + 60
                ):
                    continue
                statement = insert(db, OutdoorReading).values(
                    location_id=station.location_id,
                    metric=reading.metric,
                    recorded_at=reading.recorded_at,
                    retrieved_at=current,
                    value=reading.value,
                )
                db.execute(
                    statement.on_conflict_do_update(
                        index_elements=["location_id", "metric", "recorded_at"],
                        set_={"value": reading.value, "retrieved_at": current},
                    )
                )
                previous = cached.get(reading.metric)
                if previous is None or reading.recorded_at >= previous["recorded_at"]:
                    cached[reading.metric] = {
                        "value": reading.value,
                        "recorded_at": reading.recorded_at,
                    }
            owned.readings = cached
            owned.next_request_at = max(owned.next_request_at, self.provider.retry_at)
            configuration = db.get(OutdoorConfiguration, 1)
            configuration.next_request_at = max(
                configuration.next_request_at, owned.next_request_at
            )
            for key, value in changes.items():
                setattr(owned, key, value)
            db.commit()
            return owned

    async def request(self, station, method, *args):
        with self.sessions() as db:
            owned = self.owned(db, station)
            configuration = db.get(OutdoorConfiguration, 1)
            start = max(
                time.time(),
                owned.next_request_at,
                configuration.next_request_at,
                configuration.retry_at,
                self.provider.retry_at,
            )
            delay = start - time.time()
            if delay > 30:
                raise OutdoorError("rate_limit", delay)
            owned.next_request_at = start + REQUEST_SPACING_SECONDS
            configuration.next_request_at = owned.next_request_at
            db.commit()
        if delay > 0:
            await asyncio.sleep(delay)
        with self.sessions() as db:
            self.owned(db, station)
            db.commit()
        return await method(*args)

    async def collect(self, station):
        feed = await self.request(station, self.provider.feed, station.location_id)
        if feed.location_id != station.location_id:
            raise OutdoorError("configuration")
        return self.save(
            station,
            feed.readings,
            details=feed.details,
            last_success_at=time.time(),
            error=None,
            next_poll_at=time.time() + self.settings.outdoor_poll_interval_seconds,
        )

    async def tick(self):
        station = self.claim()
        if station is None:
            return {"checked": 0}
        failure = None
        try:
            async with asyncio.timeout(TICK_TIMEOUT_SECONDS):
                station = await self.collect(station)
        except LostLease:
            return {"checked": 1}
        except OutdoorError as exc:
            failure = exc
        except asyncio.CancelledError:
            self.release(station, OutdoorError())
            raise
        except Exception:
            failure = OutdoorError()
        self.release(station, failure)
        logger.info("outdoor_polled status=%s", failure.kind if failure else "ok")
        return {"checked": 1}

    def save_shared_backoff(self, failure=None):
        # Provider deadlines outlive station/search leases and must only move forward.
        deadlines = {"next_request_at": self.provider.retry_at}
        if failure:
            deadlines["retry_at"] = time.time() + max(60, failure.retry_after)
        with self.sessions() as db:
            db.execute(
                update(OutdoorConfiguration)
                .where(OutdoorConfiguration.id == 1)
                .values(
                    **{
                        name: case(
                            (getattr(OutdoorConfiguration, name) < deadline, deadline),
                            else_=getattr(OutdoorConfiguration, name),
                        )
                        for name, deadline in deadlines.items()
                    }
                )
            )
            db.commit()

    def release(self, station, failure):
        self.save_shared_backoff(failure if failure and failure.kind == "rate_limit" else None)
        try:
            with self.sessions() as db:
                owned = self.owned(db, station)
                configuration = db.get(OutdoorConfiguration, 1)
                owned.next_request_at = max(owned.next_request_at, self.provider.retry_at)
                configuration.next_request_at = max(
                    configuration.next_request_at,
                    owned.next_request_at,
                )
                if failure:
                    owned.failure_count += 1
                    delay = min(900, 60 * 2 ** min(owned.failure_count - 1, 4))
                    owned.retry_at = time.time() + max(delay, failure.retry_after)
                    owned.auth_error = failure.kind == "auth"
                    if failure.kind == "rate_limit":
                        configuration.retry_at = max(configuration.retry_at, owned.retry_at)
                    owned.error = str(failure)
                else:
                    owned.failure_count = 0
                    owned.retry_at = 0
                owned.lease_token = None
                owned.lease_until = 0
                configuration.lease_token = None
                configuration.lease_until = 0
                db.commit()
        except LostLease:
            pass

    def claim_search(self):
        token = str(uuid4())
        with self.sessions() as db:
            current = time.time()
            claimed = db.execute(
                update(OutdoorConfiguration)
                .where(
                    OutdoorConfiguration.id == 1,
                    OutdoorConfiguration.lease_until <= current,
                    OutdoorConfiguration.retry_at <= current,
                    OutdoorConfiguration.next_request_at <= current,
                )
                .values(
                    lease_token=token,
                    lease_until=current + LEASE_SECONDS,
                    next_request_at=current + REQUEST_SPACING_SECONDS,
                )
            ).rowcount
            if not claimed:
                configuration = db.get(OutdoorConfiguration, 1)
                raise OutdoorBusy(
                    max(
                        configuration.lease_until,
                        configuration.retry_at,
                        configuration.next_request_at,
                    )
                    - current
                )
            db.commit()
        return token

    async def search(self, query):
        return await self.search_operation(self.provider.search, query)

    async def nearby(self, latitude, longitude):
        return await self.search_operation(self.provider.nearby, latitude, longitude)

    async def search_operation(self, method, *args):
        if not self.settings.aqicn_api_token.get_secret_value():
            raise OutdoorError("configuration")
        deadline = time.monotonic() + SEARCH_QUEUE_SECONDS
        while True:
            try:
                token = self.claim_search()
                break
            except OutdoorBusy:
                with self.sessions() as db:
                    retry_at = db.get(OutdoorConfiguration, 1).retry_at
                if retry_at > time.time() or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(0.25, deadline - time.monotonic()))
        failure = None
        try:
            async with asyncio.timeout(TICK_TIMEOUT_SECONDS):
                results = await method(*args)
            with self.sessions() as db:
                configuration = self.configuration_owned(db, token)
                configuration.discovery = {"stations": results}
                db.commit()
            return results
        except OutdoorError as exc:
            failure = exc
            raise
        except LostLease:
            raise OutdoorBusy(1) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = OutdoorError()
            raise failure from None
        finally:
            self.save_shared_backoff(failure)
            with self.sessions() as db:
                try:
                    configuration = self.configuration_owned(db, token)
                    configuration.lease_token = None
                    configuration.lease_until = 0
                    db.commit()
                except LostLease:
                    pass


def selected_location(db):
    return db.get(OutdoorConfiguration, 1).location_id


def set_location(db, location_id):
    # Updating this row revokes any poll/search lease before changing the selection.
    db.execute(
        update(OutdoorConfiguration)
        .where(OutdoorConfiguration.id == 1)
        .values(
            lease_token=None,
            lease_until=0,
        )
    )
    configuration = db.get(OutdoorConfiguration, 1)
    if location_id is not None:
        candidate = next(
            (
                station
                for station in configuration.discovery.get("stations", [])
                if station["location_id"] == location_id
            ),
            None,
        )
        if candidate is None:
            raise ValueError("Search for a station before selecting a location.")
        coordinates = candidate.get("coordinates") or {}
        configuration.latitude = coordinates.get("latitude")
        configuration.longitude = coordinates.get("longitude")
    else:
        configuration.latitude = None
        configuration.longitude = None
    # Retain samples but invalidate any previous station worker, even when reselecting it.
    for station in db.scalars(
        select(OutdoorStation).where(
            OutdoorStation.location_id.in_([configuration.location_id, location_id]),
        )
    ):
        station.lease_token = None
        station.lease_until = 0
        if station.location_id == location_id:
            station.next_poll_at = 0
            station.auth_error = False
            station.retry_at = 0
    configuration.location_id = location_id
    if location_id is not None:
        station = db.get(OutdoorStation, location_id)
        if station is None:
            station = OutdoorStation(location_id=location_id, credential_version="", details={})
            db.add(station)
        details = dict(station.details)
        details.update(
            {
                key: value
                for key, value in candidate.items()
                if key in {"name", "url", "coordinates", "attribution"}
            }
        )
        # Search timestamps may have no timezone. Keep the preview separate from observations.
        preview = None
        value = candidate.get("aqi")
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            preview = {
                "value": value,
                "updated_at": candidate.get("updated_at") or candidate.get("measured_at"),
            }
        station.details = {**details, "preview": preview, "refresh_pending": True}
        station.error = None
    db.commit()


def outdoor_view(db, settings):
    configuration = db.get(OutdoorConfiguration, 1)
    location_id = configuration.location_id
    station = db.get(OutdoorStation, location_id) if location_id else None
    current = time.time()
    available = bool(settings.aqicn_api_token.get_secret_value())
    return {
        "available": available,
        "configured": location_id is not None,
        "source": "aqicn",
        "location_id": location_id,
        "coordinates": {"latitude": configuration.latitude, "longitude": configuration.longitude}
        if configuration.latitude is not None
        else None,
        "station": {
            key: value
            for key, value in station.details.items()
            if key not in {"preview", "refresh_pending"}
        }
        if station
        else None,
        "preview": station.details.get("preview") if station else None,
        "loading": bool(
            available and station and station.details.get("refresh_pending") and not station.error
        ),
        "capabilities": list(station.readings) if station else [],
        "metrics": {
            metric: {
                "value": reading["value"],
                "unit": "AQI",
                "measured_at": timestamp(reading["recorded_at"]),
                "stale": current - reading["recorded_at"] > settings.outdoor_stale_after_seconds,
            }
            for metric, reading in (station.readings.items() if station else [])
        },
        "last_success_at": timestamp(station.last_success_at) if station else None,
        "error": (station.error if available else "Outdoor data is not connected.")
        if station
        else None,
        "backfill": {"status": "unsupported", "error": None},
    }


def outdoor_history(db, location_id, hours, current):
    station = db.get(OutdoorStation, location_id) if location_id else None
    start = current - hours * 3600
    interval = max(60, math.ceil(hours * 3600 / 360))
    bucket = cast(func.floor(OutdoorReading.recorded_at / interval), Integer)
    metrics = {metric: [] for metric in METRICS}
    for metric in METRICS if station else []:
        rows = db.execute(
            select(
                bucket.label("bucket"),
                func.avg(OutdoorReading.value).label("value"),
                func.min(OutdoorReading.value).label("min"),
                func.max(OutdoorReading.value).label("max"),
                func.count(OutdoorReading.value).label("count"),
            )
            .where(
                OutdoorReading.location_id == location_id,
                OutdoorReading.metric == metric,
                OutdoorReading.recorded_at >= start,
                OutdoorReading.recorded_at <= current,
            )
            .group_by(bucket)
            .order_by(bucket)
        ).mappings()
        metrics[metric] = [
            {
                "recorded_at": min(current, max(start, (row["bucket"] + 0.5) * interval)),
                "value": round(row["value"], 2),
                "min": row["min"],
                "max": row["max"],
                "count": row["count"],
            }
            for row in rows
        ]
    return {
        "source": "aqicn",
        "start": start,
        "end": current,
        "interval_seconds": interval,
        "metrics": metrics,
    }
