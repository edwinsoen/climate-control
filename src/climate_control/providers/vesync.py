import asyncio
import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import aiohttp
from pyvesync import VeSync
from pyvesync.const import DeviceStatus, PurifierFeatures
from pyvesync.device_map import get_device_config
from pyvesync.utils.errors import (
    ErrorTypes,
    VeSyncError,
    VeSyncLoginError,
    VeSyncRateLimitError,
    VeSyncTokenError,
)
from pyvesync.utils.helpers import Helpers

from climate_control.providers.base import DeviceCommand, DeviceSnapshot, PollResult, ProviderError


class TokenExpired(Exception):
    pass


class DiscoveryManager(VeSync):
    """Keep unsupported purifier descriptors that pyvesync otherwise discards."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.descriptors = []

    def process_devices(self, response):
        self.descriptors = list(response.result.list)
        return super().process_devices(response)


def retry_delay(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0, (date - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError):
            return 0


class RequestPacer:
    def __init__(self, spacing: float):
        self.spacing = spacing
        self.last_request = 0.0
        self.lock = asyncio.Lock()

    async def before_request(self, session, context, params):
        async with self.lock:
            remaining = self.last_request + self.spacing - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self.last_request = time.monotonic()

    async def after_request(self, session, context, params):
        response = params.response
        if response.status == 429:
            raise ProviderError("rate_limit", retry_delay(response.headers.get("Retry-After")))
        if response.status in (401, 403):
            raise ProviderError("auth")
        if response.status != 200:
            return
        try:
            payload = await response.json(content_type=None)
        except (ValueError, aiohttp.ClientError):
            return
        if not isinstance(payload, dict):
            return
        error = Helpers.parse_error_code(payload)
        if error.error_type == ErrorTypes.RATE_LIMIT:
            raise ProviderError("rate_limit", retry_delay(response.headers.get("Retry-After")))
        if error.error_type == ErrorTypes.TOKEN_ERROR:
            # Intercept before the library retries a request with the previous token in its body.
            raise TokenExpired()
        if error.error_type == ErrorTypes.AUTHENTICATION:
            raise ProviderError("auth")

    def trace(self) -> aiohttp.TraceConfig:
        trace = aiohttp.TraceConfig()
        trace.on_request_start.append(self.before_request)
        trace.on_request_end.append(self.after_request)
        return trace


def capabilities(device) -> list[str]:
    result = ["power", "mode", "fan_level", "filter_life", "display"]
    features = device.features
    if PurifierFeatures.AIR_QUALITY in features:
        result.append("air_quality")
        # The 131 families report qualitative air quality, without a PM2.5 concentration.
        if not device.device_type.startswith(("LV-PUR131", "LV-RH131")):
            result.append("pm25")
    for feature in ("pm25", "pm1", "pm10"):
        measured = (
            feature != "pm25"
            and PurifierFeatures.AIR_QUALITY in features
            and getattr(device.state, feature, None) is not None
        )
        if (feature in features or measured) and feature not in result:
            if feature == "pm25" and device.device_type.startswith(("LV-PUR131", "LV-RH131")):
                continue
            result.append(feature)
    return result


def display_state(device) -> str | None:
    for attr in ("display_status", "display_set_status"):
        raw = getattr(device.state, attr, None)
        if isinstance(raw, bool):
            return "on" if raw else "off"
        if raw is not None and str(raw).lower() in {"on", "off"}:
            return str(raw).lower()
    return None


def normalize(device) -> DeviceSnapshot:
    state = device.state
    supported = capabilities(device)
    normalized = {
        "connectivity": str(state.connection_status)
        if state.connection_status is not None
        else "unknown",
        "power": str(state.device_status) if state.device_status is not None else "unknown",
        "mode": str(state.mode) if state.mode is not None else "unknown",
        "fan_level": selected_fan_level(device),
        "reported_fan_level": state.fan_level,
        "filter_life": state.filter_life,
        "display": display_state(device) if "display" in supported else None,
        "air_quality": state.air_quality_string if "air_quality" in supported else None,
        "pm25": state.pm25 if "pm25" in supported else None,
        "pm1": state.pm1 if "pm1" in supported else None,
        "pm10": state.pm10 if "pm10" in supported else None,
    }
    if normalized["air_quality"] == "unknown":
        normalized["air_quality"] = None
    config = get_device_config(device.device_type)
    return DeviceSnapshot(
        provider_id=device.cid,
        name=device.device_name,
        model=config.model_name if config else device.device_type,
        capabilities=supported,
        controls=control_options(device, supported),
        state=normalized,
        retrieved_at=time.time(),
    )


def selected_fan_level(device):
    configured = getattr(device.state, "fan_set_level", None)
    if str(device.state.mode) == "manual" and configured in getattr(device, "fan_levels", []):
        return configured
    return device.state.fan_level


def control_options(device, supported: list[str] | None = None) -> dict:
    if supported is None:
        supported = capabilities(device)
    has_warning_sensor = any(
        metric in supported for metric in ("pm25", "pm10", "air_quality")
    )
    display_modes = ["manual", *(["warnings_only"] if has_warning_sensor else [])]
    return {
        "power": True,
        "modes": [str(mode) for mode in getattr(device, "modes", []) if mode in {"auto", "sleep"}],
        "fan_levels": [
            level
            for level in getattr(device, "fan_levels", [])
            if isinstance(level, int) and not isinstance(level, bool) and level > 0
        ],
        "display": "display" in supported,
        "display_modes": display_modes if "display" in supported else [],
    }


def unknown_purifiers(manager) -> list[DeviceSnapshot]:
    known = {device.cid for device in manager.devices}
    result = []
    for descriptor in manager.descriptors:
        model = descriptor.deviceType
        purifier = str(descriptor.productType or "").lower() in {
            "purifier",
            "airpurifier",
            "air_purifier",
        } or model.lower().startswith(("lap-", "core", "lv-pur", "lv-rh"))
        if descriptor.cid not in known and purifier:
            result.append(
                DeviceSnapshot(
                    provider_id=descriptor.cid,
                    name=descriptor.deviceName,
                    model=model,
                    supported=False,
                    error="This purifier model is not supported by pyvesync.",
                )
            )
    return result


def classified(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, (VeSyncLoginError, VeSyncTokenError, TokenExpired)):
        return ProviderError("auth")
    if isinstance(exc, VeSyncRateLimitError):
        return ProviderError("rate_limit")
    return ProviderError()


class VeSyncProvider:
    def __init__(self, spacing: float = 1):
        self.spacing = spacing
        # Vendor exception messages and error-level log entries may contain response bodies.
        logging.getLogger("pyvesync").setLevel(logging.CRITICAL)

    async def poll(self, credentials: dict, tokens: dict | None) -> PollResult:
        return await self.run(credentials, tokens, self.collect)

    async def control(
        self, credentials: dict, tokens: dict | None, provider_id: str, command: DeviceCommand
    ) -> PollResult:
        async def operation(manager):
            return await self.apply_control(manager, provider_id, command)

        return await self.run(credentials, tokens, operation)

    async def run(self, credentials: dict, tokens: dict | None, operation) -> PollResult:
        pacer = RequestPacer(self.spacing)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25, connect=10, sock_read=15),
            trace_configs=[pacer.trace()],
        ) as session:
            for attempt in range(2):
                # A new manager prevents cached request attributes from retaining old tokens.
                async with DiscoveryManager(
                    username=credentials["email"],
                    password=credentials["password"],
                    country_code=credentials["country_code"],
                    time_zone=credentials["time_zone"],
                    session=session,
                    redact=True,
                ) as manager:
                    if tokens:
                        manager.set_credentials(
                            token=tokens["token"],
                            account_id=tokens["account_id"],
                            country_code=tokens["country_code"],
                            region=tokens["current_region"],
                        )
                        manager.enabled = True
                    else:
                        try:
                            if not await manager.login():
                                raise ProviderError("auth")
                        except (
                            VeSyncError,
                            ProviderError,
                            TokenExpired,
                            aiohttp.ClientError,
                            TimeoutError,
                        ) as exc:
                            return PollResult([], failure=classified(exc))
                    try:
                        return await operation(manager)
                    except TokenExpired:
                        if attempt:
                            return PollResult([], failure=ProviderError("auth"))
                        try:
                            if not await manager.auth.reauthenticate():
                                raise ProviderError("auth")
                            tokens = manager.output_credentials_dict()
                        except Exception as exc:
                            return PollResult([], failure=classified(exc))
                    except (VeSyncError, ProviderError, aiohttp.ClientError, TimeoutError) as exc:
                        return PollResult(
                            [], tokens=manager.output_credentials_dict(), failure=classified(exc)
                        )
        return PollResult([], failure=ProviderError())

    async def status(self, credentials: dict, tokens: dict | None, provider_id: str) -> PollResult:
        async def operation(manager):
            if not await manager.get_devices():
                raise ProviderError()
            device = next((d for d in manager.devices.air_purifiers if d.cid == provider_id), None)
            if device is None:
                raise ProviderError("invalid_control")
            await self.read_details(device)
            return PollResult([normalize(device)], manager.output_credentials_dict())

        return await self.run(credentials, tokens, operation)

    @staticmethod
    def check_response(device, failure_kind="temporary"):
        response = device.last_response
        if response and response.error_type == ErrorTypes.RATE_LIMIT:
            raise ProviderError("rate_limit")
        if response and response.error_type in (ErrorTypes.AUTHENTICATION, ErrorTypes.TOKEN_ERROR):
            raise ProviderError("auth")
        if (
            response
            and response.device_online is False
            or device.state.connection_status == "offline"
        ):
            raise ProviderError("offline")
        if response is None or response.error_type != ErrorTypes.SUCCESS:
            raise ProviderError(failure_kind)

    async def read_details(self, device, failure_kind="temporary"):
        # pyvesync can report SUCCESS even when detail parsing failed. Clear
        # required parsing markers so a no-op cannot reuse an earlier reading.
        device.last_response = None
        device.state.fan_level = None
        device.state.mode = "unknown"
        device.state.display_status = None
        device.state.display_set_status = None
        await device.update()
        self.check_response(device)
        if device.state.fan_level is None or str(device.state.mode) == "unknown":
            raise ProviderError(failure_kind)

    async def apply_control(self, manager, provider_id: str, command: DeviceCommand) -> PollResult:
        if not await manager.get_devices():
            raise ProviderError()
        device = next((d for d in manager.devices.air_purifiers if d.cid == provider_id), None)
        if device is None:
            raise ProviderError("invalid_control")
        options = control_options(device)
        if command.mode is not None and command.mode not in options["modes"]:
            raise ProviderError("invalid_control")
        if command.fan_level is not None and command.fan_level not in options["fan_levels"]:
            raise ProviderError("invalid_control")
        if command.display is not None and not options.get("display"):
            raise ProviderError("invalid_control")
        await self.read_details(device, failure_kind="control_failed")
        # Do not let a speed/mode/display selection turn on a device switched off elsewhere.
        if command.power is None and str(device.state.device_status) != "on":
            raise ProviderError("control_failed")
        if command.display is not None and self.matches_command(device, command):
            return PollResult(
                [normalize(device)],
                manager.output_credentials_dict(),
                control_confirmed=True,
            )
        try:
            if command.power is not None:
                await (device.turn_on() if command.power else device.turn_off())
            elif command.mode is not None:
                await device.set_mode(command.mode)
            elif command.fan_level is not None:
                await device.set_fan_speed(command.fan_level)
            elif command.display is not None:
                # BaseV2/Vital toggle_display short-circuits if light_detection_status is truthy
                # or if bool(display_set_status) already matches the target boolean.
                if hasattr(device.state, "light_detection_status"):
                    device.state.light_detection_status = DeviceStatus.OFF
                if hasattr(device.state, "display_set_status"):
                    device.state.display_set_status = (
                        DeviceStatus.OFF if command.display else DeviceStatus.ON
                    )
                await device.toggle_display(command.display)
        except (VeSyncError, aiohttp.ClientError, TimeoutError) as exc:
            error = classified(exc)
            if error.kind in {"auth", "rate_limit"}:
                raise error from exc
            # A lost acknowledgement can follow an applied command; read status now.
        rejected = False
        try:
            self.check_response(device, failure_kind="control_failed")
        except ProviderError as exc:
            if exc.kind != "control_failed":
                raise
            rejected = True
        # Read status once immediately, without holding controls in a confirmation loop.
        await self.read_details(device)
        if rejected and not self.matches_command(device, command):
            raise ProviderError("control_failed")
        # Persist the observed state even if an accepted change has not propagated yet.
        # A stale readback alone is not evidence that the command was rejected.
        return PollResult(
            [normalize(device)],
            manager.output_credentials_dict(),
            control_confirmed=self.matches_command(device, command),
        )

    @staticmethod
    def matches_command(device, command: DeviceCommand) -> bool:
        if command.power is not None:
            return str(device.state.device_status) == ("on" if command.power else "off")
        if command.mode is not None:
            return str(device.state.mode) == command.mode
        if command.display is not None:
            return display_state(device) == ("on" if command.display else "off")
        return (
            selected_fan_level(device) == command.fan_level and str(device.state.mode) == "manual"
        )

    async def collect(self, manager) -> PollResult:
        if not await manager.get_devices():
            raise ProviderError()
        snapshots = unknown_purifiers(manager)
        failure = None
        for device in manager.devices.air_purifiers:
            try:
                await self.read_details(device)
                snapshots.append(normalize(device))
            except TokenExpired:
                raise
            except (VeSyncError, ProviderError, aiohttp.ClientError, TimeoutError) as exc:
                error = classified(exc)
                snapshots.append(
                    DeviceSnapshot(
                        provider_id=device.cid,
                        name=device.device_name,
                        model=device.device_type,
                        capabilities=capabilities(device),
                        controls=control_options(device),
                        error=str(error),
                        state={"connectivity": "offline"} if error.kind == "offline" else {},
                    )
                )
                if error.kind in {"rate_limit", "auth"}:
                    return PollResult(snapshots, manager.output_credentials_dict(), error)
                if error.kind != "offline":
                    failure = error
        return PollResult(snapshots, manager.output_credentials_dict(), failure)
