"""Agent tools shared by the in-app planner (ADK) and external clients (MCP).

Each tool takes a ToolContext first. Adapters bind that context and expose the remaining
typed parameters and docstring to the model. Tools read cached data; only control tools
reach providers, through the poller's leases, pacing, and backoff.
"""

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from climate_control import automation_api as rules_api
from climate_control.agent.planner import PLANNER_TOOLS
from climate_control.api import (
    ControlInput,
    DeviceDisplaySettingsInput,
    GlobalDisplaySettingsInput,
    control,
    device_history_view,
    devices_view,
    get_display_settings_view,
    refresh_status,
    update_device_display_settings,
    update_global_display_settings,
)
from climate_control.automation_api import AgentProposal, Configuration, JobLease, Pause, Proposal
from climate_control.display import DisplaySelection, WarningThresholdId
from climate_control.outdoor import outdoor_view
from climate_control.rules import Ruleset

SCOPES = ("read", "control", "rules")
ERROR_KINDS = {
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "invalid",
    429: "rate_limited",
    502: "provider_error",
    503: "unavailable",
    504: "timeout",
}


@dataclass
class ToolContext:
    sessions: sessionmaker
    settings: Any
    actor: str
    scopes: frozenset[str]
    poller: Any = None
    planning_trigger: Any = None


class ToolError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.kind, self.message, self.retry_after = kind, message, retry_after

    def payload(self) -> dict:
        error = {"error": self.kind, "message": self.message}
        if self.retry_after:
            error["retry_after_seconds"] = self.retry_after
        return error


@dataclass
class ToolSpec:
    name: str
    scope: str
    fn: Any
    writes: bool = False
    parameters: list[inspect.Parameter] = field(default_factory=list)


TOOLS: dict[str, ToolSpec] = {}


def tool(scope: str, *, writes=False):
    def register(fn):
        parameters = list(inspect.signature(fn).parameters.values())[1:]
        TOOLS[fn.__name__] = ToolSpec(fn.__name__, scope, fn, writes, parameters)
        return fn

    return register


def validation_message(exc: ValidationError) -> str:
    details = [
        f"{'.'.join(str(part) for part in error['loc']) or 'value'}: {error['msg']}"
        for error in exc.errors()[:20]
    ]
    return "Invalid input. " + "; ".join(details)


async def invoke(spec: ToolSpec, ctx: ToolContext, arguments: dict) -> Any:
    """Run one tool with scope checks and errors normalized for models."""
    if spec.scope not in ctx.scopes:
        raise ToolError("forbidden", f"This access token lacks the {spec.scope} scope.")
    try:
        if inspect.iscoroutinefunction(spec.fn):
            return await spec.fn(ctx, **arguments)
        # Database-only tools run off the event loop, like FastAPI's sync routes.
        return await asyncio.to_thread(spec.fn, ctx, **arguments)
    except HTTPException as exc:
        retry = (exc.headers or {}).get("Retry-After")
        raise ToolError(
            ERROR_KINDS.get(exc.status_code, "failed"),
            str(exc.detail),
            int(retry) if retry and retry.isdigit() else None,
        ) from None
    except ValidationError as exc:
        raise ToolError("invalid", validation_message(exc)) from None


def device_actions(device: dict) -> dict:
    """Allowed rule actions and metrics for one device, from its reported capabilities."""
    controls = device.get("controls") or {}
    return {
        "power": bool(controls.get("power")),
        "fan": [*controls.get("fan_levels", []), *controls.get("modes", [])],
        "metrics": [
            metric
            for metric in ("pm25", "pm1", "pm10", "air_quality", "temperature", "humidity")
            if metric in (device.get("capabilities") or [])
        ],
    }


def drop_urls(value: dict) -> dict:
    return {key: item for key, item in value.items() if not key.endswith("_url")}


# Read tools


