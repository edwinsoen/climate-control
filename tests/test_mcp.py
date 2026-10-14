import asyncio
import json
import time

import pytest
from sqlalchemy import select
from test_automation import rule_document, setup_rules

from climate_control.db import AccessToken, RuleRevision

PROTOCOL = "2025-06-18"


@pytest.fixture
async def mcp_client(app, signed_client):
    # The MCP session manager runs in the app lifespan; keep it in one task so its
    # cancel scope is entered and exited by the same task.
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await started.wait()
    yield signed_client
    stop.set()
    await task


async def issue(client, *scopes, name="assistant"):
    response = await client.post("/api/access-tokens", json={"name": name, "scopes": list(scopes)})
    assert response.status_code == 201, response.text
    return response.json()["token"]


async def rpc(client, token, method, params=None, *, cookies=False):
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    saved = dict(client.cookies)
    if not cookies:
        client.cookies.clear()
    try:
        return await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            headers=headers,
        )
    finally:
        client.cookies.update(saved)


async def call(client, token, name, **arguments):
    response = await rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    text = result["content"][0]["text"]
    if result["isError"]:
        return {"tool_error": json.loads(text.split(": ", 1)[1])}
    return json.loads(text)


async def test_token_management_requires_session_and_stores_hash(app, mcp_client):
    assert (
        await mcp_client.post(
            "/api/access-tokens",
            json={"name": "x", "scopes": ["read"]},
            headers={"X-CSRF-Token": "wrong"},
        )
    ).status_code == 403
    invalid = await mcp_client.post("/api/access-tokens", json={"name": "x", "scopes": ["admin"]})
    assert invalid.status_code == 422
    token = await issue(mcp_client, "read")
    listed = (await mcp_client.get("/api/access-tokens")).json()
    assert listed[0]["scopes"] == ["read"] and token not in json.dumps(listed)
    with app.state.sessions() as db:
        assert token not in db.scalar(select(AccessToken)).token_hash
    assert (await rpc(mcp_client, token, "tools/list")).status_code == 200
    assert (await mcp_client.delete(f"/api/access-tokens/{listed[0]['id']}")).status_code == 200
    assert (await rpc(mcp_client, token, "tools/list")).status_code == 401


async def test_mcp_rejects_missing_expired_and_cookie_only_access(app, mcp_client):
    assert (await rpc(mcp_client, None, "tools/list")).status_code == 401
    assert (await rpc(mcp_client, None, "tools/list", cookies=True)).status_code == 401
    assert (await rpc(mcp_client, "cc_unknown", "tools/list")).status_code == 401
    token = await issue(mcp_client, "read")
    with app.state.sessions() as db:
        db.scalar(select(AccessToken)).expires_at = time.time() - 1
        db.commit()
    assert (await rpc(mcp_client, token, "tools/list")).status_code == 401


async def test_read_scope_tools_and_skill(mcp_client, controlled_device):
    token = await issue(mcp_client, "read")
    tools = (await rpc(mcp_client, token, "tools/list")).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["list_devices"]["annotations"]["readOnlyHint"] is True
    assert by_name["set_device_power"]["annotations"]["destructiveHint"] is True
    devices = await call(mcp_client, token, "list_devices")
    assert devices[0]["id"] == controlled_device["id"]
    context = await call(
        mcp_client, token, "get_device_automation", device_id=controlled_device["id"]
    )
    assert context["allowed_actions"] == {
        "power": True,
        "fan": [1, 2, 3, 4, "auto", "sleep"],
        "metrics": ["pm25", "air_quality"],
    }
    assert "provider-secret-token" not in json.dumps([devices, context])
    preview = await call(
        mcp_client,
        token,
        "preview_rules",
        device_id=controlled_device["id"],
        ruleset={**rule_document(controlled_device["id"]), "fallback": {"power": "on"}},
    )
    assert preview["tool_error"]["error"] == "invalid"
    assert "fallback" in preview["tool_error"]["message"]
    denied = await call(mcp_client, token, "set_device_power", device_id="x", on=True)
    assert denied["tool_error"]["error"] == "forbidden"
    resources = (await rpc(mcp_client, token, "resources/list")).json()["result"]["resources"]
    assert "skill://rule-engine/SKILL.md" in {r["uri"] for r in resources}
    prompt = await rpc(
        mcp_client,
        token,
        "prompts/get",
        {"name": "configure-device-rules", "arguments": {"device_id": "abc"}},
    )
    assert "First matching rule wins" in json.dumps(prompt.json())


