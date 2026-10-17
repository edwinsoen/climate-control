"""Background planner servicing the planning inbox of agent-managed devices.

The worker keeps the planning lease: the model only sees the device ID and the request's
reasons, investigates with read-only tools, and returns a proposal. The worker validates
and submits it through the same path as external planners.
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass

from fastapi import HTTPException
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from pydantic import ValidationError
from sqlalchemy import update

from climate_control import automation_api as rules_api
from climate_control.agent.brief import planning_brief
from climate_control.agent.planner import (
    APP_NAME,
    build_model,
    build_planner,
    parse_proposal,
    planning_message,
    repair_message,
)
from climate_control.agent.tools import ToolContext, adk_tools, validation_message
from climate_control.db import AgentRun, AutomationEvent, new_id

ACTOR = "agent:planner"
USER_ID = "planner"
MAX_REPAIRS = 2
MAX_STEPS = 60
STEP_TEXT = 2000
logger = logging.getLogger(__name__)


@dataclass
class Reply:
    text: str
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class EmbeddedPlanner:
    """Runs the ADK planner in this process with in-process read-only tools."""

    mode = "embedded"

    def __init__(self, sessions, settings, model=None):
        self.settings = settings
        self.model_name = settings.model_name
        self.model = model or build_model(
            settings.model_provider,
            settings.model_name,
            api_key=settings.openrouter_api_key.get_secret_value(),
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
        )
        self.context = ToolContext(
            sessions=sessions, settings=settings, actor=ACTOR, scopes=frozenset({"read"})
        )

    async def start(self):
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        runner = Runner(
            app_name=APP_NAME,
            agent=build_planner(self.model, adk_tools(self.context)),
            session_service=InMemorySessionService(),
        )
        session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        return EmbeddedSession(runner, session.id, self.settings.agent_max_llm_calls)


class EmbeddedSession:
    def __init__(self, runner, session_id, max_llm_calls):
        self.runner, self.session_id, self.remaining = runner, session_id, max_llm_calls

    async def send(self, message: str, record=None) -> Reply:
        from google.adk.agents import RunConfig
        from google.genai import types

        if self.remaining < 1:
            raise PlanningError("llm_limit")
        reply, texts = Reply(""), []
        content = types.Content(role="user", parts=[types.Part(text=message)])
        async for event in self.runner.run_async(
            user_id=USER_ID,
            session_id=self.session_id,
            new_message=content,
            run_config=RunConfig(max_llm_calls=self.remaining),
        ):
            add_usage(reply, event.usage_metadata)
            final = event.is_final_response()
            if event.content and event.content.parts:
                for step in describe(event.content.parts, final):
                    if record:
                        record(step)
            if event.error_code:
                # ADK reports failures, including its LLM call limit, as error events.
                limited = event.error_code == LlmCallsLimitExceededError.__name__
                raise PlanningError("llm_limit" if limited else "model_error")
            if final and event.content and event.content.parts:
                parts = event.content.parts
                texts = [p.text for p in parts if p.text and not p.thought]
        self.remaining -= reply.llm_calls
        reply.text = "\n".join(texts)
        return reply

    async def close(self):
        await self.runner.close()


class RemotePlanner:
    """Queries the planner deployed on Agent Runtime; its tools call this app's MCP API."""

    mode = "remote"

    def __init__(self, settings):
        self.resource = settings.agent_runtime_resource
        self.model_name = "agent-runtime"
        self.runtime = None

    async def start(self):
        if self.runtime is None:
            import agentplatform

            # projects/P/locations/L/reasoningEngines/ID
            parts = self.resource.split("/")
            client = agentplatform.Client(project=parts[1], location=parts[3])
            self.runtime = await asyncio.to_thread(client.runtimes.get, name=self.resource)
        session = await self.runtime.async_create_session(user_id=USER_ID)
        session_id = session["id"] if isinstance(session, dict) else session.id
        return RemoteSession(self.runtime, session_id)


class RemoteSession:
    def __init__(self, runtime, session_id):
        self.runtime, self.session_id = runtime, session_id

    async def send(self, message: str, record=None) -> Reply:
        reply, texts = Reply(""), []
        async for event in self.runtime.async_stream_query(
            message=message, user_id=USER_ID, session_id=self.session_id
        ):
            add_usage(reply, event.get("usage_metadata"))
            parts = (event.get("content") or {}).get("parts") or []
            final = bool(parts) and not any(
                "function_call" in p or "function_response" in p for p in parts
            )
            for step in describe([Remote(p) for p in parts], final):
                if record:
                    record(step)
            if final:
                answer = [p["text"] for p in parts if p.get("text") and not p.get("thought")]
                texts = answer or texts
        reply.text = "\n".join(texts)
        return reply

    async def close(self):
        try:
            await self.runtime.async_delete_session(user_id=USER_ID, session_id=self.session_id)
        except Exception:
            logger.warning("planner_remote_session_cleanup_failed")


