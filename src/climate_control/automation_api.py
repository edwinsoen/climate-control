"""Authenticated rule authoring and model-independent planning protocol."""

import copy
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import select, update

from climate_control.api import authenticated
from climate_control.automation import (
    collect_inputs,
    current_rules,
    fresh_device,
    request_review,
    validate_capabilities,
)
from climate_control.db import (
    Account,
    Automation,
    AutomationEvent,
    Device,
    OutdoorConfiguration,
    OutdoorReading,
    PlanningJob,
    RuleRevision,
)
from climate_control.history import history_view
from climate_control.outdoor import outdoor_view
from climate_control.rules import Ruleset, StrictModel, evaluate

router = APIRouter(
    prefix="/api/devices/{device_id}/automation", dependencies=[Depends(authenticated)]
)


def get_state(db, device_id, *, lock=False):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found.")
    if lock:
        # Same lock order as command dispatch. Lock covers DB changes, not network I/O.
        db.execute(
            update(Account)
            .where(Account.id == device.account_id)
            .values(next_control_at=Account.next_control_at)
        )
        db.execute(
            update(Automation)
            .where(Automation.device_id == device_id)
            .values(generation=Automation.generation)
        )
    state = db.get(Automation, device_id, populate_existing=lock)
    if state is None:
        # Older/manual test-created devices are disabled until explicitly configured.
        state = Automation(
            device_id=device_id,
            owner="manual",
            enabled=False,
            generation=0,
            revision=0,
            paused_until=0,
            pause_seconds=3600,
            runtime={},
            lease_until=0,
            next_review_at=0,
            last_review_request_at=0,
        )
        if lock:
            db.add(state)
            db.flush()
    return device, state


def job_view(job):
    if not job:
        return None
    return {
        key: getattr(job, key)
        for key in (
            "request_id",
            "status",
            "generation",
            "base_revision",
            "reasons",
            "created_at",
            "available_at",
            "lease_until",
            "attempts",
        )
    }


def state_view(db, device, state):
    ruleset = current_rules(db, state)
    return {
        "owner": state.owner,
        "enabled": state.enabled,
        "generation": state.generation,
        "revision": state.revision,
        "paused_until": state.paused_until,
        "pause_seconds": state.pause_seconds,
        "timezone": db.get(Account, device.account_id).time_zone,
        "ruleset": ruleset.model_dump() if ruleset else None,
        "summary": state.runtime.get("summary"),
        "job": job_view(db.get(PlanningJob, device.id)),
    }


def check_version(state, body):
    if state.revision != body.base_revision or state.generation != body.generation:
        raise HTTPException(409, "Automation changed. Reload before saving.")


def invalidate(db, state):
    state.generation += 1
    state.lease_token = None
    state.lease_until = 0
    # Preserve uncertain provider work across rule replacement/restart.
    state.runtime = {
        key: copy.deepcopy(state.runtime[key])
        for key in ("pending", "last_change_at")
        if key in state.runtime
    }
    job = db.get(PlanningJob, state.device_id)
    if job:
        job.status, job.lease_token, job.lease_until = "cancelled", None, 0


def pause_for_manual(db, device_id):
    _, state = get_state(db, device_id, lock=True)
    if state.enabled:
        state.paused_until = time.time() + state.pause_seconds
        state.generation += 1
        state.lease_token, state.lease_until = None, 0
        # Keep agent proposals valid only if they use the new planning generation.
        job = db.get(PlanningJob, device_id)
        if job and job.status in {"pending", "claimed"}:
            job.generation = state.generation
            job.status, job.lease_token, job.lease_until = "pending", None, 0
        db.add(
            AutomationEvent(
                device_id=device_id,
                revision=state.revision,
                details={"inhibition": "manual_pause", "paused_until": state.paused_until},
            )
        )


class Version(StrictModel):
    base_revision: int = Field(ge=0)
    generation: int = Field(ge=0)


class Configuration(Version):
    owner: str = Field(pattern="^(manual|agent)$")
    enabled: bool
    pause_seconds: int = Field(default=3600, ge=60, le=86400)


class Proposal(Version):
    ruleset: Ruleset
    reason: str = Field(min_length=1, max_length=1000)


class Pause(StrictModel):
    seconds: int = Field(default=3600, ge=60, le=86400)


class JobLease(StrictModel):
    request_id: str = Field(min_length=1, max_length=36)
    lease_token: str = Field(min_length=1, max_length=36)


class AgentProposal(Proposal, JobLease):
    pass


@router.get("")
def read_state(device_id: str, request: Request):
    with request.app.state.sessions() as db:
        device, state = get_state(db, device_id)
        return state_view(db, device, state)


@router.get("/schema")
def schema(device_id: str):
    return Ruleset.model_json_schema()


