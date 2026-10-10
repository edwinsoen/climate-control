from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """Safe, classified provider failure; never includes upstream response bodies."""

    MESSAGES = {
        "auth": "Account credentials need attention. Update them to reconnect.",
        "rate_limit": "The provider is limiting requests. Updates will resume automatically.",
        "offline": "This device is offline. Previous readings are shown.",
        "temporary": "The provider could not be reached. Updates will retry automatically.",
        "invalid_control": "This control is not supported by the device.",
        "control_failed": "The device did not accept the change.",
    }

    def __init__(self, kind: str = "temporary", retry_after: float = 0):
        self.kind = kind
        self.retry_after = retry_after
        super().__init__(self.MESSAGES[kind])


@dataclass
class DeviceSnapshot:
    provider_id: str
    name: str
    model: str
    supported: bool = True
    capabilities: list[str] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    retrieved_at: float | None = None
    error: str | None = None
    controls: dict = field(default_factory=dict)


@dataclass
class DeviceCommand:
    power: bool | None = None
    mode: str | None = None
    fan_level: int | None = None


@dataclass
class PollResult:
    devices: list[DeviceSnapshot]
    tokens: dict | None = None
    failure: ProviderError | None = None
    control_confirmed: bool = True


class DeviceProvider(Protocol):
    async def poll(self, credentials: dict, tokens: dict | None) -> PollResult: ...

    async def control(
        self, credentials: dict, tokens: dict | None, provider_id: str, command: DeviceCommand
    ) -> PollResult: ...

    async def status(
        self, credentials: dict, tokens: dict | None, provider_id: str
    ) -> PollResult: ...
