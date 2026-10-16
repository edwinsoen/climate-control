import time
from types import SimpleNamespace

import pytest
from pyvesync.auth import VeSyncAuth
from pyvesync.const import DeviceStatus
from pyvesync.models.vesync_models import ResponseDeviceListModel
from pyvesync.utils.errors import ErrorTypes, ResponseInfo
from pyvesync.utils.helpers import Helpers

from climate_control.db import Device
from climate_control.providers.base import DeviceCommand, PollResult, ProviderError
from climate_control.providers.vesync import (
    DiscoveryManager,
    RequestPacer,
    TokenExpired,
    VeSyncProvider,
    normalize,
    retry_delay,
    unknown_purifiers,
)


def descriptor(model, cid="cid"):
    return {
        "deviceRegion": "US",
        "isOwner": True,
        "deviceName": "Living room",
        "cid": cid,
        "connectionType": "wifi",
        "deviceType": model,
        "type": "wifi",
        "configModule": model,
        "connectionStatus": "online",
        "deviceStatus": "on",
    }


def manager_with(models):
    manager = DiscoveryManager("account@example.com", "password")
    manager.process_devices(
        ResponseDeviceListModel.from_dict(
            {
                "code": 0,
                "msg": "success",
                "traceId": "test-trace",
                "result": {
                    "total": len(models),
                    "pageSize": 100,
                    "pageNo": 1,
                    "list": [descriptor(model, f"cid-{i}") for i, model in enumerate(models)],
                },
            }
        )
    )
    return manager


@pytest.mark.parametrize(
    "model",
    [
        "Core200S",
        "Core300S",
        "Core400S",
        "Core600S",
        "LV-PUR131S",
        "LV-RH131S",
        "LAP-V102S-WUS",
        "LAP-V201S-WUS",
        "LAP-EL551S-WUS",
        "LAP-B851S-WUS",
    ],
)
def test_real_library_model_mapping(model):
    manager = manager_with([model])
    device = manager.devices.air_purifiers[0]
    device.state.mode = "manual"
    device.state.fan_level = 2
    device.state.filter_life = 86
    device.state.pm25 = 0
    device.state.air_quality_level = 1
    snapshot = normalize(device)
    assert snapshot.controls["fan_levels"] == [level for level in device.fan_levels if level > 0]
    assert ("auto" in snapshot.controls["modes"]) == ("auto" in device.modes)
    assert snapshot.name == "Living room"
    assert snapshot.state["power"] == "on"
    assert snapshot.state["fan_level"] == 2
    assert snapshot.state["filter_life"] == 86
    assert "display" in snapshot.capabilities
    assert snapshot.controls["display"] is True
    assert snapshot.retrieved_at > time.time() - 2
    if model == "Core200S":
        assert snapshot.controls["display_modes"] == ["manual"]
    else:
        assert snapshot.controls["display_modes"] == ["manual", "warnings_only"]
    if model in {"Core200S", "LV-PUR131S", "LV-RH131S"}:
        assert "pm25" not in snapshot.capabilities
        assert snapshot.state["pm25"] is None
        if model != "Core200S":
            assert snapshot.state["air_quality"] == "excellent"
    else:
        assert snapshot.state["pm25"] == 0
        assert snapshot.state["air_quality"] == "excellent"


def test_independent_real_managers_and_unknown_model():
    first = manager_with(["Core400S", "LAP-UNKNOWN-WUS"])
    second = manager_with(["Core200S"])
    assert first.devices is not second.devices
    assert first.devices.air_purifiers[0].device_type == "Core400S"
    assert second.devices.air_purifiers[0].device_type == "Core200S"
    unknown = unknown_purifiers(first)
    assert len(unknown) == 1
    assert unknown[0].supported is False


async def test_pacer_spaces_all_requests(monkeypatch):
    calls = []
    current = [10.0]
    monkeypatch.setattr("climate_control.providers.vesync.time.monotonic", lambda: current[0])

    async def sleep(duration):
        calls.append(duration)
        current[0] += duration

    monkeypatch.setattr("climate_control.providers.vesync.asyncio.sleep", sleep)
    pacer = RequestPacer(1)
    await pacer.before_request(None, None, None)
    await pacer.before_request(None, None, None)
    assert calls == [1]


