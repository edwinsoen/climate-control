import asyncio
import time

import pytest
from sqlalchemy import select

from climate_control.db import Account, Device
from climate_control.providers.base import DeviceSnapshot, PollResult, ProviderError


def due(app, account_id):
    with app.state.sessions() as db:
        db.get(Account, account_id).next_poll_at = 0
        db.commit()


@pytest.mark.parametrize("kind,delay", [("rate_limit", 300), ("temporary", 60), ("auth", 0)])
async def test_failure_preserves_readings_and_applies_backoff(app, connected, kind, delay):
    due(app, connected["id"])
    app.state.poller.provider.results.append(ProviderError(kind))
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        account = db.get(Account, connected["id"])
        assert account.error is not None
        assert db.scalars(select(Device)).one().state["pm25"] == 8
        if delay:
            assert delay - 2 <= account.next_poll_at - time.time() <= delay


async def test_escalating_rate_limit_and_recovery(app, connected):
    for delay in (300, 600, 1200, 1800, 1800):
        due(app, connected["id"])
        app.state.poller.provider.results.append(ProviderError("rate_limit"))
        await app.state.poller.refresh(connected["id"])
        with app.state.sessions() as db:
            account = db.get(Account, connected["id"])
            assert delay - 2 < account.next_poll_at - time.time() <= delay
    due(app, connected["id"])
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        account = db.get(Account, connected["id"])
        assert account.rate_limit_count == 0
        assert account.status == "connected"


async def test_retry_after_is_honored(app, connected):
    due(app, connected["id"])
    app.state.poller.provider.results.append(ProviderError("rate_limit", retry_after=3600))
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        assert db.get(Account, connected["id"]).next_poll_at > time.time() + 3598


async def test_simultaneous_refreshes_only_poll_once(app, connected):
    due(app, connected["id"])
    calls = len(app.state.poller.provider.calls)
    results = await asyncio.gather(*(app.state.poller.refresh(connected["id"]) for _ in range(5)))
    assert len(app.state.poller.provider.calls) == calls + 1
    assert sum(result.status == "refreshed" for result in results) == 1


async def test_partial_poll_retains_other_devices(app, connected):
    due(app, connected["id"])
    app.state.poller.provider.results.append(
        PollResult(
            [
                DeviceSnapshot(
                    "new-id", "Bedroom", "Vital 200S", state={"pm25": 12}, retrieved_at=time.time()
                ),
            ],
            failure=ProviderError(),
        )
    )
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        assert len(list(db.scalars(select(Device)))) == 2
    due(app, connected["id"])
    app.state.poller.provider.results.append(PollResult([]))
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        assert list(db.scalars(select(Device))) == []


async def test_fencing_rejects_expired_worker_results(app, connected):
    due(app, connected["id"])
    account, _ = app.state.poller.claim(connected["id"])
    with app.state.sessions() as db:
        db.get(Account, account.id).lease_token = "another-worker"
        db.commit()
    app.state.poller.finish(account, PollResult([]))
    with app.state.sessions() as db:
        assert db.scalars(select(Device)).one().state["pm25"] == 8
        assert db.get(Account, account.id).lease_token == "another-worker"


async def test_offline_preserves_previous_readings(app, connected):
    due(app, connected["id"])
    app.state.poller.provider.results.append(
        PollResult(
            [
                DeviceSnapshot(
                    "same-provider-id",
                    "Home purifier",
                    "Core 400S",
                    state={"connectivity": "offline"},
                    error="Offline",
                ),
            ]
        )
    )
    await app.state.poller.refresh(connected["id"])
    with app.state.sessions() as db:
        device = db.scalars(select(Device)).one()
        assert device.state["pm25"] == 8
        assert device.state["connectivity"] == "offline"
