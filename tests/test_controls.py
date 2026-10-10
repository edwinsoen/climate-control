import asyncio
import time

import pytest
from sqlalchemy import select

from climate_control.db import Account, Device
from climate_control.providers.base import DeviceCommand, DeviceSnapshot, PollResult, ProviderError


async def test_control_confirmed_state_keeps_other_devices(app, signed_client, controlled_device):
    with app.state.sessions() as db:
        db.add(
            Device(
                account_id=controlled_device["account_id"],
                provider_id="another",
                name="Other purifier",
                model="Core 200S",
            )
        )
        db.commit()
    calls = len(app.state.poller.provider.calls)
    response = await signed_client.post(
        f"/api/devices/{controlled_device['id']}/control", json={"fan_level": 4}
    )
    assert response.status_code == 200
    assert response.json()["state"]["mode"] == "manual"
    assert response.json()["state"]["fan_level"] == 4
    assert len(app.state.poller.provider.calls) == calls
    assert len((await signed_client.get("/api/devices")).json()) == 2
    again = await app.state.poller.control(controlled_device["id"], DeviceCommand(power=False))
    assert again.status == "cooldown"
    assert again.retry_after > 0
    assert len(app.state.poller.provider.control_calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"power": True, "mode": "auto"},
        {"power": "on"},
        {"fan_level": True},
        {"fan_level": 9},
        {"mode": "manual"},
        {"mode": "turbo"},
        {"fan_level": 2.5},
        {"power": False, "extra": 1},
    ],
)
async def test_invalid_commands_make_no_vendor_calls(
    app, signed_client, controlled_device, payload
):
    response = await signed_client.post(
        f"/api/devices/{controlled_device['id']}/control", json=payload
    )
    assert response.status_code == 422
    assert not app.state.poller.provider.control_calls


async def test_authentication_csrf_and_missing_device(client, signed_client, controlled_device):
    url = f"/api/devices/{controlled_device['id']}/control"
    csrf = signed_client.headers.pop("X-CSRF-Token")
    assert (await signed_client.post(url, json={"power": False})).status_code == 403
    signed_client.headers["X-CSRF-Token"] = csrf
    assert (
        await signed_client.post("/api/devices/missing/control", json={"power": False})
    ).status_code == 404
    signed_client.cookies.clear()
    assert (await client.post(url, json={"power": False})).status_code == 401


@pytest.mark.parametrize("state", [{"connectivity": "offline"}, {"power": "off"}])
async def test_offline_or_off_fan_rejected(app, signed_client, controlled_device, state):
    with app.state.sessions() as db:
        device = db.get(Device, controlled_device["id"])
        device.state = {**device.state, **state}
        db.commit()
    response = await signed_client.post(
        f"/api/devices/{controlled_device['id']}/control", json={"mode": "sleep"}
    )
    assert response.status_code == 409
    assert not app.state.poller.provider.control_calls


@pytest.mark.parametrize("kind", ["rate_limit", "auth", "control_failed"])
async def test_failed_control_keeps_readings_and_respects_backoff(
    app, signed_client, controlled_device, kind
):
    app.state.poller.provider.control_results.append(ProviderError(kind, retry_after=600))
    url = f"/api/devices/{controlled_device['id']}/control"
    response = await signed_client.post(url, json={"mode": "sleep"})
    assert response.status_code == (429 if kind == "rate_limit" else 502)
    current = (await signed_client.get("/api/devices")).json()[0]
    assert current["state"] == controlled_device["state"]
    assert current["retrieved_at"] == controlled_device["retrieved_at"]
    with app.state.sessions() as db:
        account = db.get(Account, controlled_device["account_id"])
        assert account.lease_token is None
        if kind == "rate_limit":
            assert account.next_poll_at > time.time() + 590
            assert int(response.headers["Retry-After"]) >= 590
        if kind == "auth":
            assert account.tokens is None
    again = await app.state.poller.control(controlled_device["id"], DeviceCommand(power=False))
    assert again.status in {"auth_error", "cooldown"}
    if kind == "rate_limit":
        # The request queue must return immediately during vendor backoff.
        response = await asyncio.wait_for(signed_client.post(url, json={"power": False}), 1)
        assert response.status_code == 429
    assert len(app.state.poller.provider.control_calls) == 1