@pytest.mark.parametrize(
    "status,payload,kind",
    [
        (429, {}, "rate_limit"),
        (200, {"code": -11003000}, "rate_limit"),
        (200, {"code": -16906000}, "rate_limit"),
        (401, {}, "auth"),
        (200, {"code": -11201000}, "auth"),
    ],
)
async def test_response_classification(status, payload, kind):
    async def response_json(**kwargs):
        return payload

    response = SimpleNamespace(status=status, headers={"Retry-After": "3600"}, json=response_json)
    with pytest.raises(ProviderError) as error:
        await RequestPacer(1).after_request(None, None, SimpleNamespace(response=response))
    assert error.value.kind == kind
    if kind == "rate_limit":
        assert error.value.retry_after == 3600


async def test_expired_token_intercepted_before_library_retry():
    async def response_json(**kwargs):
        return {"code": -11001000}

    response = SimpleNamespace(status=200, headers={}, json=response_json)
    with pytest.raises(TokenExpired):
        await RequestPacer(1).after_request(None, None, SimpleNamespace(response=response))


def fake_device(cid, error=None):
    async def update():
        if error:
            raise error
        device.state.mode = "manual"
        device.state.fan_level = 2
        device.last_response = ResponseInfo("SUCCESS", ErrorTypes.SUCCESS, "Success")

    device = SimpleNamespace(
        cid=cid,
        device_name=cid,
        device_type="Core400S",
        features=["air_quality"],
        update=update,
        last_response=ResponseInfo("SUCCESS", ErrorTypes.SUCCESS, "Success"),
        state=SimpleNamespace(
            connection_status="online",
            device_status="on",
            mode="manual",
            fan_level=2,
            filter_life=86,
            air_quality_string="good",
            pm25=8,
            pm1=None,
            pm10=None,
            last_update_ts=int(time.time()),
        ),
    )
    return device


async def test_collect_stops_after_rate_limit_preserves_partial_results():
    manager = SimpleNamespace(
        descriptors=[],
        devices=SimpleNamespace(
            air_purifiers=[
                fake_device("first"),
                fake_device("limited", ProviderError("rate_limit")),
                fake_device("never-read"),
            ]
        ),
        output_credentials_dict=lambda: {"token": "token"},
    )

    # unknown_purifiers iterates the complete container; use an adapter container for the test.
    class Devices(list):
        @property
        def air_purifiers(self):
            return self

    manager.devices = Devices(manager.devices.air_purifiers)

    async def get_devices():
        return True

    manager.get_devices = get_devices
    result = await VeSyncProvider().collect(manager)
    assert result.failure.kind == "rate_limit"
    assert [snapshot.provider_id for snapshot in result.devices] == ["first", "limited"]
    assert result.devices[0].state["pm25"] == 8


def test_retry_after_parsing():
    assert retry_delay("3600") == 3600
    assert retry_delay("not-a-date") == 0
    assert retry_delay("Wed, 01 Jan 2020 00:00:00 GMT") == 0


@pytest.mark.parametrize("malformed", [False, True])
async def test_real_core_response_parsing_and_invalid_details(malformed):
    manager = manager_with(["Core400S"])
    manager.set_credentials("token", "account", "US", "US")

    async def get_devices():
        return True

    async def call_api(*args, **kwargs):
        details = (
            {}
            if malformed
            else {
                "enabled": True,
                "filter_life": 86,
                "mode": "manual",
                "level": 2,
                "device_error_code": 0,
                "air_quality": 2,
                "air_quality_value": 8,
            }
        )
        return {"code": 0, "result": {"code": 0, "result": details}}, 200

    manager.get_devices = get_devices
    manager.async_call_api = call_api
    result = await VeSyncProvider().collect(manager)
    if malformed:
        assert result.failure is not None
        assert result.devices[0].retrieved_at is None
    else:
        assert result.failure is None
        assert result.devices[0].state["pm25"] == 8
        assert result.devices[0].state["filter_life"] == 86


