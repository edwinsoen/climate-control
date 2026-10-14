import asyncio
import inspect
import json
import time

import pytest
from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field
from sqlalchemy import select
from test_automation import rule_document, setup_rules

from climate_control.agent.planner import PLANNER_TOOLS, parse_proposal
from climate_control.agent.tools import TOOLS
from climate_control.agent.worker import (
    EmbeddedPlanner,
    PlannerJobTrigger,
    PlannerWorker,
    RemoteSession,
    run_plan,
)
from climate_control.automation_api import pause_for_manual
from climate_control.config import AgentSettings
from climate_control.db import AgentRun, PlanningJob, RuleRevision


class ScriptedLlm(BaseLlm):
    """Replays model turns; a callable step receives the request to inspect or act on."""

    model: str = "scripted"
    script: list = Field(default_factory=list)
    requests: list = Field(default_factory=list)

    @property
    def capabilities(self):
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        step = self.script.pop(0)
        part = step(llm_request) if callable(step) else step
        if inspect.isawaitable(part):
            part = await part
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=100, candidates_token_count=10
            ),
        )


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def answer(ruleset, reason="Raise cleaning for evidence."):
    return types.Part(text=json.dumps({"reason": reason, "ruleset": ruleset}))


def request_text(request):
    return json.dumps(
        [content.model_dump(mode="json") for content in request.contents], default=str
    )


async def agent_device(signed_client, device):
    path = await setup_rules(signed_client, device)
    result = await signed_client.put(
        path, json={"base_revision": 1, "generation": 2, "owner": "agent", "enabled": True}
    )
    assert result.status_code == 200
    return path


def worker(app, llm):
    settings = app.state.settings
    planner = EmbeddedPlanner(app.state.sessions, settings, model=llm)
    return PlannerWorker(app.state.sessions, settings, planner)


def proposal(device_id):
    document = rule_document(device_id)
    document["rules"][0]["action"]["fan"] = 4
    return document


def runs(app):
    with app.state.sessions() as db:
        return list(db.scalars(select(AgentRun)))


async def test_planner_answers_from_brief_in_one_call(app, signed_client, controlled_device):
    device_id = controlled_device["id"]
    path = await agent_device(signed_client, controlled_device)
    llm = ScriptedLlm(script=[answer(proposal(device_id))])
    counts = await worker(app, llm).tick()
    assert counts == {"planning_checked": 1, "planning_completed": 1, "planning_failed": 0}
    state = (await signed_client.get(path)).json()
    assert state["revision"] == 2
    assert state["ruleset"]["rules"][0]["action"]["fan"] == 4
    assert state["job"]["status"] == "completed"
    with app.state.sessions() as db:
        assert db.scalar(select(RuleRevision).where(RuleRevision.revision == 2)).author == (
            "agent:planner"
        )
    request = llm.requests[0]
    assert "First matching rule wins" in request.config.system_instruction  # Inlined skill.
    message = request.contents[0].parts[0].text
    assert '"allowed_actions":{"power":true,"fan":[1,2,3,4,"auto","sleep"]' in message
    assert '"pm25":{' in message  # Hour-of-day profile of the target's readings.
    assert "lease_token" not in message
    run = runs(app)[0]
    assert (run.status, run.revision_created, run.llm_calls) == ("completed", 2, 1)
    assert (run.input_tokens, run.output_tokens) == (100, 10)
    events = (await signed_client.get(path + "/events")).json()
    assert events[0]["planning"] == "completed"


async def test_planner_can_cross_check_with_tools(app, signed_client, controlled_device):
    device_id = controlled_device["id"]
    await agent_device(signed_client, controlled_device)
    llm = ScriptedLlm(
        script=[
            call("get_device_history", device_id=device_id, hours=168),
            answer(proposal(device_id)),
        ]
    )
    await worker(app, llm).tick()
    response = llm.requests[1].contents[-1].parts[0].function_response
    assert response.name == "get_device_history" and "error" not in response.response
    assert runs(app)[0].status == "completed"


