import asyncio
import copy
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import delete

from climate_control.automation import collect_inputs, reconsider
from climate_control.db import Account, Automation, Device, DeviceReading, PlanningJob
from climate_control.rules import Ruleset, evaluate


def rule_document(device_id=None):
    return {
        "version": 1,
        "timezone": "Asia/Singapore",
        "consecutive_readings": 1,
        "fallback": {"power": "on", "fan": 1},
        "rules": [
            {
                "id": "clean",
                "explanation": "Clean elevated indoor readings.",
                "conditions": (
                    [
                        {
                            "source": {"kind": "device", "device_id": device_id, "metric": "pm25"},
                            "lower": 5,
                        }
                    ]
                    if device_id
                    else []
                ),
                "action": {"power": "on", "fan": 3},
            }
        ],
    }


async def setup_rules(client, device, document=None):
    path = f"/api/devices/{device['id']}/automation"
    result = await client.put(
        path + "/ruleset",
        json={
            "base_revision": 0,
            "generation": 0,
            "reason": "Test rules",
            "ruleset": document or rule_document(device["id"]),
        },
    )
    assert result.status_code == 200, result.text
    result = await client.put(
        path,
        json={
            "base_revision": 1,
            "generation": 1,
            "owner": "manual",
            "enabled": True,
        },
    )
    assert result.status_code == 200, result.text
    return path


def clear_spacing(app, device):
    with app.state.sessions() as db:
        account = db.get(Account, device["account_id"])
        account.next_control_at = 0
        db.commit()


def test_rules_priority_bounds_missing_and_overnight():
    doc = rule_document("target")
    doc["rules"].insert(
        0,
        {
            "id": "outdoor",
            "explanation": "Outdoor assistance",
            "conditions": [{"source": {"kind": "outdoor", "metric": "aqi"}, "lower": 100}],
            "action": {"power": "on", "fan": 4},
        },
    )
    rules = Ruleset.model_validate(doc)
    inputs = {
        "outdoor:selected:aqi": {"value": None, "available": False},
        "device:target:pm25": {"value": 5, "available": True},
    }
    assert evaluate(rules, inputs, time.time())["rule_id"] == "clean"
    inputs["outdoor:selected:aqi"] = {"value": 100, "available": True}
    assert evaluate(rules, inputs, time.time())["rule_id"] == "outdoor"
    doc["rules"][0]["windows"] = [{"days": [0], "start": "22:00", "end": "06:00"}]
    rules = Ruleset.model_validate(doc)

    def at(day, hour):
        return datetime(2026, 10, day, hour, tzinfo=ZoneInfo("Asia/Singapore")).timestamp()

    assert evaluate(rules, inputs, at(13, 5))["rule_id"] == "outdoor"  # Tuesday, Monday window
    assert evaluate(rules, inputs, at(13, 6))["rule_id"] == "clean"
    doc["rules"][1]["conditions"][0]["upper"] = 5
    with pytest.raises(ValidationError):
        Ruleset.model_validate(doc)


@pytest.mark.parametrize(
    "change",
    [
        {"timezone": "invalid/zone"},
        {"consecutive_readings": True},
        {"fallback": {"power": "off", "fan": 1}},
        {"unexpected": "code"},
    ],
)
def test_strict_schema(change):
    with pytest.raises(ValidationError):
        Ruleset.model_validate({**rule_document(), **change})


async def test_executes_and_records_observed_control(app, signed_client, controlled_device):
    path = await setup_rules(signed_client, controlled_device)
    await app.state.poller.automation.tick()
    assert app.state.poller.provider.control_calls[-1][-1].fan_level == 3
    events_before_confirmation = (await signed_client.get(path + "/events")).json()
    await app.state.poller.automation.tick()
    assert len(app.state.poller.provider.control_calls) == 1
    events = (await signed_client.get(path + "/events")).json()
    assert events == events_before_confirmation
    assert any(e.get("intent", {}).get("fan_level") == 3 for e in events)
    assert any(e.get("confirmed") is True for e in events)