async def test_control_tool_pauses_rules_with_token_actor(app, mcp_client, controlled_device):
    path = await setup_rules(mcp_client, controlled_device)
    token = await issue(mcp_client, "control", name="home-assistant")
    result = await call(
        mcp_client, token, "set_device_fan_level", device_id=controlled_device["id"], level=1
    )
    assert result["state"]["fan_level"] == 1
    assert app.state.poller.provider.control_calls[-1][-1].fan_level == 1
    state = (await mcp_client.get(path)).json()
    assert state["paused_until"] > time.time()
    events = (await mcp_client.get(path + "/events")).json()
    assert events[0]["actor"] == "token:home-assistant"


async def test_rule_tools_follow_ownership_and_planning_protocol(
    app, mcp_client, controlled_device
):
    device_id = controlled_device["id"]
    token = await issue(mcp_client, "read", "rules", name="planner")
    saved = await call(
        mcp_client,
        token,
        "save_device_rules",
        device_id=device_id,
        ruleset=rule_document(device_id),
        reason="Initial rules",
        generation=0,
        base_revision=0,
    )
    assert saved["revision"] == 1
    stale = await call(
        mcp_client,
        token,
        "save_device_rules",
        device_id=device_id,
        ruleset=rule_document(device_id),
        reason="Stale",
        generation=0,
        base_revision=0,
    )
    assert stale["tool_error"]["error"] == "conflict"
    state = await call(
        mcp_client,
        token,
        "configure_device_automation",
        device_id=device_id,
        owner="agent",
        enabled=True,
        generation=1,
        base_revision=1,
    )
    assert state["owner"] == "agent"
    assert [job["device_id"] for job in await call(mcp_client, token, "list_planning_jobs")] == [
        device_id
    ]
    lease = await call(mcp_client, token, "claim_planning_job", device_id=device_id)
    document = rule_document(device_id)
    document["rules"][0]["action"]["fan"] = 4
    submitted = await call(
        mcp_client,
        token,
        "submit_planning_result",
        device_id=device_id,
        request_id=lease["request_id"],
        lease_token=lease["lease_token"],
        generation=lease["generation"],
        base_revision=lease["base_revision"],
        reason="Faster cleaning",
        ruleset=document,
    )
    assert submitted["revision"] == 2
    with app.state.sessions() as db:
        authors = list(db.scalars(select(RuleRevision.author).order_by(RuleRevision.revision)))
    assert authors == ["token:planner", "token:planner"]


async def test_remote_planner_toolset_reads_through_mcp(app, mcp_client, monkeypatch):
    import httpx2

    from climate_control.agent import remote
    from climate_control.agent.planner import PLANNER_TOOLS

    token = await issue(mcp_client, "read", name="agent-runtime")
    secrets = []
    monkeypatch.setattr(remote, "secret_value", lambda name: secrets.append(name) or token)

    toolset = remote.mcp_toolset(
        "http://localhost:8080/mcp",
        "projects/p/secrets/token",
        transport=httpx2.ASGITransport(app=app),
    )
    try:
        tools = await toolset.get_tools()
        assert {tool.name for tool in tools} == set(PLANNER_TOOLS)
    finally:
        await toolset.close()
    assert secrets and set(secrets) == {"projects/p/secrets/token"}