async def test_planner_runs_with_scheduled_job_settings(app, signed_client, controlled_device):
    # The GCP plan job has database access only: no encryption key or provider tokens.
    device_id = controlled_device["id"]
    await agent_device(signed_client, controlled_device)
    settings = AgentSettings(_env_file=None, database_url=app.state.settings.database_url)
    planner = EmbeddedPlanner(
        app.state.sessions,
        settings,
        model=ScriptedLlm(script=[call("get_outdoor_air_quality"), answer(proposal(device_id))]),
    )
    await PlannerWorker(app.state.sessions, settings, planner).tick()
    assert runs(app)[0].status == "completed"


async def test_planner_repairs_rejected_proposal(app, signed_client, controlled_device):
    device_id = controlled_device["id"]
    path = await agent_device(signed_client, controlled_device)
    invalid = proposal(device_id)
    invalid["fallback"]["fan"] = 999
    llm = ScriptedLlm(
        script=[answer(invalid), types.Part(text="no json"), answer(proposal(device_id))]
    )
    await worker(app, llm).tick()
    assert "fan level" in request_text(llm.requests[1]).lower()
    assert "reason" in request_text(llm.requests[2])
    assert (await signed_client.get(path)).json()["revision"] == 2
    assert runs(app)[0].attempts == 3


async def test_planner_failure_backs_off_and_keeps_rules(app, signed_client, controlled_device):
    path = await agent_device(signed_client, controlled_device)
    llm = ScriptedLlm(script=[types.Part(text="I cannot decide.")] * 3)
    counts = await worker(app, llm).tick()
    assert counts["planning_failed"] == 1
    state = (await signed_client.get(path)).json()
    assert state["revision"] == 1
    assert state["job"]["status"] == "pending"
    assert state["job"]["available_at"] > time.time() + 60
    run = runs(app)[0]
    assert (run.status, run.error_kind) == ("failed", "invalid_proposal")
    assert (await signed_client.get(path + "/events")).json()[0]["planning"] == "failed"
    assert await worker(app, ScriptedLlm()).tick() == {
        "planning_checked": 0,
        "planning_completed": 0,
        "planning_failed": 0,
    }


async def test_manual_control_during_planning_discards_proposal(
    app, signed_client, controlled_device
):
    device_id = controlled_device["id"]
    path = await agent_device(signed_client, controlled_device)

    def manual_change(_):
        with app.state.sessions() as db:
            pause_for_manual(db, device_id)
            db.commit()
        return answer(proposal(device_id))

    await worker(app, ScriptedLlm(script=[manual_change])).tick()
    state = (await signed_client.get(path)).json()
    assert state["revision"] == 1
    assert state["job"]["status"] == "pending"
    assert runs(app)[0].status == "superseded"
    assert "planning" not in (await signed_client.get(path + "/events")).json()[0]


async def test_unchanged_proposal_confirms_without_new_revision(
    app, signed_client, controlled_device
):
    device_id = controlled_device["id"]
    path = await agent_device(signed_client, controlled_device)
    with app.state.sessions() as db:
        before = db.get(PlanningJob, device_id)
        assert before.status == "pending"
    await worker(app, ScriptedLlm(script=[answer(rule_document(device_id), "Still fits.")])).tick()
    state = (await signed_client.get(path)).json()
    assert (state["revision"], state["job"]["status"]) == (1, "completed")
    assert runs(app)[0].status == "unchanged"


async def test_llm_call_limit_fails_job(app, signed_client, controlled_device):
    device_id = controlled_device["id"]
    await agent_device(signed_client, controlled_device)
    app.state.settings.agent_max_llm_calls = 2
    llm = ScriptedLlm(script=[call("get_device_automation", device_id=device_id)] * 3)
    await worker(app, llm).tick()
    assert (runs(app)[0].status, runs(app)[0].error_kind) == ("failed", "llm_limit")


def test_parse_proposal_takes_last_result_object():
    text = 'Draft {"reason": "a", "ruleset": {"x": 1}}\n```json\n{"reason": "b", "ruleset": {}}```'
    assert parse_proposal(text)["reason"] == "b"
    with pytest.raises(ValueError):
        parse_proposal('{"ruleset": {}}')