@router.put("")
def configure(device_id: str, body: Configuration, request: Request):
    with request.app.state.sessions() as db:
        device, state = get_state(db, device_id, lock=True)
        check_version(state, body)
        if body.enabled and body.owner == "manual" and state.revision == 0:
            raise HTTPException(422, "Save a ruleset before enabling manual automation.")
        invalidate(db, state)
        state.owner, state.enabled, state.pause_seconds = (
            body.owner,
            body.enabled,
            body.pause_seconds,
        )
        if state.owner == "agent":
            request_review(db, state, {"reason": "configuration_changed"}, time.time(), force=True)
        db.commit()
        return state_view(db, device, state)


@router.post("/pause")
def pause(device_id: str, body: Pause, request: Request):
    with request.app.state.sessions() as db:
        device, state = get_state(db, device_id, lock=True)
        state.paused_until = time.time() + body.seconds
        state.lease_token, state.lease_until = None, 0
        db.commit()
        return state_view(db, device, state)


@router.post("/resume")
def resume(device_id: str, request: Request):
    with request.app.state.sessions() as db:
        device, state = get_state(db, device_id, lock=True)
        state.paused_until = 0
        runtime = copy.deepcopy(state.runtime)
        runtime.pop("candidate", None)
        state.runtime = runtime
        db.commit()
        return state_view(db, device, state)


def validate(db, device, ruleset):
    try:
        validate_capabilities(db, device, ruleset)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/preview")
def preview(device_id: str, body: Ruleset, request: Request):
    with request.app.state.sessions() as db:
        device, _ = get_state(db, device_id)
        validate(db, device, body)
        now = time.time()
        inputs = collect_inputs(db, body, request.app.state.settings, now)
        return {
            "decision": evaluate(body, inputs, now),
            "inputs": inputs,
            "target_available": fresh_device(db, device, request.app.state.settings, now),
        }


def activate(db, state, body, owner):
    validate(db, db.get(Device, state.device_id), body.ruleset)
    invalidate(db, state)
    state.revision += 1
    state.next_review_at = time.time() + body.ruleset.review_after_seconds
    db.add(
        RuleRevision(
            device_id=state.device_id,
            revision=state.revision,
            owner=owner,
            reason=body.reason,
            ruleset=body.ruleset.model_dump(),
        )
    )
    db.flush()


@router.put("/ruleset")
def save_rules(device_id: str, body: Proposal, request: Request):
    with request.app.state.sessions() as db:
        device, state = get_state(db, device_id, lock=True)
        check_version(state, body)
        if state.owner != "manual":
            raise HTTPException(409, "Take manual ownership before editing rules.")
        activate(db, state, body, "manual")
        db.commit()
        return state_view(db, device, state)


@router.get("/revisions")
def revisions(device_id: str, request: Request, before: int | None = None):
    with request.app.state.sessions() as db:
        get_state(db, device_id)
        query = select(RuleRevision).where(RuleRevision.device_id == device_id)
        if before is not None:
            query = query.where(RuleRevision.revision < before)
        rows = list(db.scalars(query.order_by(RuleRevision.revision.desc()).limit(50)))
        return [
            {
                "revision": r.revision,
                "owner": r.owner,
                "reason": r.reason,
                "created_at": r.created_at,
                "ruleset": r.ruleset,
            }
            for r in rows
        ]


@router.get("/events")
def events(device_id: str, request: Request, before: int | None = None):
    with request.app.state.sessions() as db:
        get_state(db, device_id)
        query = select(AutomationEvent).where(AutomationEvent.device_id == device_id)
        if before is not None:
            query = query.where(AutomationEvent.id < before)
        return [
            {"id": r.id, "created_at": r.created_at, "revision": r.revision, **r.details}
            for r in db.scalars(query.order_by(AutomationEvent.id.desc()).limit(50))
        ]


@router.get("/context")
def context(device_id: str, request: Request):
    with request.app.state.sessions() as db:
        device, state = get_state(db, device_id)
        ruleset = current_rules(db, state)
        devices = list(db.scalars(select(Device)))
        config = db.get(OutdoorConfiguration, 1)
        return {
            **state_view(db, device, state),
            "target_device_id": device_id,
            "devices": [
                {
                    "id": d.id,
                    "name": d.name,
                    "capabilities": d.capabilities,
                    "controls": d.controls,
                    "state": d.state,
                    "retrieved_at": d.retrieved_at,
                    "available": fresh_device(db, d, request.app.state.settings, time.time()),
                    "history_url": f"/api/devices/{d.id}/history?hours=720&raw=true",
                }
                for d in devices
            ],
            "outdoor_station_id": config.location_id if config else None,
            "outdoor": outdoor_view(db, request.app.state.settings),
            "outdoor_url": "/api/outdoor",
            "outdoor_history_url": f"/api/devices/{device_id}/automation/outdoor-history",
            "inputs": collect_inputs(db, ruleset, request.app.state.settings, time.time())
            if ruleset
            else {},
            "units": {
                "pm25": "µg/m³",
                "pm1": "µg/m³",
                "pm10": "µg/m³",
                "air_quality": "rank 1–4",
                "outdoor": "AQI",
            },
            "target_history": history_view(db, device_id, 720, time.time(), raw=True, limit=1000),
            "events_url": f"/api/devices/{device_id}/automation/events",
        }


