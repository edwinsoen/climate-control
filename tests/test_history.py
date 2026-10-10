import math
import time

import pytest
from conftest import account_input
from sqlalchemy import select
from test_polling import due

from climate_control.db import Device, DeviceReading
from climate_control.history import RETENTION_SECONDS
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


@pytest.mark.parametrize("kind", ["offline", "duplicate", "invalid", "failure"])
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
                        retrieved_at=recorded_at if kind == "duplicate" else time.time(),
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
    due(app, connected["id"])
    await app.state.poller.refresh(connected["id"])
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