@tool("read")
def list_devices(ctx: ToolContext) -> list:
    """List every device with cached state, capabilities, controls, and automation summary.

    Readings come from the database cache; `stale` marks old data. PM values are µg/m³.
    """
    return devices_view(ctx)


@tool("read")
def get_device(ctx: ToolContext, device_id: str) -> dict:
    """Get one device's cached state, capabilities, controls, and automation summary."""
    for device in devices_view(ctx):
        if device["id"] == device_id:
            return device
    raise ToolError("not_found", "Device not found.")


@tool("read")
def get_device_history(
    ctx: ToolContext,
    device_id: str,
    hours: int = 24,
    raw: bool = False,
    after: float | None = None,
    limit: int = 500,
) -> dict:
    """Get indoor reading history for a device over 24, 168, or 720 hours.

    Summaries are bucketed; set raw=true for paired observations. For more raw pages, pass
    the returned `next_after` as `after`. Missing samples are gaps, not zero values.
    """
    return device_history_view(ctx, device_id, hours, raw, after, max(1, min(limit, 5000)))


@tool("read")
def get_outdoor_air_quality(ctx: ToolContext) -> dict:
    """Get the selected outdoor station and its latest readings, in AQI units."""
    with ctx.sessions() as db:
        return outdoor_view(db, ctx.settings)


@tool("read")
def get_outdoor_history(ctx: ToolContext, after: int = 0, limit: int = 500) -> dict:
    """Get exact outdoor observations (AQI) from the last 30 days, oldest first.

    For more pages, pass the returned `next_after` as `after`.
    """
    return rules_api.outdoor_observations(ctx, after, max(1, min(limit, 5000)))


@tool("read")
def get_rule_schema(ctx: ToolContext) -> dict:
    """Get the authoritative JSON Schema for rulesets (version 1)."""
    return Ruleset.model_json_schema()


@tool("read")
def get_device_automation(ctx: ToolContext, device_id: str, history_limit: int = 200) -> dict:
    """Get a device's planning context.

    Includes ownership, enabled and pause state, generation and revision, current rules,
    latest decision, planning job, all devices with capabilities and freshness, outdoor
    data, current rule inputs, units, recent raw target history, and `allowed_actions`
    for the target device. Use get_device_history with raw=true for older target data.
    """
    limit = max(1, min(history_limit, 1000))
    context = drop_urls(rules_api.context_view(ctx, device_id, history_limit=limit))
    context["devices"] = [drop_urls(device) for device in context["devices"]]
    target = next((d for d in context["devices"] if d["id"] == device_id), {})
    context["allowed_actions"] = device_actions(target)
    return context


@tool("read")
def list_rule_events(ctx: ToolContext, device_id: str, before: int | None = None) -> list:
    """List up to 50 recent automation events (decisions, commands, outcomes), newest first.

    Paginate with `before` set to the smallest event ID received.
    """
    return rules_api.events_view(ctx, device_id, before)


@tool("read")
def list_rule_revisions(ctx: ToolContext, device_id: str, before: int | None = None) -> list:
    """List up to 10 rule revisions with author and reason, newest first.

    Paginate with `before` set to the smallest revision received.
    """
    return rules_api.revisions_view(ctx, device_id, before, limit=10)


@tool("read")
def preview_rules(ctx: ToolContext, device_id: str, ruleset: dict) -> dict:
    """Validate a ruleset for a device and evaluate it against current data.

    Sends no commands. Returns the decision, inputs, and target availability, or the
    validation errors to fix. A preview covers only the current moment: design for every
    time window and for missing outdoor or other-device data.
    """
    return rules_api.preview_rules(ctx, device_id, Ruleset.model_validate(ruleset))


@tool("read")
def get_display_settings(ctx: ToolContext) -> dict:
    """Get the global air quality warning threshold and available WHO threshold presets."""
    return get_display_settings_view(ctx)


# Control tools


async def _control(ctx: ToolContext, device_id: str, **change) -> dict:
    return await control(ctx, device_id, ControlInput(**change), ctx.actor)


