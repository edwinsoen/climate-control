import asyncio
import logging
import math
import time
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import delete, select, update

from climate_control.config import Settings
from climate_control.db import Account, Device
from climate_control.history import prune_readings, record_reading
from climate_control.providers.base import DeviceCommand, DeviceProvider, PollResult, ProviderError
from climate_control.security import Vault

logger = logging.getLogger(__name__)
LEASE_SECONDS = 300
POLL_TIMEOUT_SECONDS = 240
CONTROL_COOLDOWN_SECONDS = 5
CONTROL_QUEUE_SECONDS = 30


@dataclass
class RefreshResult:
    status: str
    retry_after: int = 0
    control_confirmed: bool = True


class Poller:
    def __init__(self, sessions, settings: Settings, vault: Vault, provider: DeviceProvider):
        self.sessions = sessions
        self.settings = settings
        self.vault = vault
        self.provider = provider
        self.concurrency = asyncio.Semaphore(3)

    def claim(self, account_id: str) -> tuple[Account | None, RefreshResult]:
        current = time.time()
        lease_token = str(uuid4())
        with self.sessions() as db:
            claimed = db.execute(
                update(Account)
                .where(
                    Account.id == account_id,
                    Account.status != "auth_error",
                    Account.next_poll_at <= current,
                    Account.lease_until <= current,
                )
                .values(
                    lease_token=lease_token,
                    lease_until=current + LEASE_SECONDS,
                    last_attempt_at=current,
                    next_poll_at=current + self.settings.poll_interval_seconds,
                )
            ).rowcount
            db.commit()
            account = db.get(Account, account_id)
            if not account:
                return None, RefreshResult("missing")
            if not claimed:
                if account.status == "auth_error":
                    return None, RefreshResult("auth_error")
                return None, RefreshResult(
                    "cooldown",
                    max(1, math.ceil(max(account.next_poll_at, account.lease_until) - current)),
                )
            return account, RefreshResult("claimed")

    async def refresh(self, account_id: str) -> RefreshResult:
        # Acquire a capacity slot before claiming; queued work never holds an expiring lease.
        async with self.concurrency:
            account, decision = self.claim(account_id)
            if account is None:
                return decision
            try:
                async with asyncio.timeout(POLL_TIMEOUT_SECONDS):
                    result = await self.provider.poll(
                        self.vault.open(account.credentials),
                        self.vault.open(account.tokens) if account.tokens else None,
                    )
            except ProviderError as exc:
                result = PollResult([], failure=exc)
            except asyncio.CancelledError:
                self.finish(account, PollResult([], failure=ProviderError()))
                raise
            except Exception:
                # Upstream errors may contain secrets; log only a stable event and internal ID.
                logger.warning("provider_poll_failed account=%s", account.id)
                result = PollResult([], failure=ProviderError())
            self.finish(account, result)
            logger.info(
                "account_polled account=%s status=%s",
                account.id,
                result.failure.kind if result.failure else "ok",
            )
            return RefreshResult("refreshed")

    async def queued_control(self, device_id: str, command: DeviceCommand) -> RefreshResult:
        # Waiting requests hold no lease or capacity slot. The database lease still
        # serializes commands and polls across instances sharing the database.
        deadline = time.monotonic() + CONTROL_QUEUE_SECONDS
        while True:
            result = await self.control(device_id, command)
            if result.status not in {"busy", "cooldown"}:
                return result
            with self.sessions() as db:
                device = db.get(Device, device_id)
                account = db.get(Account, device.account_id) if device else None
                if account is None:
                    return RefreshResult("missing")
                if account.status == "rate_limited" and account.next_poll_at > time.time():
                    return RefreshResult(
                        "cooldown", max(1, math.ceil(account.next_poll_at - time.time()))
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            await asyncio.sleep(min(0.25, remaining))

    async def control(self, device_id: str, command: DeviceCommand | None) -> RefreshResult:
        async with self.concurrency:
            current = time.time()
            with self.sessions() as db:
                device = db.get(Device, device_id)
                if device is None:
                    return RefreshResult("missing")
                lease_token = str(uuid4())
                claimed = db.execute(
                    update(Account)
                    .where(
                        Account.id == device.account_id,
                        Account.provider == "vesync",
                        Account.status != "auth_error",
                        Account.lease_until <= current,
                        Account.next_control_at <= current,
                        # User commands cannot bypass a provider's rate-limit backoff.
                        (Account.status != "rate_limited") | (Account.next_poll_at <= current),
                    )
                    .values(
                        lease_token=lease_token,
                        lease_until=current + LEASE_SECONDS,
                        next_control_at=current + CONTROL_COOLDOWN_SECONDS
                        if command is not None
                        else Account.next_control_at,
                    )
                ).rowcount
                db.commit()
                account = db.get(Account, device.account_id)
                if not account:
                    return RefreshResult("missing")
                if not claimed:
                    if account.status == "auth_error":
                        return RefreshResult("auth_error")
                    if account.lease_until > current:
                        return RefreshResult("busy")
                    deadline = max(
                        account.next_control_at,
                        account.next_poll_at if account.status == "rate_limited" else 0,
                    )
                    return RefreshResult("cooldown", max(1, math.ceil(deadline - current)))
                provider_id = device.provider_id
            try:
                async with asyncio.timeout(POLL_TIMEOUT_SECONDS):
                    credentials = self.vault.open(account.credentials)
                    tokens = self.vault.open(account.tokens) if account.tokens else None
                    if command is None:
                        result = await self.provider.status(credentials, tokens, provider_id)
                    else:
                        result = await self.provider.control(
                            credentials, tokens, provider_id, command
                        )
            except ProviderError as exc:
                result = PollResult([], failure=exc)
            except asyncio.CancelledError:
                self.finish(account, PollResult([], failure=ProviderError()), complete=False)
                raise
            except Exception:
                logger.warning(
                    "provider_control_failed account=%s device=%s", account.id, device_id
                )
                result = PollResult([], failure=ProviderError("control_failed"))
            self.finish(account, result, complete=False)
            return RefreshResult(
                result.failure.kind if result.failure else "controlled",
                math.ceil(result.failure.retry_after) if result.failure else 0,
                result.control_confirmed,
            )

    def finish(self, original: Account, result: PollResult, *, complete: bool = True) -> None:
        current = time.time()
        with self.sessions() as db:
            # Fencing: a late worker cannot overwrite a new lease's results or a deleted account.
            owned = db.execute(
                update(Account)
                .where(Account.id == original.id, Account.lease_token == original.lease_token)
                .values(lease_until=0.0, lease_token=None)
            ).rowcount
            if not owned:
                db.rollback()
                return
            account = db.get(Account, original.id)
            if result.tokens:
                account.tokens = self.vault.seal(result.tokens)
            failure = result.failure
            if failure and failure.kind not in {"invalid_control", "control_failed"}:
                account.error = str(failure)
                account.failure_count += 1
                if failure.kind == "auth":
                    account.status = "auth_error"
                    account.tokens = None
                elif failure.kind == "rate_limit":
                    account.status = "rate_limited"
                    account.rate_limit_count += 1
                    delay = min(1800, 300 * 2 ** min(account.rate_limit_count - 1, 3))
                    account.next_poll_at = current + max(delay, failure.retry_after)
                else:
                    account.status = "unavailable"
                    delay = min(900, 60 * 2 ** min(account.failure_count - 1, 4))
                    account.next_poll_at = current + max(delay, self.settings.poll_interval_seconds)
            elif complete:
                account.status = "connected"
                account.error = None
                account.failure_count = 0
                account.rate_limit_count = 0
                account.last_success_at = current
                # Measure the interval between cycle starts; this works with minute cron ticks.
                account.next_poll_at = max(
                    original.last_attempt_at + self.settings.poll_interval_seconds,
                    current + 1,
                )
            else:
                account.next_poll_at = max(
                    original.next_poll_at, current + CONTROL_COOLDOWN_SECONDS
                )

            existing = {
                device.provider_id: device
                for device in db.scalars(select(Device).where(Device.account_id == account.id))
            }
            for snapshot in result.devices:
                device = existing.get(snapshot.provider_id)
                if device is None:
                    device = Device(account_id=account.id, provider_id=snapshot.provider_id)
                    db.add(device)
                device.name = snapshot.name
                device.model = snapshot.model
                device.supported = snapshot.supported
                device.capabilities = snapshot.capabilities
                device.controls = snapshot.controls
                device.error = snapshot.error
                if snapshot.retrieved_at is not None:
                    device.state = {
                        key: (
                            None if isinstance(value, float) and not math.isfinite(value) else value
                        )
                        for key, value in snapshot.state.items()
                    }
                    device.retrieved_at = snapshot.retrieved_at
                elif snapshot.state.get("connectivity") == "offline":
                    # Connectivity may be known even when fresh sensor readings are unavailable.
                    device.state = {**(device.state or {}), "connectivity": "offline"}
                record_reading(db, device, snapshot, current)
            # A failed or incomplete poll never removes existing devices.
            if complete and failure is None:
                seen = {snapshot.provider_id for snapshot in result.devices}
                for provider_id, device in existing.items():
                    if provider_id not in seen:
                        db.delete(device)
            prune_readings(db, current)
            db.commit()

    async def tick(self) -> dict:
        with self.sessions() as db:
            current = time.time()
            ids = list(
                db.scalars(
                    select(Account.id).where(
                        Account.next_poll_at <= current,
                        Account.lease_until <= current,
                        Account.status != "auth_error",
                    )
                )
            )
        results = await asyncio.gather(*(self.refresh(account_id) for account_id in ids))
        return {"checked": len(ids), "refreshed": sum(r.status == "refreshed" for r in results)}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                with self.sessions() as db:
                    current = time.time()
                    from climate_control.db import LoginSession, LoginThrottle, OAuthFlow

                    for model in (LoginSession, LoginThrottle, OAuthFlow):
                        db.execute(delete(model).where(model.expires_at <= current))
                    db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("scheduler_tick_failed")
            await asyncio.sleep(5)
