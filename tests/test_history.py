import math
import time

import pytest
from conftest import account_input
from sqlalchemy import select
from test_polling import due

from climate_control.db import Account, Device, DeviceReading
from climate_control.history import RETENTION_SECONDS, history_view
from climate_control.providers.base import DeviceSnapshot, PollResult, ProviderError


async def history(signed_client):
    device = (await signed_client.get("/api/devices")).json()[0]
    return await signed_client.get(f"/api/devices/{device['id']}/history")


async def test_history_records_fresh_polls_without_extra_provider_calls(
    app, connected, signed_client
):
    response = await history(signed_client)
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "recorded"
    assert data["metrics"]["pm25"][0]["value"] == 8
    assert data["metrics"]["air_quality"][0]["value"] == 2
    assert len(app.state.poller.provider.calls) == 1
    with app.state.sessions() as db:
        assert (
            db.scalars(select(DeviceReading)).one().device_id == db.scalars(select(Device)).one().id
        )


@pytest.mark.parametrize("kind", ["offline", "duplicate", "older", "invalid", "failure", "error"])
async def test_history_does_not_repeat_or_invent_readings(app, connected, kind):
    with app.state.sessions() as db:
        device = db.scalars(select(Device)).one()
        recorded_at = device.retrieved_at
    due(app, connected["id"])
    if kind == "failure":
        app.state.poller.provider.results.append(ProviderError())
    else:
        app.state.poller.provider.results.append(
            PollResult(
                [
                    DeviceSnapshot(
                        provider_id="same-provider-id",
                        name="Home purifier",
                        model="Core 400S",
                        capabilities=["pm25"],
                        state={
                            "pm25": math.nan if kind == "invalid" else 10,
                            "connectivity": "offline" if kind == "offline" else "online",
                        },
                        retrieved_at=(
                            recorded_at
                            if kind == "duplicate"
                            else recorded_at - 1
                            if kind == "older"
                            else time.time()
                        ),
                        error="Read failed" if kind == "error" else None,
                    )
                ]
            )
        )
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        readings = list(db.scalars(select(DeviceReading)))
        assert len(readings) == 1
        assert readings[0].pm25 == 8
        if kind == "invalid":
            assert db.scalars(select(Device)).one().state["pm25"] is None


async def test_history_aggregation_gaps_ranges_and_quality(app, connected, signed_client):
    current = time.time()
    interval = 240
    bucket_start = math.floor((current - 3600) / interval) * interval
    with app.state.sessions() as db:
        device = db.scalars(select(Device)).one()
        db.add_all(
            [
                DeviceReading(
                    device_id=device.id, recorded_at=bucket_start + 10, pm25=0, air_quality=1
                ),
                DeviceReading(
                    device_id=device.id, recorded_at=bucket_start + 70, pm25=100, air_quality=4
                ),
                DeviceReading(
                    device_id=device.id, recorded_at=bucket_start + interval * 3 + 10, pm25=20
                ),
            ]
        )
        db.commit()
    data = (await history(signed_client)).json()
    points = data["metrics"]["pm25"]
    assert points[0]["value"] == 50
    assert points[0]["min"] == 0
    assert points[0]["max"] == 100
    assert points[0]["count"] == 2
    assert points[1]["recorded_at"] - points[0]["recorded_at"] == interval * 3
    assert data["metrics"]["air_quality"][0]["value"] == 4


async def test_retention_and_account_deletion_clean_up_history(app, connected, signed_client):
    with app.state.sessions() as db:
        device = db.scalars(select(Device)).one()
        db.add(
            DeviceReading(
                device_id=device.id, recorded_at=time.time() - RETENTION_SECONDS - 1, pm25=42
            )
        )
        db.commit()
    await app.state.poller.tick()
    with app.state.sessions() as db:
        assert all(
            reading.recorded_at > time.time() - RETENTION_SECONDS
            for reading in db.scalars(select(DeviceReading))
        )
    assert (await signed_client.delete(f"/api/accounts/{connected['id']}")).status_code == 200
    with app.state.sessions() as db:
        assert list(db.scalars(select(DeviceReading))) == []


async def test_history_access_period_and_device_isolation(client, signed_client, app, connected):
    with app.state.sessions() as db:
        device_id = db.scalars(select(Device)).one().id
    response = await signed_client.get(f"/api/devices/{device_id}/history?hours=720")
    assert response.status_code == 200
    assert (await signed_client.get(f"/api/devices/{device_id}/history?hours=1")).status_code == 422
    assert (await signed_client.get("/api/devices/missing/history")).status_code == 404
    response = await signed_client.post(
        "/api/accounts", json=account_input("Other", "other@example.com")
    )
    assert response.status_code == 201
    with app.state.sessions() as db:
        other = db.scalar(select(Device).where(Device.account_id == response.json()["id"]))
        db.add(DeviceReading(device_id=other.id, recorded_at=time.time() - 500, pm25=9999))
        db.commit()
    points = (await signed_client.get(f"/api/devices/{device_id}/history")).json()["metrics"][
        "pm25"
    ]
    assert all(point["max"] != 9999 for point in points)
    client.cookies.clear()
    assert (await client.get(f"/api/devices/{device_id}/history")).status_code == 401