@pytest.mark.parametrize(
    "env,provider",
    [
        ({}, None),
        ({"OPENROUTER_API_KEY": "key"}, "openrouter"),
        ({"OPENROUTER_API_KEY": "key", "K_SERVICE": "api", "GOOGLE_CLOUD_PROJECT": "p"}, "gemini"),
        ({"K_SERVICE": "api"}, None),
        ({"AGENT_PROVIDER": "disabled", "OPENROUTER_API_KEY": "key"}, None),
        ({"AGENT_PROVIDER": "gemini"}, None),
        (
            {
                "AGENT_RUNTIME": "remote",
                "AGENT_RUNTIME_RESOURCE": "projects/p/locations/l/reasoningEngines/1",
            },
            "remote",
        ),
    ],
)
def test_provider_selection(monkeypatch, env, provider):
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "GOOGLE_CLOUD_PROJECT", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert AgentSettings(_env_file=None).model_provider == provider


async def test_scheduled_pass_is_disabled_without_a_model(app):
    settings = AgentSettings(_env_file=None, database_url=app.state.settings.database_url)
    settings.agent_provider = "disabled"
    assert await run_plan(settings) == {"planning": "disabled"}


async def test_remote_session_reads_final_text_and_usage():
    usage = {"prompt_token_count": 50, "candidates_token_count": 5}

    class Runtime:
        async def async_stream_query(self, **kwargs):
            assert kwargs["session_id"] == "session" and "lease_token" not in kwargs["message"]
            yield {
                "content": {"parts": [{"function_call": {"name": "list_devices"}}]},
                **{"usage_metadata": usage},
            }
            yield {"content": {"parts": [{"function_response": {"name": "list_devices"}}]}}
            yield {
                "content": {"parts": [{"text": '{"reason": "r", "ruleset": {}}'}]},
                **{"usage_metadata": usage},
            }

    reply = await RemoteSession(Runtime(), "session").send("plan")
    assert parse_proposal(reply.text)["reason"] == "r"
    assert (reply.llm_calls, reply.input_tokens, reply.output_tokens) == (2, 100, 10)


def test_planner_reads_everything_except_revision_history():
    reads = {name for name, spec in TOOLS.items() if spec.scope == "read"}
    assert set(PLANNER_TOOLS) == reads - {"list_rule_revisions"}


async def test_deleted_rules_are_planned_fresh_without_history(
    app, signed_client, controlled_device
):
    device_id = controlled_device["id"]
    path = await agent_device(signed_client, controlled_device)
    # The household takes manual control, deletes every rule, then hands it back.
    manual = {"base_revision": 1, "generation": 3, "owner": "manual", "enabled": True}
    assert (await signed_client.put(path, json=manual)).status_code == 200
    empty = {**rule_document(device_id), "rules": [], "fallback": {"power": "hold"}}
    saved = await signed_client.put(
        path + "/ruleset",
        json={"base_revision": 1, "generation": 4, "reason": "Cleared", "ruleset": empty},
    )
    assert saved.status_code == 200
    await agent_device_from(signed_client, path, generation=5, revision=2)
    llm = ScriptedLlm(script=[answer(proposal(device_id))])
    await worker(app, llm).tick()
    first = request_text(llm.requests[0])
    assert "no active rules" in first
    assert "list_rule_revisions" not in llm.requests[0].tools_dict
    assert (await signed_client.get(path)).json()["revision"] == 3


async def test_agent_ownership_and_review_requests_start_planning_now(
    app, signed_client, controlled_device
):
    calls = []
    app.state.planning_trigger = lambda: calls.append(1)
    path = await setup_rules(signed_client, controlled_device)
    assert calls == []  # Manual saves and enabling never plan.
    await agent_device_from(signed_client, path, generation=2)
    assert calls == [1]
    assert (await signed_client.post(path + "/reconsider")).status_code == 200
    assert calls == [1, 1]


async def agent_device_from(client, path, generation, revision=1):
    result = await client.put(
        path,
        json={
            "base_revision": revision,
            "generation": generation,
            "owner": "agent",
            "enabled": True,
        },
    )
    assert result.status_code == 200