class Remote:
    """Reads an Agent Runtime event part with the same attributes as a genai part."""

    def __init__(self, part: dict):
        self.part = part

    def __getattr__(self, name):
        value = self.part.get(name)
        return Remote(value) if isinstance(value, dict) else value


def describe(parts, final: bool) -> list[dict]:
    """Summarize one event's parts as display steps; model output is never stored whole."""
    steps = []
    for part in parts:
        if getattr(part, "thought", None) and part.text:
            steps.append({"kind": "thought", "text": part.text[:STEP_TEXT]})
        elif part.function_call is not None:
            arguments = {
                key: value
                for key, value in (part.function_call.args or {}).items()
                # Rulesets are the planner's output, not a step worth repeating.
                if key != "ruleset"
            }
            steps.append(
                {
                    "kind": "tool",
                    "name": part.function_call.name,
                    "arguments": json.dumps(arguments, separators=(",", ":"), default=str)[:300],
                }
            )
        elif part.function_response is not None:
            response = part.function_response.response
            error = response.get("error") if isinstance(response, dict) else None
            steps.append({"kind": "result", "name": part.function_response.name, "error": error})
        elif part.text and not final:
            steps.append({"kind": "note", "text": part.text[:STEP_TEXT]})
    return [{"at": time.time(), **step} for step in steps]


def add_usage(reply: Reply, usage):
    if not usage:
        return
    get = usage.get if isinstance(usage, dict) else lambda key: getattr(usage, key, None)
    reply.llm_calls += 1
    reply.input_tokens += get("prompt_token_count") or 0
    reply.output_tokens += get("candidates_token_count") or 0


class PlanningError(Exception):
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def create_planner(sessions, settings):
    provider = settings.model_provider
    if provider is None:
        return None
    if provider == "remote":
        return RemotePlanner(settings)
    return EmbeddedPlanner(sessions, settings)


class Recorder:
    """Collects a run's steps and persists them while the run is still in progress."""

    def __init__(self, save, interval=1.0):
        self.save, self.interval = save, interval
        self.steps: list[dict] = []
        self.saved = 0.0

    def record(self, step: dict):
        if len(self.steps) >= MAX_STEPS:
            return
        self.steps.append(step)
        now = time.monotonic()
        if now - self.saved >= self.interval:
            self.saved = now
            self.save(list(self.steps))