async def test_settings_and_sensor_values_share_observations_including_off_and_sensorless(
    app, connected, signed_client
):
    provider = app.state.poller.provider
    for state in (
        {"power": "on", "mode": "auto", "fan_level": 3, "reported_fan_level": 3, "pm25": 21},
        {"power": "off", "mode": "manual", "fan_level": 4, "reported_fan_level": 0, "pm25": None},
    ):
        due(app, connected["id"])
        provider.states["home@example.com"] = state
        await app.state.poller.refresh(connected["id"])
    device = (await signed_client.get("/api/devices")).json()[0]
    url = f"/api/devices/{device['id']}/history?raw=true"
    observations = (await signed_client.get(url)).json()["observations"]
    assert [
        (row["power"], row["mode"], row["fan_level"], row["reported_fan_level"], row["pm25"])
        for row in observations
    ] == [
        ("on", "manual", 2, None, 8),
        ("on", "auto", 3, 3, 21),
        ("off", "manual", 4, 0, None),
    ]
    # Raw samples are paginated without losing changes inside a chart bucket.
    first = (await signed_client.get(f"{url}&limit=2")).json()
    second = (await signed_client.get(f"{url}&limit=2&after={first['next_after']}")).json()
    assert first["observations"] + second["observations"] == observations
    assert second["next_after"] is None
    assert len(provider.calls) == 3
    assert (await signed_client.get(f"{url}&after=nan")).status_code == 422
    assert (await signed_client.get(f"{url}&limit=0")).status_code == 422


async def test_setting_bands_keep_exact_transitions_and_observation_gaps(app, connected):
    current = time.time()
    with app.state.sessions() as db:
        device = db.scalars(select(Device)).one()
        for seconds, mode, fan in [
            (600, "manual", 2),
            (540, "manual", 2),
            (520, "auto", 3),
            (500, "manual", 4),
            (200, "sleep", 1),
        ]:
            db.add(
                DeviceReading(
                    device_id=device.id,
                    recorded_at=current - seconds,
                    power="on",
                    mode=mode,
                    fan_level=fan,
                    pm25=10,
                )
            )
        db.flush()
        bands = history_view(db, device.id, 24, current)["settings"]
        assert [
            (band["recorded_at"] - current, band["end_at"] - current, band["mode"])
            for band in bands
        ] == [
            (-600, -520, "manual"),
            (-520, -500, "auto"),
            (-500, -500, "manual"),
            (-200, -200, "sleep"),
            (device.retrieved_at - current, device.retrieved_at - current, "manual"),
        ]


async def test_history_records_observed_control_state_and_never_requested_or_failed_settings(
    app, signed_client, controlled_device
):
    provider = app.state.poller.provider
    observed = provider.snapshot({"label": "Home", "email": "home@example.com"})
    provider.control_results.append(PollResult(observed.devices, control_confirmed=False))
    url = f"/api/devices/{controlled_device['id']}"
    response = await signed_client.post(f"{url}/control", json={"mode": "auto"})
    assert response.json()["control_confirmed"] is False
    rows = (await signed_client.get(f"{url}/history?raw=true")).json()["observations"]
    assert [row["mode"] for row in rows] == ["manual", "manual"]

    for command in ({"fan_level": 4}, {"power": False}):
        with app.state.sessions() as db:
            db.get(Account, controlled_device["account_id"]).next_control_at = 0
            db.commit()
        assert (await signed_client.post(f"{url}/control", json=command)).status_code == 200
    with app.state.sessions() as db:
        db.get(Account, controlled_device["account_id"]).next_control_at = 0
        db.commit()
    provider.control_results.append(ProviderError("control_failed"))
    assert (await signed_client.post(f"{url}/control", json={"power": True})).status_code == 502
    rows = (await signed_client.get(f"{url}/history?raw=true")).json()["observations"]
    assert [(row["power"], row["fan_level"]) for row in rows] == [
        ("on", 2),
        ("on", 2),
        ("on", 4),
        ("off", 4),
    ]


async def test_invalid_setting_fields_are_unknown_without_discarding_valid_pm(app, connected):
    provider = app.state.poller.provider
    due(app, connected["id"])
    provider.states["home@example.com"] = {
        "power": [],
        "mode": "unknown",
        "fan_level": True,
        "reported_fan_level": -1,
        "pm25": 12,
    }
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        row = db.scalars(select(DeviceReading).order_by(DeviceReading.recorded_at.desc())).first()
        assert row.pm25 == 12
        assert (row.power, row.mode, row.fan_level, row.reported_fan_level) == (
            None,
            None,
            None,
            None,
        )


async def test_sensorless_and_off_observations_record_settings(app, connected):
    provider = app.state.poller.provider
    snapshot = provider.snapshot({"label": "Home", "email": "home@example.com"}).devices[0]
    snapshot.capabilities = ["power", "mode", "fan_level"]
    snapshot.state = {
        "connectivity": "online",
        "power": "off",
        "mode": "auto",
        "fan_level": 2,
        "reported_fan_level": 0,
    }
    provider.results.append(PollResult([snapshot]))
    due(app, connected["id"])
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        row = db.scalars(select(DeviceReading).order_by(DeviceReading.recorded_at.desc())).first()
        assert (row.power, row.mode, row.fan_level, row.reported_fan_level) == ("off", "auto", 2, 0)
        assert (row.pm25, row.pm1, row.pm10, row.air_quality) == (None, None, None, None)


async def test_unknown_settings_interrupt_band_without_losing_air_quality(app, connected):
    current = time.time()
    with app.state.sessions() as db:
        device = db.scalars(select(Device)).one()
        db.add_all(
            [
                DeviceReading(
                    device_id=device.id, recorded_at=current - 600, pm25=4, power="on", mode="auto"
                ),
                DeviceReading(device_id=device.id, recorded_at=current - 540, pm25=6),
                DeviceReading(
                    device_id=device.id, recorded_at=current - 480, pm25=8, power="on", mode="auto"
                ),
            ]
        )
        db.flush()
        bands = history_view(db, device.id, 24, current)["settings"]
        assert [
            (band["recorded_at"] - current, band["end_at"] - current) for band in bands[:2]
        ] == [
            (-600, -540),
            (-480, -480),
        ]