@pytest.mark.parametrize(
    "action,observed",
    [
        ({"power": "on", "fan": 2}, {}),
        ({"power": "on", "fan": "auto"}, {"mode": "auto"}),
        ({"power": "off"}, {"power": "off"}),
        ({"power": "hold"}, {}),
    ],
)
async def test_noop_decisions_update_status_without_activity(
    app, signed_client, controlled_device, action, observed
):
    device_id = controlled_device["id"]
    document = rule_document(device_id)
    document["rules"][0]["action"] = action
    higher = copy.deepcopy(document["rules"][0])
    higher.update(id="higher", explanation="Higher readings")
    higher["conditions"][0]["lower"] = 10
    document["rules"].insert(0, higher)
    document["fallback"] = {"power": "hold"}
    path = await setup_rules(signed_client, controlled_device, document)
    for pm25, expected_rule in [(8, "clean"), (12, "higher"), (12, "higher"), (0, None)]:
        with app.state.sessions() as db:
            device = db.get(Device, device_id)
            device.state = {**device.state, **observed, "pm25": pm25}
            device.retrieved_at = time.time()
            db.commit()
        await app.state.poller.automation.tick()
        assert (await signed_client.get(path)).json()["summary"]["decision"]["rule_id"] == (
            expected_rule
        )
    assert (await signed_client.get(path + "/events")).json() == []
    assert not app.state.poller.provider.control_calls


async def test_stale_target_and_missing_optional_source(app, signed_client, controlled_device):
    doc = rule_document(controlled_device["id"])
    doc["rules"].insert(
        0,
        {
            "id": "outdoor",
            "explanation": "Outdoor boost",
            "conditions": [{"source": {"kind": "outdoor", "metric": "aqi"}, "lower": 100}],
            "action": {"power": "on", "fan": 4},
        },
    )
    path = await setup_rules(signed_client, controlled_device, doc)
    preview = (await signed_client.post(path + "/preview", json=doc)).json()
    assert preview["decision"]["rule_id"] == "clean"
    with app.state.sessions() as db:
        db.get(Device, controlled_device["id"]).retrieved_at = time.time() - 1000
        db.commit()
    await app.state.poller.automation.tick()
    assert not app.state.poller.provider.control_calls
    assert (await signed_client.get(path)).json()["summary"]["inhibition"] == "target_unavailable"


async def test_new_readings_and_restart(app, signed_client, controlled_device):
    doc = rule_document(controlled_device["id"])
    doc["consecutive_readings"] = 2
    await setup_rules(signed_client, controlled_device, doc)
    await app.state.poller.automation.tick()
    await app.state.poller.automation.tick()
    assert not app.state.poller.provider.control_calls
    await app.state.poller.control(controlled_device["id"], None)
    from climate_control.automation import RuleEngine

    engine = RuleEngine(app.state.sessions, app.state.settings, app.state.poller)
    await engine.tick()
    assert len(app.state.poller.provider.control_calls) == 1


async def test_power_on_sequence(app, signed_client, controlled_device):
    with app.state.sessions() as db:
        device = db.get(Device, controlled_device["id"])
        device.state = {**device.state, "power": "off"}
        db.commit()
    app.state.poller.provider.states["home@example.com"] = {"power": "off"}
    await setup_rules(signed_client, controlled_device, rule_document())
    await app.state.poller.automation.tick()
    assert app.state.poller.provider.control_calls[-1][-1].power is True
    clear_spacing(app, controlled_device)
    await app.state.poller.automation.tick()
    assert app.state.poller.provider.control_calls[-1][-1].fan_level == 3


async def test_manual_pause_fences_and_resume(app, signed_client, controlled_device):
    path = await setup_rules(signed_client, controlled_device)
    result = await signed_client.post(
        f"/api/devices/{controlled_device['id']}/control", json={"fan_level": 1}
    )
    assert result.status_code == 200
    state = (await signed_client.get(path)).json()
    assert 3590 < state["paused_until"] - time.time() <= 3600
    clear_spacing(app, controlled_device)
    await app.state.poller.automation.tick()
    assert len(app.state.poller.provider.control_calls) == 1
    await signed_client.post(path + "/resume")
    await app.state.poller.automation.tick()
    assert app.state.poller.provider.control_calls[-1][-1].fan_level == 3