@tool("control", writes=True)
async def set_device_power(ctx: ToolContext, device_id: str, on: bool) -> dict:
    """Turn a device on or off. Pauses its rules for the configured pause duration."""
    return await _control(ctx, device_id, power=on)


@tool("control", writes=True)
async def set_device_fan_level(ctx: ToolContext, device_id: str, level: int) -> dict:
    """Set a manual fan level from the device's `controls.fan_levels`. Pauses its rules.

    The device must already be on.
    """
    return await _control(ctx, device_id, fan_level=level)


@tool("control", writes=True)
async def set_device_mode(ctx: ToolContext, device_id: str, mode: Literal["auto", "sleep"]) -> dict:
    """Set auto or sleep mode, if listed in the device's `controls.modes`. Pauses its rules.

    The device must already be on.
    """
    return await _control(ctx, device_id, mode=mode)


@tool("control", writes=True)
async def refresh_device_status(ctx: ToolContext, device_id: str) -> dict:
    """Read a device's current status from its provider now, respecting provider pacing."""
    return await refresh_status(ctx, device_id)


@tool("control", writes=True)
async def set_device_display(
    ctx: ToolContext,
    device_id: str,
    mode: DisplaySelection,
) -> dict:
    """Control a device's display or set its display mode.

    `on`, `off`, `bright`, and `dim` switch the display mode to `manual` and only send a provider
    command if the physical display is not already in that state (the device must be on). `manual`
    stops warning display modes without toggling the physical display. `warnings_only`,
    `warnings_only_bright`, and `warnings_only_dim` evaluate and actuate on new device readings.
    Does not pause automation rules.
    """
    return await update_device_display_settings(
        ctx, device_id, DeviceDisplaySettingsInput(mode=mode)
    )


@tool("control", writes=True)
async def set_display_warning_threshold(
    ctx: ToolContext,
    warning_threshold: WarningThresholdId | None = None,
    device_id: str | None = None,
) -> dict:
    """Set the WHO warning threshold for `warnings_only` display mode.

    Omit `device_id` to set the global threshold (`who_2021_annual`, `who_2021_24h`,
    `who_2005_annual`, or `who_2005_24h`). Pass `device_id` to set a per-device override,
    or pass `warning_threshold=None` with `device_id` to clear the device override.
    """
    if device_id is None:
        if warning_threshold is None:
            raise ToolError(
                "invalid",
                "Provide a warning_threshold when updating the global display setting.",
            )
        return await asyncio.to_thread(
            update_global_display_settings,
            ctx,
            GlobalDisplaySettingsInput(warning_threshold=warning_threshold),
        )
    return await update_device_display_settings(
        ctx,
        device_id,
        DeviceDisplaySettingsInput(warning_threshold=warning_threshold),
    )


# Rule tools


@tool("rules", writes=True)
def save_device_rules(
    ctx: ToolContext,
    device_id: str,
    ruleset: dict,
    reason: str,
    generation: int,
    base_revision: int,
) -> dict:
    """Save rules for a manually managed device.

    Pass `generation` and `revision` from get_device_automation as `generation` and
    `base_revision`; a conflict means the automation changed, so reread it. Agent-managed
    devices accept rules only through the planning job tools. Saving does not enable rules.
    """
    body = Proposal(
        ruleset=ruleset, reason=reason, generation=generation, base_revision=base_revision
    )
    return rules_api.save_manual_rules(ctx, device_id, body, ctx.actor)


@tool("rules", writes=True)
def configure_device_automation(
    ctx: ToolContext,
    device_id: str,
    owner: Literal["manual", "agent"],
    enabled: bool,
    generation: int,
    base_revision: int,
    pause_seconds: int = 3600,
) -> dict:
    """Set rule ownership and whether rules run.

    `agent` ownership hands the device to the planner and queues a planning request.
    Manual devices need saved rules before enabling. `pause_seconds` is the pause applied
    after direct control (60–86400).
    """
    body = Configuration(
        owner=owner,
        enabled=enabled,
        pause_seconds=pause_seconds,
        generation=generation,
        base_revision=base_revision,
    )
    return rules_api.configure_automation(ctx, device_id, body)