class PlannerWorker:
    def __init__(self, sessions, settings, planner):
        self.sessions, self.settings, self.planner = sessions, settings, planner
        self.context = ToolContext(
            sessions=sessions, settings=settings, actor=ACTOR, scopes=frozenset({"rules"})
        )
        self.wake = asyncio.Event()
        self.loop = None

    def notify(self):
        """Wake the loop now; safe to call from request threads."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.wake.set)

    async def tick(self) -> dict:
        """Plan claimable devices one at a time until the time budget is spent."""
        await asyncio.to_thread(self.reconcile)
        counts = {"planning_checked": 0, "planning_completed": 0, "planning_failed": 0}
        deadline = time.monotonic() + self.settings.agent_tick_budget_seconds
        for device_id in await asyncio.to_thread(rules_api.claimable_jobs, self.context):
            if time.monotonic() + self.settings.agent_job_timeout_seconds > deadline:
                break
            status = await self.plan(device_id)
            if status is None:
                continue
            counts["planning_checked"] += 1
            if status in {"completed", "unchanged"}:
                counts["planning_completed"] += 1
            elif status != "superseded":
                counts["planning_failed"] += 1
        return counts

    async def run(self, interval=30):
        self.loop = asyncio.get_running_loop()
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except Exception:
                logger.exception("planner_tick_failed")
            try:
                await asyncio.wait_for(self.wake.wait(), interval)
            except TimeoutError:
                pass

    async def plan(self, device_id: str) -> str | None:
        try:
            lease = await asyncio.to_thread(rules_api.claim_planning_job, self.context, device_id)
        except HTTPException:
            return None  # Claimed elsewhere, changed, or deferred since listing.
        run = AgentRun(
            id=new_id(),
            device_id=device_id,
            request_id=lease["request_id"],
            mode=self.planner.mode,
            model=self.planner.model_name,
            status="running",
            llm_calls=0,
            input_tokens=0,
            output_tokens=0,
            attempts=0,
            started_at=time.time(),
            steps=[],
        )
        await asyncio.to_thread(self.save, run)

        def persist(steps):
            run.steps = steps
            self.save(run)

        recorder = Recorder(persist)
        status, error, revision, session = "failed", None, None, None
        try:
            # The job timeout stays under the five-minute lease, so no renewal is needed.
            async with asyncio.timeout(self.settings.agent_job_timeout_seconds):
                session = await self.planner.start()
                status, revision = await self.converse(device_id, lease, session, run, recorder)
        except TimeoutError:
            error = "timeout"
        except PlanningError as exc:
            error = exc.kind
        except LlmCallsLimitExceededError:
            error = "llm_limit"
        except Exception:
            error = "error"
            logger.exception("planner_run_failed device_id=%s", device_id)
        finally:
            if session is not None:
                await session.close()
        if status == "failed":
            error = error or "invalid_proposal"
            await asyncio.to_thread(self.release, device_id, lease)
        run.status, run.error_kind, run.revision_created = status, error, revision
        run.steps, run.finished_at = recorder.steps, time.time()
        await asyncio.to_thread(self.save, run, event=status != "superseded")
        logger.info(
            "planner_run_finished device_id=%s status=%s error=%s llm_calls=%s",
            device_id,
            status,
            error,
            run.llm_calls,
        )
        return status

    async def converse(self, device_id, lease, session, run, recorder):
        brief = await asyncio.to_thread(planning_brief, self.context, device_id, lease)
        message = planning_message(brief)
        for attempt in range(MAX_REPAIRS + 1):
            run.attempts = attempt + 1
            reply = await session.send(message, recorder.record)
            run.llm_calls += reply.llm_calls
            run.input_tokens += reply.input_tokens
            run.output_tokens += reply.output_tokens
            try:
                proposal = parse_proposal(reply.text)
                body = rules_api.AgentProposal(
                    request_id=lease["request_id"],
                    lease_token=lease["lease_token"],
                    generation=lease["generation"],
                    base_revision=lease["base_revision"],
                    reason=proposal["reason"],
                    ruleset=proposal["ruleset"],
                )
                state = await asyncio.to_thread(
                    rules_api.complete_planning_job, self.context, device_id, body, ACTOR
                )
            except ValueError as exc:
                problem = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
                recorder.record(
                    {"at": time.time(), "kind": "rejected", "text": problem[:STEP_TEXT]}
                )
                message = repair_message(problem)
                continue
            except HTTPException as exc:
                if exc.status_code == 409:
                    return "superseded", None
                if exc.status_code != 422:
                    raise
                detail = str(exc.detail)
                recorder.record({"at": time.time(), "kind": "rejected", "text": detail[:STEP_TEXT]})
                message = repair_message(detail)
                continue
            if state["revision"] == lease["base_revision"]:
                return "unchanged", None
            return "completed", state["revision"]
        return "failed", None

    def reconcile(self):
        """Close runs whose process stopped; the job timeout bounds every live run."""
        current = time.time()
        with self.sessions() as db:
            db.execute(
                update(AgentRun)
                .where(
                    AgentRun.status == "running",
                    AgentRun.started_at < current - self.settings.agent_job_timeout_seconds - 60,
                )
                .values(status="failed", error_kind="interrupted", finished_at=current)
            )
            db.commit()

    def release(self, device_id, lease):
        try:
            rules_api.fail_planning_job(
                self.context,
                device_id,
                rules_api.JobLease(
                    request_id=lease["request_id"], lease_token=lease["lease_token"]
                ),
            )
        except HTTPException:
            pass  # The lease was superseded; the inbox already reflects the newer state.

    def save(self, run: AgentRun, event=False):
        with self.sessions() as db:
            run = db.merge(run)
            if event:
                state = rules_api.get_state(db, run.device_id)[1]
                details = {"planning": run.status, "run_id": run.id}
                if run.error_kind:
                    details["error"] = run.error_kind
                db.add(
                    AutomationEvent(
                        device_id=run.device_id, revision=state.revision, details=details
                    )
                )
            db.commit()


async def run_plan(settings=None, planner=None) -> dict:
    """One planning pass, for the on-demand GCP `planner` job."""
    from climate_control.config import AgentSettings
    from climate_control.db import database

    settings = settings or AgentSettings()
    engine, sessions = database(settings.database_url)
    try:
        planner = planner or create_planner(sessions, settings)
        if planner is None:
            return {"planning": "disabled"}
        return await PlannerWorker(sessions, settings, planner).tick()
    finally:
        engine.dispose()


class PlannerJobTrigger:
    """Starts the planner Cloud Run Job on demand, from the API process."""

    def __init__(self, job: str):
        self.job = job
        self.started_at = -math.inf

    def __call__(self):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            response = AuthorizedSession(credentials).post(
                f"https://run.googleapis.com/v2/{self.job}:run", json={}, timeout=5
            )
            response.raise_for_status()
            self.started_at = time.monotonic()
        except Exception:
            # The next scheduled tick retries while planning work remains claimable.
            logger.warning("planner_trigger_failed")