async def test_agent_leases_takeover_invalid_and_stale_proposals(
    app, signed_client, controlled_device
):
    path = await setup_rules(signed_client, controlled_device)
    result = await signed_client.put(
        path, json={"base_revision": 1, "generation": 2, "owner": "agent", "enabled": True}
    )
    assert result.status_code == 200
    assert result.json()["ruleset"]["rules"][0]["id"] == "clean"
    lease = (await signed_client.post(path + "/jobs/claim")).json()
    assert "lease_token" in lease
    assert (await signed_client.post(path + "/jobs/claim")).status_code == 409
    payload = {
        "request_id": lease["request_id"],
        "lease_token": lease["lease_token"],
        "generation": 3,
        "base_revision": 1,
        "reason": "Agent update",
        "ruleset": rule_document(),
    }
    invalid = copy.deepcopy(payload)
    invalid["ruleset"]["fallback"]["fan"] = 999
    assert (await signed_client.post(path + "/jobs/complete", json=invalid)).status_code == 422
    assert (
        await signed_client.put(
            path + "/ruleset",
            json={k: v for k, v in payload.items() if k not in {"request_id", "lease_token"}},
        )
    ).status_code == 409
    assert (await signed_client.post(path + "/jobs/complete", json=payload)).status_code == 200
    assert (await signed_client.post(path + "/jobs/complete", json=payload)).status_code == 409
    assert (await signed_client.get(path)).json()["revision"] == 2


async def test_concurrent_ticks_dispatch_once(app, signed_client, controlled_device):
    await setup_rules(signed_client, controlled_device)
    await asyncio.gather(app.state.poller.automation.tick(), app.state.poller.automation.tick())
    assert len(app.state.poller.provider.control_calls) == 1


async def test_reconsideration_threshold_reset_and_stalled(app, signed_client, controlled_device):
    device_id = controlled_device["id"]
    doc = rule_document(device_id)
    source = {"kind": "device", "device_id": device_id, "metric": "pm25"}
    doc["triggers"] = [
        {"id": "high", "kind": "above", "source": source, "threshold": 10, "reset": 5},
        {
            "id": "stalled",
            "kind": "not_improving",
            "source": source,
            "threshold": 5,
            "readings": 3,
            "improvement": 2,
        },
    ]
    rules = Ruleset.model_validate(doc)
    now = time.time()
    with app.state.sessions() as db:
        state = db.get(Automation, device_id)
        state.owner, state.next_review_at = "agent", now + 86400
        device = db.get(Device, device_id)
        runtime = {}
        db.execute(delete(DeviceReading).where(DeviceReading.device_id == device_id))
        for offset, value in [(-120, 12), (-60, 11), (-1, 11)]:
            db.add(
                DeviceReading(
                    device_id=device_id,
                    recorded_at=now + offset,
                    pm25=value,
                    power="on",
                    mode="manual",
                    fan_level=2,
                )
            )
        db.flush()
        inputs = collect_inputs(db, rules, app.state.settings, now)
        inputs[source_key := f"device:{device_id}:pm25"]["value"] = 12
        reconsider(db, state, device, rules, inputs, runtime, app.state.settings, now)
        db.flush()
        job = db.get(PlanningJob, device_id)
        assert {r["reason"] for r in job.reasons} == {"trigger:high", "trigger:stalled"}
        request_id = job.request_id
        reconsider(db, state, device, rules, inputs, runtime, app.state.settings, now + 1)
        assert job.request_id == request_id
        assert len(job.reasons) == 2
        inputs[source_key]["value"] = 4
        reconsider(db, state, device, rules, inputs, runtime, app.state.settings, now + 2)
        assert not runtime["triggers"]["high"]["active"]