@pytest.mark.parametrize("stage", ["status", "control_initial", "control_readback"])
@pytest.mark.parametrize("invalid_details", [{}, {"mode": "manual", "level": 2}])
async def test_malformed_details_preserve_cached_state_and_timestamp(
    app, signed_client, controlled_device, monkeypatch, stage, invalid_details
):
    manager = manager_with(["Core400S"])
    manager.set_credentials("token", "account", "US", "US")
    active = False
    reads = 0
    writes = []

    async def get_devices():
        return True

    async def call_api(endpoint, method, body, *args):
        nonlocal reads
        payload = body.to_dict()["payload"]
        if payload["method"] == "getPurifierStatus":
            reads += int(active)
        else:
            writes.append(payload["method"])
        details = {
            "enabled": True,
            "filter_life": 86,
            "mode": "manual",
            "level": 2,
            "device_error_code": 0,
            "air_quality": 2,
            "air_quality_value": 8,
        }
        if (
            active
            and payload["method"] == "getPurifierStatus"
            and (stage != "control_readback" or reads > 1)
        ):
            details = invalid_details
        return {"code": 0, "result": {"code": 0, "result": details}}, 200

    manager.get_devices = get_devices
    manager.async_call_api = call_api
    # Seed a valid library state: a failed parse must not reuse these old values.
    await manager.devices.air_purifiers[0].update()
    active = True
    provider = VeSyncProvider(spacing=0)

    async def local_run(credentials, tokens, operation):
        try:
            return await operation(manager)
        except ProviderError as exc:
            return PollResult([], failure=exc)

    monkeypatch.setattr(provider, "run", local_run)
    app.state.poller.provider = provider
    with app.state.sessions() as db:
        db.get(Device, controlled_device["id"]).provider_id = "cid-0"
        db.commit()
    url = f"/api/devices/{controlled_device['id']}"
    response = await signed_client.post(
        f"{url}/status" if stage == "status" else f"{url}/control",
        **({} if stage == "status" else {"json": {"power": False}}),
    )
    assert response.status_code == 502
    assert manager.devices.air_purifiers[0].last_response.error_type == ErrorTypes.SUCCESS
    current = (await signed_client.get("/api/devices")).json()[0]
    assert current["state"] == controlled_device["state"]
    assert current["retrieved_at"] == controlled_device["retrieved_at"]
    assert writes == (["setSwitch"] if stage == "control_readback" else [])


async def test_token_reauthentication_rebuilds_cached_manager_attributes(monkeypatch):
    provider = VeSyncProvider()
    managers = []
    reauths = []

    async def reauthenticate(auth):
        reauths.append(auth)
        auth.set_credentials("new-token", "account", "US", "US")
        return True

    async def collect(manager):
        managers.append(manager)
        cached = Helpers.get_manager_attributes(manager, ("token",))
        if len(managers) == 1:
            assert cached["token"] == "old-token"
            raise TokenExpired()
        assert cached["token"] == "new-token"
        from climate_control.providers.base import PollResult

        return PollResult([], manager.output_credentials_dict())

    monkeypatch.setattr(VeSyncAuth, "reauthenticate", reauthenticate)
    monkeypatch.setattr(provider, "collect", collect)
    credentials = {
        "email": "owner@example.com",
        "password": "password",
        "country_code": "US",
        "time_zone": "UTC",
    }
    result = await provider.poll(
        credentials,
        {
            "token": "old-token",
            "account_id": "account",
            "country_code": "US",
            "current_region": "US",
        },
    )
    assert result.failure is None
    assert result.tokens["token"] == "new-token"
    assert managers[0] is not managers[1]
    assert len(reauths) == 1