@tool("rules", writes=True)
def pause_device_automation(ctx: ToolContext, device_id: str, seconds: int = 3600) -> dict:
    """Pause a device's rules for 60–86400 seconds."""
    return rules_api.pause_automation(ctx, device_id, Pause(seconds=seconds))


@tool("rules", writes=True)
def resume_device_automation(ctx: ToolContext, device_id: str) -> dict:
    """Resume a paused device's rules."""
    return rules_api.resume_automation(ctx, device_id)


@tool("rules", writes=True)
def request_rule_review(ctx: ToolContext, device_id: str) -> dict:
    """Ask the planner to review an agent-managed device's rules now."""
    return rules_api.request_reconsideration(ctx, device_id)


@tool("rules")
def list_planning_jobs(ctx: ToolContext) -> list:
    """List agent-managed devices with planning requests that can be claimed now."""
    jobs = []
    for device_id in rules_api.claimable_jobs(ctx):
        state = rules_api.automation_view(ctx, device_id)
        jobs.append({"device_id": device_id, **(state["job"] or {})})
    return jobs


@tool("rules", writes=True)
def claim_planning_job(ctx: ToolContext, device_id: str) -> dict:
    """Claim a device's planning request for five minutes.

    Keep `request_id`, `lease_token`, `generation`, and `base_revision` for submission.
    Renew long work; a conflict means another planner has it or nothing is pending.
    """
    return rules_api.claim_planning_job(ctx, device_id)


@tool("rules", writes=True)
def renew_planning_job(ctx: ToolContext, device_id: str, request_id: str, lease_token: str) -> dict:
    """Extend a claimed planning lease by five minutes."""
    return rules_api.renew_planning_job(
        ctx, device_id, JobLease(request_id=request_id, lease_token=lease_token)
    )


@tool("rules", writes=True)
def submit_planning_result(
    ctx: ToolContext,
    device_id: str,
    request_id: str,
    lease_token: str,
    generation: int,
    base_revision: int,
    reason: str,
    ruleset: dict,
) -> dict:
    """Submit rules for a claimed planning request.

    Valid rules activate atomically; unchanged rules confirm the current revision. Invalid
    rules return errors and keep the lease. A conflict means the request is obsolete:
    discard the proposal and reread context before claiming new work.
    """
    body = AgentProposal(
        request_id=request_id,
        lease_token=lease_token,
        generation=generation,
        base_revision=base_revision,
        reason=reason,
        ruleset=ruleset,
    )
    return rules_api.complete_planning_job(ctx, device_id, body, ctx.actor)


@tool("rules", writes=True)
def fail_planning_job(ctx: ToolContext, device_id: str, request_id: str, lease_token: str) -> dict:
    """Release a claimed planning request after failure; it retries with backoff."""
    return rules_api.fail_planning_job(
        ctx, device_id, JobLease(request_id=request_id, lease_token=lease_token)
    )


def text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def adk_tools(ctx: ToolContext, names=PLANNER_TOOLS) -> list:
    """Bind tools to a context as plain functions for ADK; errors return to the model."""
    bound = []
    for name in names:
        spec = TOOLS[name]

        def make(spec):
            async def call(**arguments):
                try:
                    return await invoke(spec, ctx, arguments)
                except ToolError as exc:
                    return exc.payload()

            call.__name__ = spec.name
            call.__doc__ = spec.fn.__doc__
            call.__signature__ = inspect.Signature(spec.parameters)
            call.__annotations__ = {p.name: p.annotation for p in spec.parameters}
            return call

        bound.append(make(spec))
    return bound