async def test_pending_command_recovery_and_bounded_retry(app, signed_client, controlled_device):
    path = await setup_rules(signed_client, controlled_device)
    provider = app.state.poller.provider
    # Successful transport with a contradictory observed state is not a confirmation.
    credentials = {"label": "Home", "email": "home@example.com"}
    for attempt in range(3):
        result = provider.snapshot(credentials)
        result.control_confirmed = False
        provider.control_results.append(result)
        clear_spacing(app, controlled_device)
        with app.state.sessions() as db:
            state = db.get(Automation, controlled_device["id"])
            state.runtime = {**state.runtime, "retry_at": 0}
            db.commit()
        await app.state.poller.automation.tick()
        assert len(provider.control_calls) == attempt + 1
        await app.state.poller.automation.tick()
        assert len(provider.control_calls) == attempt + 1
        with app.state.sessions() as db:
            state = db.get(Automation, controlled_device["id"])
            runtime = copy.deepcopy(state.runtime)
            runtime["pending"]["sent_at"] = time.time() - 100
            state.runtime = runtime
            db.commit()
        await app.state.poller.automation.tick()
    assert (await signed_client.get(path)).json()["summary"]["inhibition"] == "retry_limit"
    clear_spacing(app, controlled_device)
    await app.state.poller.automation.tick()
    assert len(provider.control_calls) == 3


async def test_manual_command_wins_inflight_automation(app, signed_client, controlled_device):
    path = await setup_rules(signed_client, controlled_device)
    provider = app.state.poller.provider
    original = provider.control
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(credentials, tokens, provider_id, command):
        if command.fan_level == 3:
            started.set()
            await release.wait()
        return await original(credentials, tokens, provider_id, command)

    provider.control = delayed
    worker = asyncio.create_task(app.state.poller.automation.tick())
    await started.wait()
    manual = asyncio.create_task(
        signed_client.post(f"/api/devices/{controlled_device['id']}/control", json={"fan_level": 1})
    )
    # Yield until the manual request has persisted its pause, before releasing provider I/O.
    for _ in range(100):
        await asyncio.sleep(0.01)
        if (await signed_client.get(path)).json()["paused_until"] > time.time():
            break
    else:
        pytest.fail("Manual pause was not persisted before waiting for provider lease")
    release.set()
    await worker
    clear_spacing(app, controlled_device)
    assert (await manual).status_code == 200
    await app.state.poller.automation.tick()
    assert [call[-1].fan_level for call in provider.control_calls] == [3, 1]
    events = (await signed_client.get(path + "/events")).json()
    assert any(event.get("outcome") == "controlled" for event in events)


async def test_ownership_change_fences_before_dispatch(app, signed_client, controlled_device):
    path = await setup_rules(signed_client, controlled_device)
    original = app.state.poller.control

    async def intercept(device_id, command, **kwargs):
        response = await signed_client.put(
            path,
            json={
                "base_revision": 1,
                "generation": 2,
                "owner": "agent",
                "enabled": True,
            },
        )
        assert response.status_code == 200
        return await original(device_id, command, **kwargs)

    app.state.poller.control = intercept
    await app.state.poller.automation.tick()
    assert not app.state.poller.provider.control_calls


async def test_job_expiration_failure_and_takeover(app, signed_client, controlled_device):
    path = await setup_rules(signed_client, controlled_device)
    await signed_client.put(
        path,
        json={
            "base_revision": 1,
            "generation": 2,
            "owner": "agent",
            "enabled": True,
        },
    )
    first = (await signed_client.post(path + "/jobs/claim")).json()
    with app.state.sessions() as db:
        db.get(PlanningJob, controlled_device["id"]).lease_until = 0
        db.commit()
    second = (await signed_client.post(path + "/jobs/claim")).json()
    assert first["lease_token"] != second["lease_token"]

    def lease(job):
        return {key: job[key] for key in ("request_id", "lease_token")}

    assert (await signed_client.post(path + "/jobs/renew", json=lease(first))).status_code == 409
    assert (await signed_client.post(path + "/jobs/fail", json=lease(second))).status_code == 200
    assert (await signed_client.post(path + "/jobs/claim")).status_code == 409
    response = await signed_client.put(
        path,
        json={
            "base_revision": 1,
            "generation": 3,
            "owner": "manual",
            "enabled": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["job"]["status"] == "cancelled"


async def test_authentication_and_csrf(client, controlled_device):
    path = f"/api/devices/{controlled_device['id']}/automation"
    client.cookies.clear()
    assert (await client.get(path)).status_code == 401
    from conftest import TEST_PASSWORD

    await client.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})
    client.headers.pop("X-CSRF-Token", None)
    assert (await client.post(path + "/resume")).status_code == 403


