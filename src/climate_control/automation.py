"""Database-backed interpreter and durable agent reconsideration inbox."""

import copy
import math
import time
from uuid import uuid4

from sqlalchemy import delete, select, update

from climate_control.db import (
    Account,
    Automation,
    AutomationEvent,
    Device,
    DeviceReading,
    OutdoorConfiguration,
    OutdoorReading,
    OutdoorStation,
    PlanningJob,
    RuleRevision,
)
from climate_control.history import QUALITY_LEVELS
from climate_control.providers.base import DeviceCommand
from climate_control.rules import Ruleset, evaluate

LEASE_SECONDS = 300


def current_rules(db, state):
    row = db.scalar(
        select(RuleRevision).where(
            RuleRevision.device_id == state.device_id, RuleRevision.revision == state.revision
        )
    )
    return Ruleset.model_validate(row.ruleset) if row else None


def validate_capabilities(db, device, ruleset, *, check_sources=True):
    if not device.supported:
        raise ValueError("This device is unsupported.")
    for action in [ruleset.fallback, *(rule.action for rule in ruleset.rules)]:
        if action.power != "hold" and not device.controls.get("power"):
            raise ValueError("This device cannot control power.")
        if isinstance(action.fan, int) and action.fan not in device.controls.get("fan_levels", []):
            raise ValueError("The requested fan level is unsupported.")
        if isinstance(action.fan, str) and action.fan not in device.controls.get("modes", []):
            raise ValueError("The requested fan mode is unsupported.")
    for source in ruleset.sources().values() if check_sources else []:
        if source.kind == "device":
            referenced = db.get(Device, source.device_id)
            if not referenced or source.metric not in referenced.capabilities:
                raise ValueError("A referenced device or metric is unavailable.")


def fresh_device(db, device, settings, current):
    if not device:
        return False
    account = db.get(Account, device.account_id)
    return bool(
        account
        and not account.error
        and not device.error
        and device.supported
        and device.state.get("connectivity") == "online"
        and device.retrieved_at is not None
        and 0
        <= current - device.retrieved_at
        <= max(settings.stale_after_seconds, settings.poll_interval_seconds * 3)
    )


def collect_inputs(db, ruleset, settings, current):
    values = {}
    config = db.get(OutdoorConfiguration, 1)
    station = db.get(OutdoorStation, config.location_id) if config and config.location_id else None
    for key, source in ruleset.sources().items():
        if source.kind == "device":
            device = db.get(Device, source.device_id)
            value = device.state.get(source.metric) if device else None
            if source.metric == "air_quality":
                value = QUALITY_LEVELS.get(str(value).lower())
            timestamp = device.retrieved_at if device else None
            available = fresh_device(db, device, settings, current) and (
                source.metric in device.capabilities
            )
            unit = "rank" if source.metric == "air_quality" else "µg/m³"
            identity = device.id if device else None
        else:
            metric = source.metric.removesuffix("_aqi")
            reading = station.readings.get(metric, {}) if station else {}
            value, timestamp = reading.get("value"), reading.get("recorded_at")
            available = timestamp is not None and 0 <= current - timestamp <= (
                settings.outdoor_stale_after_seconds
            )
            identity = station.location_id if station else None
            unit = "AQI"
        numeric = type(value) in {float, int} and math.isfinite(value) and value >= 0
        values[key] = {
            "value": value if numeric else None,
            "at": timestamp,
            "available": bool(available and numeric),
            "unit": unit,
            "source_id": identity,
            "age_seconds": current - timestamp if timestamp is not None else None,
        }
    return values


def request_review(db, state, reason, current, *, force=False, cooldown=1800):
    if state.owner != "agent":
        return
    job = db.get(PlanningJob, state.device_id)
    if job and job.status in {"pending", "claimed"}:
        reasons = list(job.reasons)
        if not any(item["reason"] == reason["reason"] for item in reasons):
            job.reasons = [*reasons[-63:], reason]
        if force:
            job.available_at = min(current, job.available_at)
        return
    if not job:
        job = PlanningJob(device_id=state.device_id)
        db.add(job)
    job.request_id = str(uuid4())
    job.status = "pending"
    job.generation = state.generation
    job.base_revision = state.revision
    job.reasons = [reason]
    job.created_at = current
    job.available_at = current if force else max(current, state.last_review_request_at + cooldown)
    job.lease_token = None
    job.lease_until = 0
    job.attempts = 0
    job.claimed_reasons = 0
    state.last_review_request_at = job.available_at


