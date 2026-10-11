import asyncio
import math
import time
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from climate_control.config import Settings
from climate_control.db import OutdoorConfiguration, OutdoorReading, OutdoorStation
from climate_control.history import RETENTION_SECONDS
from climate_control.outdoor import (
    LostLease,
    OutdoorBusy,
    OutdoorCollector,
    outdoor_view,
    selected_location,
    set_location,
)
from climate_control.providers.aqicn import Measurement, OutdoorError, StationFeed


def date(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


class FakeOutdoor:
    retry_at = 0.0

    def __init__(self):
        self.calls = []
        self.feed_results = []
        self.search_results = []
        now = time.time()
        self.readings = [
            Measurement("aqi", now - 3600, 42),
            Measurement("pm25", now - 3600, 42),
            Measurement("pm10", now - 5 * 3600, 20),
        ]

    def details(self, location_id):
        return {
            "name": "Outdoor test station" if location_id == 1 else "Other test station",
            "url": "https://aqicn.org/city/test/",
            "coordinates": {"latitude": 1.35, "longitude": 103.8},
            "attribution": [{"name": "Test agency", "url": "https://example.com/agency"}],
        }

    async def feed(self, location_id):
        self.calls.append(("feed", location_id))
        if self.feed_results:
            result = self.feed_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return StationFeed(location_id, self.details(location_id), self.readings)

    async def search(self, query):
        self.calls.append(("search", query))
        if self.search_results:
            result = self.search_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return [
            {
                "location_id": i,
                **self.details(i),
                "aqi": 42,
                "updated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            }
            for i in (1, 2)
        ]

    async def nearby(self, latitude, longitude):
        self.calls.append(("nearby", latitude, longitude))
        return [{"location_id": 1, **self.details(1), "aqi": 42}]


@pytest.fixture
def outdoor(app, monkeypatch):
    monkeypatch.setattr("climate_control.outdoor.REQUEST_SPACING_SECONDS", 0)
    app.state.settings.aqicn_api_token = SecretStr("outdoor-test-token")
    with app.state.sessions() as db:
        db.get(OutdoorConfiguration, 1).location_id = 1
        db.commit()
    collector = OutdoorCollector(app.state.sessions, app.state.settings, FakeOutdoor())
    app.state.outdoor = collector
    app.state.poller.outdoor = collector
    return collector


def due(app, **changes):
    with app.state.sessions() as db:
        station = db.get(OutdoorStation, selected_location(db))
        station.next_poll_at = 0
        station.retry_at = 0
        for key, value in changes.items():
            setattr(station, key, value)
        if "lease_until" in changes:
            db.get(OutdoorConfiguration, 1).lease_until = changes["lease_until"]
        db.commit()


async def test_current_and_history_are_cached_aqi_and_require_login(app, outdoor, signed_client):
    await outdoor.tick()
    calls = len(outdoor.provider.calls)
    data = (await signed_client.get("/api/outdoor")).json()
    assert data["source"] == "aqicn"
    assert data["metrics"]["aqi"]["unit"] == "AQI"
    assert data["metrics"]["aqi"]["value"] == 42
    assert not data["metrics"]["aqi"]["stale"]
    assert data["metrics"]["pm10"]["stale"]
    assert data["metrics"]["aqi"]["measured_at"] != data["last_success_at"]
    assert data["backfill"]["status"] == "unsupported"
    assert "outdoor-test-token" not in str(data)
    history = (await signed_client.get("/api/outdoor/history?hours=720")).json()
    assert history["source"] == "aqicn"
    assert history["metrics"]["aqi"][0]["value"] == 42
    assert len(outdoor.provider.calls) == calls
    assert (await signed_client.get("/api/outdoor/history?hours=1")).status_code == 422
    signed_client.cookies.clear()
    assert (await signed_client.get("/api/outdoor")).status_code == 401
    assert (await signed_client.get("/api/outdoor/history")).status_code == 401


async def test_poll_interval_and_missing_token_do_not_contact_provider(app, outdoor, signed_client):
    await outdoor.tick()
    other = OutdoorCollector(app.state.sessions, app.state.settings, outdoor.provider)
    assert (await other.tick())["checked"] == 0
    assert outdoor.provider.calls == [("feed", 1)]
    app.state.settings.aqicn_api_token = SecretStr("")
    due(app)
    assert (await other.tick())["checked"] == 0
    data = (await signed_client.get("/api/outdoor")).json()
    assert not data["available"]
    assert data["metrics"]["aqi"]["value"] == 42


async def test_duplicate_corrections_and_older_or_invalid_samples_preserve_current(app, outdoor):
    await outdoor.tick()
    recorded = outdoor.provider.readings[0].recorded_at
    outdoor.provider.readings[0].value = 45
    due(app)
    await outdoor.tick()
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(OutdoorReading)) == 3
        assert db.get(OutdoorStation, 1).readings["aqi"]["value"] == 45
    outdoor.provider.readings = [
        Measurement("aqi", recorded - 60, 100),
        Measurement("aqi", time.time(), math.nan),
        Measurement("aqi", time.time(), -1),
        Measurement("aqi", time.time(), True),
        Measurement("pm25", time.time() + 600, 5),
    ]
    due(app)
    await outdoor.tick()
    with app.state.sessions() as db:
        assert db.get(OutdoorStation, 1).readings["aqi"]["value"] == 45
        assert db.scalar(select(func.count()).select_from(OutdoorReading)) == 4


@pytest.mark.parametrize("kind", ["temporary", "auth", "rate_limit"])
async def test_failures_preserve_cached_state_and_timestamps(app, outdoor, kind):
    await outdoor.tick()
    with app.state.sessions() as db:
        before = db.get(OutdoorStation, 1).last_success_at
    due(app)
    outdoor.provider.feed_results = [OutdoorError(kind, 600 if kind == "rate_limit" else 0)]
    await outdoor.tick()
    with app.state.sessions() as db:
        station = db.get(OutdoorStation, 1)
        assert station.last_success_at == before
        assert station.readings["aqi"]["value"] == 42
        assert station.error
        assert station.auth_error == (kind == "auth")
    calls = len(outdoor.provider.calls)
    await OutdoorCollector(app.state.sessions, app.state.settings, outdoor.provider).tick()
    assert len(outdoor.provider.calls) == calls


async def test_replaced_token_clears_auth_failure(app, outdoor):
    outdoor.provider.feed_results = [OutdoorError("auth")]
    await outdoor.tick()
    app.state.settings.aqicn_api_token = SecretStr("replacement-token")
    await OutdoorCollector(app.state.sessions, app.state.settings, outdoor.provider).tick()
    with app.state.sessions() as db:
        assert not db.get(OutdoorStation, 1).auth_error
        assert db.get(OutdoorStation, 1).readings["aqi"]["value"] == 42


async def test_overlapping_workers_and_expired_lease_cannot_persist(app, outdoor):
    started, resume = asyncio.Event(), asyncio.Event()
    feed = outdoor.provider.feed

    async def blocked_feed(location_id):
        started.set()
        await resume.wait()
        return await feed(location_id)

    outdoor.provider.feed = blocked_feed
    worker = asyncio.create_task(outdoor.tick())
    await started.wait()
    assert (await outdoor.tick())["checked"] == 0
    resume.set()
    await worker
    due(app)
    old = outdoor.claim()
    due(app, lease_until=0)
    new = outdoor.claim()
    with pytest.raises(LostLease):
        outdoor.save(old, [Measurement("aqi", time.time(), 999)])
    outdoor.release(old, OutdoorError())
    with app.state.sessions() as db:
        assert db.get(OutdoorStation, 1).lease_token == new.lease_token


async def test_request_pacing_persists_between_workers(app, outdoor, monkeypatch):
    monkeypatch.setattr("climate_control.outdoor.REQUEST_SPACING_SECONDS", 2)
    station = outdoor.claim()
    await outdoor.request(station, outdoor.provider.feed, 1)
    outdoor.release(station, None)
    station = outdoor.claim()
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("climate_control.outdoor.asyncio.sleep", sleep)
    await outdoor.request(station, outdoor.provider.feed, 1)
    assert delays and delays[0] > 1


async def test_direct_city_search_select_change_and_remove_persist(app, outdoor, signed_client):
    search = await signed_client.post("/api/outdoor/stations/search", json={"query": " Singapore "})
    assert search.status_code == 200
    assert outdoor.provider.calls == [("search", "Singapore")]
    selected = await signed_client.put("/api/outdoor/location", json={"location_id": 1})
    assert selected.json()["coordinates"] == {"latitude": 1.35, "longitude": 103.8}
    assert selected.json()["station"]["name"] == "Outdoor test station"
    assert selected.json()["preview"]["value"] == 42
    assert selected.json()["loading"] is True
    # The save response is immediate; the existing collector runs as a background task.
    assert outdoor.provider.calls == [("search", "Singapore"), ("feed", 1)]
    replacement = OutdoorCollector(app.state.sessions, app.state.settings, outdoor.provider)
    await replacement.tick()
    assert (await signed_client.get("/api/outdoor")).json()["metrics"]["aqi"]["value"] == 42
    outdoor.provider.readings[0].value = 77
    await signed_client.put("/api/outdoor/location", json={"location_id": 2})
    assert (await signed_client.get("/api/outdoor/history")).json()["metrics"]["aqi"][0][
        "value"
    ] == 77
    await signed_client.delete("/api/outdoor/location")
    assert (await signed_client.get("/api/outdoor")).json()["configured"] is False
    with app.state.sessions() as db:
        db.add(
            OutdoorReading(
                location_id=1,
                metric="aqi",
                value=999,
                recorded_at=time.time() - RETENTION_SECONDS - 10,
                retrieved_at=time.time(),
            )
        )
        db.commit()
    calls = len(outdoor.provider.calls)
    await replacement.tick()
    assert len(outdoor.provider.calls) == calls
    with app.state.sessions() as db:
        assert (
            db.scalar(select(func.min(OutdoorReading.recorded_at)))
            > time.time() - RETENTION_SECONDS
        )


async def test_location_change_revokes_pending_poll_and_preserves_rate_limit(app, outdoor):
    await outdoor.tick()
    due(app)
    old = outdoor.claim()
    with app.state.sessions() as db:
        configuration = db.get(OutdoorConfiguration, 1)
        configuration.discovery = {"stations": [{"location_id": 2, "name": "New station"}]}
        configuration.next_request_at = time.time() + 5
        configuration.retry_at = time.time() + 600
        db.commit()
        set_location(db, 2)
        assert db.get(OutdoorConfiguration, 1).next_request_at > time.time()
    with pytest.raises(LostLease):
        outdoor.save(old, [Measurement("aqi", time.time(), 999)])
    outdoor.release(old, OutdoorError())
    assert (await outdoor.tick())["checked"] == 0
    with app.state.sessions() as db:
        assert db.get(OutdoorStation, 1).readings["aqi"]["value"] == 42
        assert selected_location(db) == 2


@pytest.mark.parametrize("operation", ["feed", "search", "nearby"])
@pytest.mark.parametrize("existing_cooldown", [0, 1200])
async def test_revoked_request_saves_shared_backoff_without_releasing_new_worker(
    app, outdoor, monkeypatch, operation, existing_cooldown
):
    await outdoor.tick()
    await outdoor.search("Singapore")
    due(app)
    with app.state.sessions() as db:
        station = db.get(OutdoorStation, 1)
        before = (station.readings, station.last_success_at, station.error, station.failure_count)
        discovery = db.get(OutdoorConfiguration, 1).discovery

    started, resume = asyncio.Event(), asyncio.Event()

    async def blocked_request(*args):
        started.set()
        await resume.wait()
        outdoor.provider.retry_at = time.time() + 600
        raise OutdoorError("rate_limit", 600)

    monkeypatch.setattr(outdoor.provider, operation, blocked_request)
    if operation == "feed":
        worker = asyncio.create_task(outdoor.tick())
    elif operation == "search":
        worker = asyncio.create_task(outdoor.search("Other city"))
    else:
        worker = asyncio.create_task(outdoor.nearby(1.35, 103.8))
    await started.wait()
    with app.state.sessions() as db:
        set_location(db, 2)

    replacement = OutdoorCollector(app.state.sessions, app.state.settings, FakeOutdoor())
    new_station = replacement.claim()
    assert new_station is not None
    deadline = time.time() + existing_cooldown
    if existing_cooldown:
        with app.state.sessions() as db:
            configuration = db.get(OutdoorConfiguration, 1)
            configuration.retry_at = deadline
            configuration.next_request_at = deadline
            db.commit()

    resume.set()
    if operation == "feed":
        await worker
    else:
        with pytest.raises(OutdoorError, match="limiting requests"):
            await worker
    with app.state.sessions() as db:
        configuration = db.get(OutdoorConfiguration, 1)
        assert configuration.retry_at >= max(deadline, outdoor.provider.retry_at)
        assert configuration.next_request_at >= max(deadline, outdoor.provider.retry_at)
        assert configuration.lease_token == new_station.lease_token
        assert configuration.discovery == discovery
        station = db.get(OutdoorStation, 1)
        assert (
            station.readings,
            station.last_success_at,
            station.error,
            station.failure_count,
        ) == before

    replacement.release(new_station, None)
    assert (await replacement.tick())["checked"] == 0
    with pytest.raises(OutdoorBusy) as exc:
        await replacement.search("Singapore")
    assert exc.value.retry_after > 590
    assert replacement.provider.calls == []


@pytest.mark.parametrize("operation", ["search", "nearby"])
async def test_revoked_search_cannot_replace_discovery(app, outdoor, monkeypatch, operation):
    await outdoor.search("Singapore")
    with app.state.sessions() as db:
        discovery = db.get(OutdoorConfiguration, 1).discovery
    started, resume = asyncio.Event(), asyncio.Event()

    async def blocked_request(*args):
        started.set()
        await resume.wait()
        return [{"location_id": 999, "name": "Obsolete result"}]

    monkeypatch.setattr(outdoor.provider, operation, blocked_request)
    request = outdoor.search("Other city") if operation == "search" else outdoor.nearby(0, 0)
    worker = asyncio.create_task(request)
    await started.wait()
    with app.state.sessions() as db:
        set_location(db, 2)
    resume.set()
    with pytest.raises(OutdoorBusy):
        await worker
    with app.state.sessions() as db:
        assert db.get(OutdoorConfiguration, 1).discovery == discovery


async def test_location_actions_validate_input_session_and_csrf(app, outdoor, signed_client):
    for query in ("", " ", "x", "x" * 121, 123, True):
        assert (
            await signed_client.post("/api/outdoor/stations/search", json={"query": query})
        ).status_code == 422
    for coordinates in (
        {"latitude": 91, "longitude": 0},
        {"latitude": 0, "longitude": -181},
        {"latitude": True, "longitude": 0},
    ):
        assert (
            await signed_client.post("/api/outdoor/stations/nearby", json=coordinates)
        ).status_code == 422
    assert (
        await signed_client.put("/api/outdoor/location", json={"location_id": 999})
    ).status_code == 422
    assert (
        await signed_client.put("/api/outdoor/location", json={"location_id": True})
    ).status_code == 422
    assert outdoor.provider.calls == []
    signed_client.headers.pop("X-CSRF-Token")
    assert (
        await signed_client.post("/api/outdoor/stations/search", json={"query": "Singapore"})
    ).status_code == 403
    assert (
        await signed_client.post(
            "/api/outdoor/stations/nearby", json={"latitude": 0, "longitude": 0}
        )
    ).status_code == 403
    assert (await signed_client.delete("/api/outdoor/location")).status_code == 403
    signed_client.cookies.clear()
    assert (
        await signed_client.post("/api/outdoor/stations/search", json={"query": "Singapore"})
    ).status_code == 401


async def test_search_failure_preserves_selection_and_shared_cooldown(app, outdoor, signed_client):
    outdoor.provider.search_results = [OutdoorError("rate_limit", 600)]
    response = await signed_client.post("/api/outdoor/stations/search", json={"query": "Singapore"})
    assert response.status_code == 503
    with app.state.sessions() as db:
        assert selected_location(db) == 1
        assert db.get(OutdoorConfiguration, 1).lease_token is None
    response = await signed_client.post("/api/outdoor/stations/search", json={"query": "Singapore"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 590
    assert (await outdoor.tick())["checked"] == 0
    assert len(outdoor.provider.calls) == 1


async def test_selection_preview_is_immediate_and_never_becomes_history(
    app, outdoor, signed_client
):
    await outdoor.search("Singapore")
    with app.state.sessions() as db:
        set_location(db, 2)
        view = outdoor_view(db, app.state.settings)
        assert view["preview"]["value"] == 42
        assert view["loading"] is True
        assert view["metrics"] == {}
        assert db.scalar(select(func.count()).select_from(OutdoorReading)) == 0
    outdoor.provider.feed_results = [OutdoorError()]
    await outdoor.tick()
    failed = (await signed_client.get("/api/outdoor")).json()
    assert failed["loading"] is False
    assert failed["preview"]["value"] == 42
    assert failed["error"]
    assert failed["last_success_at"] is None
    due(app)
    await outdoor.tick()
    updated = (await signed_client.get("/api/outdoor")).json()
    assert updated["preview"] is None
    assert updated["loading"] is False
    assert updated["metrics"]["aqi"]["measured_at"] == date(
        outdoor.provider.readings[0].recorded_at
    )


async def test_reselecting_cached_station_keeps_readings_and_accepts_zero_preview(app, outdoor):
    await outdoor.tick()
    await outdoor.search("Singapore")
    with app.state.sessions() as db:
        before = dict(db.get(OutdoorStation, 1).readings)
        configuration = db.get(OutdoorConfiguration, 1)
        configuration.discovery = {"stations": [{"location_id": 1, "name": "Station", "aqi": 0}]}
        db.commit()
        set_location(db, 1)
        view = outdoor_view(db, app.state.settings)
        assert view["preview"]["value"] == 0
        assert view["loading"] is True
        assert db.get(OutdoorStation, 1).readings == before


async def test_empty_and_geolocation_search_do_not_change_selected_station(outdoor, signed_client):
    outdoor.provider.search_results = [[]]
    assert (
        await signed_client.post("/api/outdoor/stations/search", json={"query": "Unknown"})
    ).json() == {"stations": []}
    response = await signed_client.post(
        "/api/outdoor/stations/nearby", json={"latitude": 1.35, "longitude": 103.8}
    )
    assert response.json()["stations"][0]["location_id"] == 1
    assert outdoor.provider.calls[-1] == ("nearby", 1.35, 103.8)


async def test_scheduler_collects_outdoor_without_devices(app, outdoor):
    result = await app.state.poller.tick()
    assert result["outdoor_checked"] == 1
    assert outdoor.provider.calls == [("feed", 1)]


def test_api_token_has_no_location_environment_and_stays_secret(app, monkeypatch):
    values = app.state.settings.model_dump()
    values.update(aqicn_api_token="private-token")
    monkeypatch.setenv("OPENAQ_API_KEY", "old-key")
    monkeypatch.setenv("OPENAQ_LOCATION_ID", "999")
    settings = Settings(_env_file=None, **values)
    assert not hasattr(settings, "openaq_api_key")
    assert "private-token" not in repr(settings)
    values.update(outdoor_poll_interval_seconds=0)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, **values)
    assert "private-token" not in str(exc.value)