def test_dst_and_equal_time_window():
    doc = rule_document()
    doc["timezone"] = "America/New_York"
    doc["rules"][0]["windows"] = [{"start": "01:00", "end": "02:00", "days": [6]}]
    rules = Ruleset.model_validate(doc)
    for fold in (0, 1):
        at = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=fold)
        assert evaluate(rules, {}, at.timestamp())["rule_id"] == "clean"
    doc["rules"][0]["windows"] = [{"start": "00:00", "end": "00:00", "days": [6]}]
    assert evaluate(Ruleset.model_validate(doc), {}, at.timestamp())["rule_id"] == "clean"


async def test_power_on_at_requested_fan_starts_dwell(app, signed_client, controlled_device):
    with app.state.sessions() as db:
        device = db.get(Device, controlled_device["id"])
        device.state = {**device.state, "power": "off", "fan_level": 3}
        db.commit()
    app.state.poller.provider.states["home@example.com"] = {"power": "off", "fan_level": 3}
    await setup_rules(signed_client, controlled_device, rule_document())
    await app.state.poller.automation.tick()
    await app.state.poller.automation.tick()
    with app.state.sessions() as db:
        state = db.get(Automation, controlled_device["id"])
        assert state.runtime["last_change_at"] > time.time() - 5
    assert len(app.state.poller.provider.control_calls) == 1


async def test_agent_completion_keeps_new_reconsideration_reasons(
    app, signed_client, controlled_device
):
    from climate_control.automation import request_review

    path = await setup_rules(signed_client, controlled_device)
    await signed_client.put(
        path,
        json={
            "base_revision": 1,
            "generation": 2,
            "owner": "agent",
            "enabled": True,
        },
    )
    lease = (await signed_client.post(path + "/jobs/claim")).json()
    with app.state.sessions() as db:
        state = db.get(Automation, controlled_device["id"])
        request_review(db, state, {"reason": "new_source_failure"}, time.time())
        db.commit()
    response = await signed_client.post(
        path + "/jobs/complete",
        json={
            "request_id": lease["request_id"],
            "lease_token": lease["lease_token"],
            "generation": 3,
            "base_revision": 1,
            "reason": "Agent update",
            "ruleset": rule_document(),
        },
    )
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "pending"
    assert job["base_revision"] == 2
    assert job["request_id"] != lease["request_id"]
    assert job["reasons"] == [{"reason": "new_source_failure"}]


async def test_context_is_cached_and_rules_survive_missing_reference(
    app, signed_client, controlled_device
):
    from conftest import account_input

    other_account = (
        await signed_client.post("/api/accounts", json=account_input("Other", "other@example.com"))
    ).json()
    devices = (await signed_client.get("/api/devices")).json()
    other = next(d for d in devices if d["id"] != controlled_device["id"])
    doc = rule_document(other["id"])
    doc["rules"][0]["conditions"][0] = {
        "source": {"kind": "device", "device_id": other["id"], "metric": "pm25"},
        "test": "unavailable",
    }
    path = await setup_rules(signed_client, controlled_device, doc)
    before = len(app.state.poller.provider.calls)
    context = await signed_client.get(path + "/context")
    assert context.status_code == 200
    assert "target_history" in context.json()
    assert "outdoor" in context.json()
    assert "vesync-secret" not in context.text
    assert len(app.state.poller.provider.calls) == before
    await signed_client.delete(f"/api/accounts/{other_account['id']}")
    await app.state.poller.automation.tick()
    assert app.state.poller.provider.control_calls[-1][-1].fan_level == 3