@router.get("/outdoor-history")
def outdoor_history(
    device_id: str,
    request: Request,
    after: int = 0,
    limit: int = Query(default=1000, ge=1, le=5000),
):
    with request.app.state.sessions() as db:
        get_state(db, device_id)
        config = db.get(OutdoorConfiguration, 1)
        if not config or not config.location_id:
            return {"observations": [], "next_after": None}
        rows = list(
            db.scalars(
                select(OutdoorReading)
                .where(
                    OutdoorReading.location_id == config.location_id,
                    OutdoorReading.id > after,
                    OutdoorReading.recorded_at >= time.time() - 30 * 86400,
                )
                .order_by(OutdoorReading.id)
                .limit(limit + 1)
            )
        )
        return {
            "station_id": config.location_id,
            "unit": "AQI",
            "observations": [
                {
                    "id": r.id,
                    "metric": r.metric,
                    "recorded_at": r.recorded_at,
                    "retrieved_at": r.retrieved_at,
                    "value": r.value,
                }
                for r in rows[:limit]
            ],
            "next_after": rows[limit - 1].id if len(rows) > limit else None,
        }


@router.post("/reconsider")
def reconsider(device_id: str, request: Request):
    with request.app.state.sessions() as db:
        device, state = get_state(db, device_id, lock=True)
        if state.owner != "agent":
            raise HTTPException(409, "This device is manually managed.")
        request_review(db, state, {"reason": "user_request"}, time.time(), force=True)
        db.commit()
        return state_view(db, device, state)


@router.post("/jobs/claim")
def claim_job(device_id: str, request: Request):
    with request.app.state.sessions() as db:
        _, state = get_state(db, device_id, lock=True)
        job = db.get(PlanningJob, device_id)
        now = time.time()
        if (
            state.owner != "agent"
            or not job
            or job.status not in {"pending", "claimed"}
            or (job.available_at > now or job.lease_until > now)
        ):
            raise HTTPException(409, "No planning request is available.")
        job.status, job.lease_token, job.lease_until = "claimed", str(uuid4()), now + 300
        job.attempts += 1
        job.claimed_reasons = len(job.reasons)
        db.commit()
        return {**job_view(job), "lease_token": job.lease_token}


def owned_job(db, state, body):
    job = db.get(PlanningJob, state.device_id)
    if (
        state.owner != "agent"
        or not job
        or job.request_id != body.request_id
        or (
            job.status != "claimed"
            or job.lease_token != body.lease_token
            or job.lease_until <= time.time()
            or job.generation != state.generation
            or job.base_revision != state.revision
        )
    ):
        raise HTTPException(409, "This planning lease is expired or superseded.")
    return job


@router.post("/jobs/renew")
def renew_job(device_id: str, body: JobLease, request: Request):
    with request.app.state.sessions() as db:
        _, state = get_state(db, device_id, lock=True)
        job = owned_job(db, state, body)
        job.lease_until = time.time() + 300
        db.commit()
        return job_view(job)


@router.post("/jobs/fail")
def fail_job(device_id: str, body: JobLease, request: Request):
    with request.app.state.sessions() as db:
        _, state = get_state(db, device_id, lock=True)
        job = owned_job(db, state, body)
        job.status, job.lease_token, job.lease_until = "pending", None, 0
        job.available_at = time.time() + min(3600, 60 * 2 ** min(job.attempts, 6))
        db.commit()
        return job_view(job)


@router.post("/jobs/complete")
def complete_job(device_id: str, body: AgentProposal, request: Request):
    with request.app.state.sessions() as db:
        device, state = get_state(db, device_id, lock=True)
        check_version(state, body)
        job = owned_job(db, state, body)
        later_reasons = job.reasons[job.claimed_reasons :]
        activate(db, state, body, "agent")
        job.status, job.lease_token, job.lease_until = "completed", None, 0
        for reason in later_reasons:
            request_review(
                db,
                state,
                reason,
                time.time(),
                cooldown=body.ruleset.reconsideration_cooldown_seconds,
            )
        db.commit()
        return state_view(db, device, state)