async def test_control_blocks_overlapping_provider_calls_and_polling(
    app, signed_client, controlled_device, monkeypatch
):
    started, release = asyncio.Event(), asyncio.Event()
    original = app.state.poller.provider.control

    async def blocked(*args):
        started.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(app.state.poller.provider, "control", blocked)
    url = f"/api/devices/{controlled_device['id']}/control"
    first = asyncio.create_task(signed_client.post(url, json={"mode": "auto"}))
    await started.wait()
    try:
        assert (
            await app.state.poller.control(controlled_device["id"], DeviceCommand(power=False))
        ).status == "busy"
        assert (
            await app.state.poller.refresh(controlled_device["account_id"])
        ).status == "cooldown"
    finally:
        release.set()
    assert (await first).status_code == 200


async def test_another_device_command_waits_for_account_lease(
    app, signed_client, controlled_device, monkeypatch
):
    monkeypatch.setattr("climate_control.polling.CONTROL_COOLDOWN_SECONDS", 0)
    with app.state.sessions() as db:
        first_provider_id = db.get(Device, controlled_device["id"]).provider_id
        other = Device(
            account_id=controlled_device["account_id"],
            provider_id="other",
            name="Other purifier",
            model="Core 400S",
            supported=True,
            controls=controlled_device["controls"],
            state=controlled_device["state"],
        )
        db.add(other)
        db.commit()
        other_id = other.id
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked(credentials, tokens, provider_id, command):
        calls.append(provider_id)
        if len(calls) == 1:
            started.set()
            await release.wait()
        return PollResult(
            [
                DeviceSnapshot(
                    provider_id=provider_id,
                    name="Purifier",
                    model="Core 400S",
                    controls=controlled_device["controls"],
                    state={**controlled_device["state"], "mode": command.mode},
                    retrieved_at=time.time(),
                )
            ]
        )

    monkeypatch.setattr(app.state.poller.provider, "control", blocked)
    first = asyncio.create_task(
        signed_client.post(f"/api/devices/{controlled_device['id']}/control", json={"mode": "auto"})
    )
    await started.wait()
    second = asyncio.create_task(
        signed_client.post(f"/api/devices/{other_id}/control", json={"mode": "sleep"})
    )
    try:
        await asyncio.sleep(0.03)
        assert not second.done()
        assert len(calls) == 1
    finally:
        release.set()
    responses = await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["state"]["mode"] for response in responses] == ["auto", "sleep"]
    assert calls == [first_provider_id, "other"]


async def test_queued_command_obeys_cooldown_after_lease_is_free(
    app, signed_client, controlled_device, monkeypatch
):
    monkeypatch.setattr("climate_control.polling.CONTROL_COOLDOWN_SECONDS", 0.5)
    url = f"/api/devices/{controlled_device['id']}/control"
    assert (await signed_client.post(url, json={"mode": "auto"})).status_code == 200
    with app.state.sessions() as db:
        account = db.get(Account, controlled_device["account_id"])
        assert account.lease_token is None
        deadline = account.next_control_at
    provider = app.state.poller.provider
    original = provider.control
    started_at = []

    async def recorded(*args):
        started_at.append(time.time())
        return await original(*args)

    monkeypatch.setattr(provider, "control", recorded)
    second = asyncio.create_task(signed_client.post(url, json={"mode": "sleep"}))
    await asyncio.sleep(0.03)
    assert not second.done()
    assert not started_at
    assert (await asyncio.wait_for(second, 2)).status_code == 200
    assert started_at[0] >= deadline


async def test_removed_account_cannot_be_recreated_by_command(
    app, signed_client, controlled_device, monkeypatch
):
    async def removed(*args):
        with app.state.sessions() as db:
            db.delete(db.get(Account, controlled_device["account_id"]))
            db.commit()
        return PollResult([DeviceSnapshot("same-provider-id", "Purifier", "Core400S")])

    monkeypatch.setattr(app.state.poller.provider, "control", removed)
    response = await signed_client.post(
        f"/api/devices/{controlled_device['id']}/control", json={"power": False}
    )
    assert response.status_code == 404
    with app.state.sessions() as db:
        assert not list(db.scalars(select(Device)))