@pytest.mark.parametrize(
    "command",
    [
        DeviceCommand(power=False),
        DeviceCommand(mode="auto"),
        DeviceCommand(mode="sleep"),
        DeviceCommand(fan_level=4),
        DeviceCommand(display=False),
        DeviceCommand(display=True),
    ],
)
async def test_real_core_control_requests_and_readback(command):
    manager = manager_with(["Core400S"])
    manager.set_credentials("token", "account", "US", "US")
    details = {
        "enabled": True,
        "display": not command.display if command.display is not None else True,
        "filter_life": 86,
        "mode": "manual",
        "level": 2,
        "device_error_code": 0,
        "air_quality": 2,
        "air_quality_value": 8,
    }
    requests = []

    async def get_devices():
        return True

    async def call_api(endpoint, method, body, *args, **kwargs):
        payload = body.to_dict()["payload"]
        requests.append(payload)
        if payload["method"] == "setSwitch":
            details["enabled"] = payload["data"]["enabled"]
        elif payload["method"] == "setPurifierMode":
            details["mode"] = payload["data"]["mode"]
        elif payload["method"] == "setLevel":
            details.update(mode="manual", level=payload["data"]["level"])
        elif payload["method"] == "setDisplay":
            details["display"] = payload["data"]["state"]
        return {"code": 0, "result": {"code": 0, "result": dict(details)}}, 200

    manager.get_devices = get_devices
    manager.async_call_api = call_api
    result = await VeSyncProvider().apply_control(manager, "cid-0", command)
    assert len(requests) == 3
    assert requests[0]["method"] == requests[-1]["method"] == "getPurifierStatus"
    state = result.devices[0].state
    if command.power is not None:
        assert state["power"] == "off"
    if command.mode is not None:
        assert requests[1]["method"] == "setPurifierMode"
        assert requests[1]["data"]["mode"] == command.mode
        assert state["mode"] == command.mode
    if command.fan_level is not None:
        assert state["fan_level"] == 4 and state["mode"] == "manual"
    if command.display is not None:
        assert requests[1]["method"] == "setDisplay"
        assert requests[1]["data"]["state"] is command.display
        assert state["display"] == ("on" if command.display else "off")
        assert result.control_confirmed is True


async def test_vital_display_control_bypasses_library_short_circuit():
    manager = manager_with(["LAP-V201S-WUS"])
    manager.set_credentials("token", "account", "US", "US")
    details = {
        "powerSwitch": 1,
        "filterLifePercent": 90,
        "workMode": "manual",
        "manualSpeedLevel": 2,
        "fanSpeedLevel": 2,
        "AQLevel": 1,
        "PM25": 4,
        "screenState": 0,
        "childLockSwitch": 0,
        "screenSwitch": 1,
        "lightDetectionSwitch": 1,
        "environmentLightState": 0,
        "scheduleCount": 0,
        "timerRemain": 0,
        "efficientModeTimeRemain": 0,
        "errorCode": 0,
    }
    requests = []

    async def get_devices():
        return True

    async def call_api(endpoint, method, body, *args, **kwargs):
        payload = body.to_dict()["payload"]
        requests.append(payload)
        if payload["method"] == "setDisplay":
            flag = payload["data"]["screenSwitch"]
            details.update(screenState=flag, screenSwitch=flag)
        return {"code": 0, "result": {"code": 0, "result": dict(details)}}, 200

    manager.get_devices = get_devices
    manager.async_call_api = call_api
    # When screenSwitch is 1 and screenState is 0 (dimmed by sleep/light detection),
    # display_state reads "on" from display_set_status and skips a redundant setDisplay(True).
    noop_on = await VeSyncProvider().apply_control(
        manager, "cid-0", DeviceCommand(display=True)
    )
    assert [req["method"] for req in requests] == ["getPurifierStatus"]
    assert noop_on.devices[0].state["display"] == "on"
    assert noop_on.control_confirmed is True

    requests.clear()
    result = await VeSyncProvider().apply_control(
        manager, "cid-0", DeviceCommand(display=False)
    )
    assert [req["method"] for req in requests] == [
        "getPurifierStatus",
        "setDisplay",
        "getPurifierStatus",
    ]
    assert result.devices[0].state["display"] == "off"
    assert result.control_confirmed is True

    # If live status already matches target display, apply_control short-circuits before setDisplay.
    requests.clear()
    noop = await VeSyncProvider().apply_control(
        manager, "cid-0", DeviceCommand(display=False)
    )
    assert [req["method"] for req in requests] == ["getPurifierStatus"]
    assert noop.control_confirmed is True

    # display_state falls back to display_status when display_set_status is UNKNOWN.
    device = manager.devices.air_purifiers[0]
    device.state.display_set_status = DeviceStatus.UNKNOWN
    device.state.display_status = DeviceStatus.ON
    assert normalize(device).state["display"] == "on"


