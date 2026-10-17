import asyncio
import time

import pytest
from sqlalchemy import select
from test_automation import setup_rules

from climate_control.agent.worker import PlannerJobTrigger
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


@pytest.mark.parametrize(
    "threshold,pm25,pm10,expected",
    [
        ("who_2021_annual", 5, 15, False),
        ("who_2021_annual", 6, 15, True),
        ("who_2021_annual", 5, 16, True),
        ("who_2021_24h", 15, 45, False),
        ("who_2021_24h", 16, 45, True),
        ("who_2021_24h", 15, 46, True),
        ("who_2005_annual", 10, 20, False),
        ("who_2005_annual", 11, 20, True),
        ("who_2005_annual", 10, 21, True),
        ("who_2005_24h", 25, 50, False),
        ("who_2005_24h", 26, 50, True),
        ("who_2005_24h", 25, 51, True),
    ],
)
def test_who_warning_threshold_evaluation(threshold, pm25, pm10, expected):
    from climate_control.display import should_display_warning

    assert (
        should_display_warning(
            ["power", "pm25", "pm10", "air_quality"],
            {"pm25": pm25, "pm10": pm10, "air_quality": "excellent"},
            threshold,
        )
        is expected
    )
    assert (
        should_display_warning(
            ["power", "air_quality"],
            {"air_quality": "moderate" if expected else "good"},
            threshold,
        )
        is expected
    )


async def test_warnings_only_actuates_on_new_readings_and_syncs_external_changes(
    app, signed_client, connected
):
    from climate_control.providers.base import DeviceCommand

    provider = app.state.poller.provider
    device_id = (await signed_client.get("/api/devices")).json()[0]["id"]
    await signed_client.put(
        f"/api/devices/{device_id}/display-settings",
        json={"mode": "warnings_only"},
    )
    assert not provider.control_calls

    # New reading with PM2.5 = 8 (<= 15 for default who_2021_24h) turns display off.
    due(app, connected["id"])
    await app.state.poller.refresh(connected["id"])
    assert len(provider.control_calls) == 1
    assert provider.control_calls[-1][-1] == DeviceCommand(display=False)
    device = (await signed_client.get("/api/devices")).json()[0]
    assert device["state"]["display"] == "off"
    assert device["display"]["mode"] == "warnings_only"

    # Subsequent reading still below threshold does not re-send display command.
    due(app, connected["id"])
    await app.state.poller.refresh(connected["id"])
    assert len(provider.control_calls) == 1

    # Reading exceeds threshold while device is powered off -> does not actuate.
    provider.states["home@example.com"].update(power="off", pm25=22)
    due(app, connected["id"])
    await app.state.poller.refresh(connected["id"])
    assert len(provider.control_calls) == 1

    # Device is powered on and reading exceeds threshold (22 > 15) -> turns display on.
    provider.states["home@example.com"].update(power="on", pm25=22)
    due(app, connected["id"])
    await app.state.poller.refresh(connected["id"])
    assert len(provider.control_calls) == 2
    assert provider.control_calls[-1][-1] == DeviceCommand(display=True)
    device = (await signed_client.get("/api/devices")).json()[0]
    assert device["state"]["display"] == "on"
    assert device["display"]["mode"] == "warnings_only"

    # Switching from "warnings_only" to "off" while PM2.5 > threshold turns display off
    # with one command, sets mode to "manual", and does not re-trigger warning actuation.
    with app.state.sessions() as db:
        db.get(Account, connected["id"]).next_control_at = 0
        db.commit()
    resp = await signed_client.put(
        f"/api/devices/{device_id}/display-settings",
        json={"mode": "off"},
    )
    assert resp.status_code == 200
    assert len(provider.control_calls) == 3
    assert provider.control_calls[-1][-1] == DeviceCommand(display=False)
    device = resp.json()
    assert device["state"]["display"] == "off"
    assert device["display"]["mode"] == "manual"

    # In "manual" mode, an external toggle updates state.display without sending commands.
    provider.states["home@example.com"]["display"] = "on"
    due(app, connected["id"])
    await app.state.poller.refresh(connected["id"])
    assert len(provider.control_calls) == 3
    device = (await signed_client.get("/api/devices")).json()[0]
    assert device["state"]["display"] == "on"
    assert device["display"]["mode"] == "manual"


async def test_scheduled_tick_tolerates_jitter_but_not_for_cooldown_or_rate_limit(app, connected):
    with app.state.sessions() as db:
        db.get(Account, connected["id"]).next_poll_at = time.time() + 3
        db.commit()
    assert (await app.state.poller.refresh(connected["id"])).status == "cooldown"
    assert (await app.state.poller.tick())["refreshed"] == 1
    with app.state.sessions() as db:
        account = db.get(Account, connected["id"])
        account.status, account.next_poll_at = "rate_limited", time.time() + 3
        db.commit()
    assert (await app.state.poller.tick())["checked"] == 0


async def test_tick_starts_planner_after_failure_but_not_after_recent_start(
    app, signed_client, controlled_device, monkeypatch
):
    import google.auth
    from google.auth.transport import requests

    posts, failures = [], [True]

    class Session:
        def __init__(self, credentials):
            pass

        def post(self, url, **kwargs):
            posts.append(url)
            return self

        def raise_for_status(self):
            if failures and failures.pop():
                raise RuntimeError("unavailable")

    monkeypatch.setattr(google.auth, "default", lambda scopes: (None, None))
    monkeypatch.setattr(requests, "AuthorizedSession", Session)
    trigger = PlannerJobTrigger("projects/p/locations/r/jobs/planner")
    app.state.planning_trigger = app.state.poller.planning_trigger = trigger
    await app.state.poller.tick()
    assert posts == []
    path = await setup_rules(signed_client, controlled_device)
    response = await signed_client.put(
        path, json={"base_revision": 1, "generation": 2, "owner": "agent", "enabled": True}
    )
    assert response.status_code == 200
    assert len(posts) == 1  # The user's request failed to start the job.
    await app.state.poller.tick()
    await app.state.poller.tick()
    assert len(posts) == 2