async def test_notify_wakes_the_planner_loop(app):
    class Counting(PlannerWorker):
        ticks = 0

        async def tick(self):
            self.ticks += 1
            return {}

    worker = Counting(app.state.sessions, app.state.settings, planner=None)
    task = asyncio.create_task(worker.run(interval=3600))
    try:
        while worker.ticks < 1:
            await asyncio.sleep(0.01)
        worker.notify()
        async with asyncio.timeout(2):
            while worker.ticks < 2:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()


def test_job_trigger_starts_planner_job_and_tolerates_failure(monkeypatch):
    import google.auth
    from google.auth.transport import requests as transport

    posts = []

    class Session:
        def __init__(self, credentials):
            pass

        def post(self, url, **kwargs):
            posts.append(url)
            raise ConnectionError("unreachable")

    monkeypatch.setattr(google.auth, "default", lambda scopes: (object(), "p"))
    monkeypatch.setattr(transport, "AuthorizedSession", Session)
    PlannerJobTrigger("projects/p/locations/r/jobs/climate-planner")()
    assert posts == [
        "https://run.googleapis.com/v2/projects/p/locations/r/jobs/climate-planner:run"
    ]


async def test_run_steps_show_what_the_agent_did(app, signed_client, controlled_device):
    device_id = controlled_device["id"]
    path = await agent_device(signed_client, controlled_device)
    invalid = proposal(device_id)
    invalid["fallback"]["fan"] = 999
    llm = ScriptedLlm(
        script=[
            call("get_device_history", device_id=device_id, hours=168),
            answer(invalid),
            answer(proposal(device_id)),
        ]
    )
    await worker(app, llm).tick()

    state = (await signed_client.get(path)).json()
    assert state["run"]["status"] == "completed"
    assert state["run"]["step_count"] >= 3
    assert "steps" not in state["run"]  # Polling stays small; steps load on demand.

    runs = (await signed_client.get(path + "/runs")).json()
    steps = runs[0]["steps"]
    assert [step["kind"] for step in steps[:2]] == ["tool", "result"]
    assert steps[0]["name"] == "get_device_history" and '"hours":168' in steps[0]["arguments"]
    rejected = next(step for step in steps if step["kind"] == "rejected")
    assert "fan" in rejected["text"].lower()
    assert all("ruleset" not in str(step.get("arguments", "")) for step in steps)


async def test_steps_are_saved_while_the_run_is_still_working(
    app, signed_client, controlled_device
):
    device_id = controlled_device["id"]
    path = await agent_device(signed_client, controlled_device)
    seen = []

    async def inspect(_request):
        # Runs between the tool call and the answer, as the rule page would poll.
        seen.append((await signed_client.get(path)).json()["run"])
        return answer(proposal(device_id))

    llm = ScriptedLlm(script=[call("get_device_history", device_id=device_id), inspect])
    planner = EmbeddedPlanner(app.state.sessions, app.state.settings, model=llm)
    await PlannerWorker(app.state.sessions, app.state.settings, planner).tick()
    assert seen[0]["status"] == "running" and seen[0]["step_count"] >= 1


async def test_abandoned_runs_are_closed_on_the_next_pass(app, signed_client, controlled_device):
    device_id = controlled_device["id"]
    path = await agent_device(signed_client, controlled_device)
    settings = app.state.settings
    with app.state.sessions() as db:
        # A run whose process stopped before it could finish, as a restart would leave it.
        db.add(
            AgentRun(
                device_id=device_id,
                request_id="old",
                mode="embedded",
                model="test",
                status="running",
                started_at=time.time() - settings.agent_job_timeout_seconds - 120,
            )
        )
        db.commit()
    assert (await signed_client.get(path)).json()["run"]["status"] == "running"

    await worker(app, ScriptedLlm(script=[answer(proposal(device_id))])).tick()

    with app.state.sessions() as db:
        stale = db.scalar(select(AgentRun).where(AgentRun.request_id == "old"))
    assert (stale.status, stale.error_kind) == ("failed", "interrupted")
    assert stale.finished_at
    assert (await signed_client.get(path)).json()["run"]["status"] == "completed"