@pytest.mark.parametrize("failure", ["rejected", "offline"])
async def test_control_never_claims_success_on_failed_response(failure, monkeypatch):
    manager = manager_with(["Core400S"])
    manager.set_credentials("token", "account", "US", "US")
    requests = []

    async def get_devices():
        return True

    async def call_api(endpoint, method, body, *args):
        payload = body.to_dict()["payload"]
        requests.append(payload)
        if failure == "rejected" and payload["method"] == "setLevel":
            return {"code": -1}, 200
        details = {
            "enabled": True,
            "filter_life": 86,
            "mode": "auto",
            "level": 2,
            "device_error_code": 0,
            "air_quality": 2,
            "air_quality_value": 8,
        }
        return {"code": 0, "result": {"code": 0, "result": details}}, 200

    manager.get_devices = get_devices
    manager.async_call_api = call_api
    if failure == "offline":
        manager.devices.air_purifiers[0].state.connection_status = "offline"

        # An update that leaves the purifier offline must block any command.
        async def update(self):
            pass

        monkeypatch.setattr(type(manager.devices.air_purifiers[0]), "update", update)
    with pytest.raises(ProviderError):
        await VeSyncProvider().apply_control(manager, "cid-0", DeviceCommand(fan_level=4))
    if failure == "offline":
        assert not requests


@pytest.mark.parametrize("propagates", [True, False])
@pytest.mark.parametrize("lost_acknowledgement", [True, False])
async def test_accepted_control_reads_status_once_without_false_failure(
    propagates, lost_acknowledgement
):
    manager = manager_with(["Core400S"])
    manager.set_credentials("token", "account", "US", "US")
    methods = []
    readbacks = 0

    async def get_devices():
        return True

    async def call_api(endpoint, method, body, *args):
        nonlocal readbacks
        payload = body.to_dict()["payload"]
        methods.append(payload["method"])
        if lost_acknowledgement and payload["method"] == "setLevel":
            raise TimeoutError()
        if payload["method"] == "getPurifierStatus" and "setLevel" in methods:
            readbacks += 1
        applied = propagates and readbacks >= 1
        details = {
            "enabled": True,
            "filter_life": 86,
            "mode": "manual" if applied else "auto",
            "level": 4 if applied else 2,
            "device_error_code": 0,
            "air_quality": 2,
            "air_quality_value": 8,
        }
        return {"code": 0, "result": {"code": 0, "result": details}}, 200

    manager.get_devices = get_devices
    manager.async_call_api = call_api
    result = await VeSyncProvider(spacing=0).apply_control(
        manager, "cid-0", DeviceCommand(fan_level=4)
    )
    assert result.failure is None
    assert result.control_confirmed == propagates
    assert readbacks == 1
    assert methods.count("setLevel") == 1
    assert result.devices[0].state["fan_level"] == (4 if propagates else 2)


def test_manual_selector_uses_configured_speed_not_transient_motor_speed():
    manager = manager_with(["Core400S"])
    device = manager.devices.air_purifiers[0]
    device.state.mode = "manual"
    device.state.fan_level = 2
    device.state.fan_set_level = 4
    snapshot = normalize(device)
    assert snapshot.state["fan_level"] == 4
    assert snapshot.state["reported_fan_level"] == 2
    assert VeSyncProvider.matches_command(device, DeviceCommand(fan_level=4))


async def test_model_specific_control_validation_before_vendor_request():
    manager = manager_with(["Core200S"])
    manager.set_credentials("token", "account", "US", "US")

    async def get_devices():
        return True

    manager.get_devices = get_devices
    for command in [DeviceCommand(mode="auto"), DeviceCommand(fan_level=4)]:
        with pytest.raises(ProviderError) as error:
            await VeSyncProvider().apply_control(manager, "cid-0", command)
        assert error.value.kind == "invalid_control"