def source_samples(db, source, current, window, settings):
    if source.kind == "device":
        query = (
            select(DeviceReading.recorded_at, getattr(DeviceReading, source.metric))
            .where(
                DeviceReading.device_id == source.device_id,
                DeviceReading.recorded_at >= current - window,
                DeviceReading.recorded_at <= current,
            )
            .order_by(DeviceReading.recorded_at.desc())
            .limit(101)
        )
        gap = max(settings.stale_after_seconds, settings.poll_interval_seconds * 3)
    else:
        config = db.get(OutdoorConfiguration, 1)
        if not config or not config.location_id:
            return [], settings.outdoor_stale_after_seconds
        query = (
            select(OutdoorReading.recorded_at, OutdoorReading.value)
            .where(
                OutdoorReading.location_id == config.location_id,
                OutdoorReading.metric == source.metric.removesuffix("_aqi"),
                OutdoorReading.recorded_at >= current - window,
                OutdoorReading.recorded_at <= current,
            )
            .order_by(OutdoorReading.recorded_at.desc())
            .limit(101)
        )
        gap = settings.outdoor_stale_after_seconds
    samples = []
    for at, value in reversed(list(db.execute(query))):
        if value is None:
            samples = []
            continue
        if samples and at - samples[-1][0] > gap:
            samples = []
        samples.append([at, value])
    return samples, gap


def stable_on(db, device, start, end, gap):
    rows = list(
        db.scalars(
            select(DeviceReading)
            .where(
                DeviceReading.device_id == device.id,
                DeviceReading.recorded_at >= start - gap,
                DeviceReading.recorded_at <= end,
            )
            .order_by(DeviceReading.recorded_at)
        )
    )
    before = [r for r in rows if r.recorded_at <= start]
    if not before:
        return False
    rows = [before[-1], *(r for r in rows if r.recorded_at > start)]
    expected = (device.state.get("mode"), device.state.get("fan_level"))
    return (
        all(r.power == "on" and (r.mode, r.fan_level) == expected for r in rows)
        and all(b.recorded_at - a.recorded_at <= gap for a, b in zip(rows, rows[1:], strict=False))
        and end - rows[-1].recorded_at <= gap
    )


def reconsider(db, state, device, ruleset, inputs, runtime, settings, current):
    if state.owner != "agent":
        return
    cooldown = ruleset.reconsideration_cooldown_seconds if ruleset else 1800

    def queue(reason, **evidence):
        request_review(db, state, {"reason": reason, **evidence}, current, cooldown=cooldown)

    if current >= state.next_review_at:
        queue("periodic_review")
        state.next_review_at = current + (ruleset.review_after_seconds if ruleset else 86400)
    signature = {
        "controls": device.controls,
        "capabilities": device.capabilities,
        "sources": {k: v["source_id"] for k, v in inputs.items()},
    }
    if runtime.get("configuration") is not None and runtime["configuration"] != signature:
        queue("capabilities_or_sources_changed")
    runtime["configuration"] = signature
    trigger_state = runtime.setdefault("triggers", {})
    for trigger in ruleset.triggers if ruleset else []:
        previous = trigger_state.get(trigger.id, {})
        fired = False
        evidence = {}
        if trigger.kind == "failures":
            active = runtime.get("failures", 0) >= trigger.readings
            fired = active and not previous.get("active", False)
            previous = {"active": active}
        else:
            value = inputs[trigger.source.key()]
            evidence = {"input": value}
            if trigger.kind == "availability":
                fired = "available" in previous and previous["available"] != value["available"]
                previous = {"available": value["available"]}
            elif not value["available"]:
                previous = {}
            elif trigger.kind in {"above", "below"}:
                active = previous.get("active", False)
                number = value["value"]
                if active and (
                    number <= trigger.reset if trigger.kind == "above" else number >= trigger.reset
                ):
                    active = False
                crossed = (
                    number >= trigger.threshold
                    if trigger.kind == "above"
                    else (number <= trigger.threshold)
                )
                fired = crossed and not active
                previous = {"active": active or crossed}
            else:
                samples, _ = source_samples(
                    db, trigger.source, current, trigger.window_seconds, settings
                )
                samples = samples[-trigger.readings :]
                active = len(samples) == trigger.readings and all(
                    v >= trigger.threshold for _, v in samples
                )
                if active and trigger.kind == "not_improving":
                    active = samples[0][1] - samples[-1][1] < trigger.improvement and (
                        not runtime.get("pending")
                        and stable_on(
                            db,
                            device,
                            samples[0][0],
                            current,
                            max(settings.stale_after_seconds, settings.poll_interval_seconds * 3),
                        )
                    )
                fired = active and not previous.get("active", False)
                previous = {"active": active}
                evidence["samples"] = samples
        trigger_state[trigger.id] = previous
        if fired:
            queue(f"trigger:{trigger.id}", **evidence)


def next_command(action, device):
    state = device.state
    if action["power"] == "hold":
        return None
    if action["power"] != state.get("power"):
        return DeviceCommand(power=action["power"] == "on")
    if action["power"] == "off":
        return None
    fan = action["fan"]
    if isinstance(fan, int):
        return (
            None
            if state.get("mode") == "manual" and state.get("fan_level") == fan
            else (DeviceCommand(fan_level=fan))
        )
    return None if state.get("mode") == fan else DeviceCommand(mode=fan)


def command_matches(command, device):
    if command.get("power") is not None:
        return device.state.get("power") == ("on" if command["power"] else "off")
    if command.get("mode") is not None:
        return device.state.get("mode") == command["mode"]
    return device.state.get("mode") == "manual" and (
        device.state.get("fan_level") == command.get("fan_level")
    )


class RuleEngine:
    def __init__(self, sessions, settings, poller):
        self.sessions, self.settings, self.poller = sessions, settings, poller

    async def tick(self):
        current = time.time()
        with self.sessions() as db:
            ids = list(
                db.scalars(
                    select(Automation.device_id)
                    .where((Automation.enabled.is_(True)) | (Automation.owner == "agent"))
                    .order_by(Automation.device_id)
                )
            )
            db.execute(
                delete(AutomationEvent).where(AutomationEvent.created_at < current - 30 * 86400)
            )
            db.commit()
        for device_id in ids:
            await self.step(device_id)
        return len(ids)

    async def step(self, device_id):
        current, token = time.time(), str(uuid4())
        with self.sessions() as db:
            if not db.execute(
                update(Automation)
                .where(Automation.device_id == device_id, Automation.lease_until <= current)
                .values(lease_token=token, lease_until=current + LEASE_SECONDS)
            ).rowcount:
                db.rollback()
                return
            state = db.get(Automation, device_id)
            device = db.get(Device, device_id)
            ruleset = current_rules(db, state)
            runtime = copy.deepcopy(state.runtime)
            inputs = collect_inputs(db, ruleset, self.settings, current) if ruleset else {}
            reconsider(db, state, device, ruleset, inputs, runtime, self.settings, current)
            decision = evaluate(ruleset, inputs, current) if ruleset else None
            command, inhibition = None, None
            if not state.enabled or not ruleset:
                inhibition = "disabled" if not state.enabled else "waiting_for_rules"
            elif state.paused_until > current:
                inhibition = "paused"
            elif not fresh_device(db, device, self.settings, current):
                inhibition = "target_unavailable"
            else:
                try:
                    validate_capabilities(db, device, ruleset, check_sources=False)
                except ValueError:
                    inhibition = "unsupported_action"
                if not inhibition:
                    command, inhibition = self.prepare(
                        db, state, device, ruleset, decision, inputs, runtime, current
                    )
            summary = {"decision": decision, "inhibition": inhibition}
            if runtime.get("summary") != summary and inhibition not in {"already_applied", "hold"}:
                db.add(
                    AutomationEvent(
                        device_id=device_id,
                        revision=state.revision,
                        details={**summary, "inputs": inputs},
                    )
                )
            runtime["summary"] = summary
            state.runtime = runtime
            generation, revision = state.generation, state.revision
            db.commit()
        try:
            if command:

                def guard(db, device, now):
                    # Called within the account-claim transaction, before provider dispatch.
                    if not db.execute(
                        update(Automation)
                        .where(
                            Automation.device_id == device_id,
                            Automation.lease_token == token,
                            Automation.generation == generation,
                            Automation.revision == revision,
                            Automation.enabled.is_(True),
                            Automation.paused_until <= now,
                            Automation.lease_until > now,
                        )
                        .values(lease_until=now + LEASE_SECONDS)
                    ).rowcount:
                        return False
                    owned = db.get(Automation, device_id, populate_existing=True)
                    latest = current_rules(db, owned)
                    db.refresh(device)
                    current_inputs = collect_inputs(db, latest, self.settings, now)
                    current_decision = evaluate(latest, current_inputs, now)
                    if not fresh_device(db, device, self.settings, now) or (
                        current_decision["action"] != decision["action"]
                        or current_decision["rule_id"] != decision["rule_id"]
                        or next_command(current_decision["action"], device) != command
                    ):
                        return False
                    saved = copy.deepcopy(owned.runtime)
                    saved["pending"] = {
                        "command": vars(command),
                        "sent_at": now,
                        "observation_at": device.retrieved_at,
                        "outcome": "uncertain",
                    }
                    saved["attempts"] = saved.get("attempts", 0) + 1
                    owned.runtime = saved
                    db.add(
                        AutomationEvent(
                            device_id=device_id,
                            revision=revision,
                            details={
                                "intent": vars(command),
                                "decision": current_decision,
                                "inputs": current_inputs,
                            },
                        )
                    )
                    return True

                result = await self.poller.control(device_id, command, guard=guard)
                with self.sessions() as db:
                    exists = db.execute(
                        update(Automation)
                        .where(Automation.device_id == device_id)
                        .values(generation=Automation.generation)
                    ).rowcount
                    dispatched = result.status not in {
                        "busy",
                        "cooldown",
                        "superseded",
                        "auth_error",
                        "missing",
                    }
                    if db.execute(
                        update(Automation)
                        .where(
                            Automation.device_id == device_id,
                            Automation.lease_token == token,
                            Automation.generation == generation,
                        )
                        .values(lease_until=time.time() + LEASE_SECONDS)
                    ).rowcount:
                        state = db.get(Automation, device_id, populate_existing=True)
                        runtime = copy.deepcopy(state.runtime)
                        if dispatched:
                            pending = runtime.get("pending")
                            if pending:
                                pending["outcome"] = result.status
                        state.runtime = runtime
                    # A takeover fences runtime writes, but the old intent still needs
                    # its observed outcome. Deletion removes both intent and outcome.
                    if exists and dispatched:
                        db.add(
                            AutomationEvent(
                                device_id=device_id,
                                revision=revision,
                                details={
                                    "outcome": result.status,
                                    "confirmed": result.status == "controlled"
                                    and result.control_confirmed,
                                },
                            )
                        )
                    db.commit()
        finally:
            with self.sessions() as db:
                db.execute(
                    update(Automation)
                    .where(Automation.device_id == device_id, Automation.lease_token == token)
                    .values(lease_token=None, lease_until=0)
                )
                db.commit()

    def prepare(self, db, state, device, ruleset, decision, inputs, runtime, current):
        pending = runtime.get("pending")
        if pending:
            if device.retrieved_at > pending["observation_at"] and command_matches(
                pending["command"], device
            ):
                runtime.pop("pending")
                runtime["attempts"] = 0
                runtime["failures"] = 0
                # The fan stage of power-on is allowed without another transition delay.
                fan_stage_remains = (
                    pending["command"].get("power") is True
                    and decision["action"]["power"] == "on"
                    and next_command(decision["action"], device) is not None
                )
                if not fan_stage_remains:
                    runtime["last_change_at"] = current
            elif current < pending["sent_at"] + 90:
                return None, "awaiting_confirmation"
            elif device.retrieved_at <= pending["sent_at"]:
                # Existing scheduled polls own status recovery. Never blindly resend.
                return None, "awaiting_fresh_status"
            else:
                runtime.pop("pending")
                runtime["failures"] = runtime.get("failures", 0) + 1
                runtime["retry_at"] = current + min(900, 30 * 2 ** runtime["failures"])
                db.add(
                    AutomationEvent(
                        device_id=device.id,
                        revision=state.revision,
                        details={"outcome": "unconfirmed"},
                    )
                )
        key = {"rule_id": decision["rule_id"], "action": decision["action"]}
        evidence = {k: inputs[k]["at"] for k in decision["evidence_keys"] if inputs[k]["available"]}
        if not evidence:
            evidence = {"target": device.retrieved_at}
        candidate = runtime.get("candidate", {})
        if candidate.get("key") != key:
            candidate = {"key": key, "evidence": evidence, "count": 1, "at": current}
            runtime["attempts"] = 0
            runtime["retry_at"] = 0
        elif current - candidate.get("at", 0) > max(
            self.settings.stale_after_seconds,
            self.settings.poll_interval_seconds * 3,
            self.settings.outdoor_stale_after_seconds
            if any(k.startswith("outdoor:") for k in evidence)
            else 0,
        ):
            candidate.update(evidence=evidence, count=1, at=current)
        elif evidence and any(
            at is not None and at > candidate.get("evidence", {}).get(k, 0)
            for k, at in evidence.items()
        ):
            candidate.update(evidence=evidence, count=candidate["count"] + 1, at=current)
        runtime["candidate"] = candidate
        command = next_command(decision["action"], device)
        if command is None:
            return None, "hold" if decision["action"]["power"] == "hold" else "already_applied"
        if runtime.get("attempts", 0) >= 3:
            return None, "retry_limit"
        if runtime.get("retry_at", 0) > current:
            return None, "retry_backoff"
        if decision["reading_based"] and candidate["count"] < ruleset.consecutive_readings:
            return None, "waiting_for_readings"
        if current < runtime.get("last_change_at", 0) + ruleset.min_change_seconds:
            return None, "minimum_change_interval"
        return command, None
