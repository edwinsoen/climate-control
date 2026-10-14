"""Planner assembly for Agent Runtime, where tools come from the app's MCP endpoint.

Only resource names live in the deployment's environment. Credentials are read from
Secret Manager with the runtime's service account and cached briefly, so rotating a
secret version takes effect without redeploying.
"""

import asyncio
import base64
import os
import threading
import time

from climate_control.agent.planner import (
    DEFAULT_MODELS,
    PLANNER_TOOLS,
    build_model,
    build_planner,
)

SECRET_TTL_SECONDS = 600
_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def secret_value(secret: str) -> str:
    """Read the latest version of `projects/P/secrets/S`, cached for ten minutes."""
    with _lock:
        cached = _cache.get(secret)
        if cached and time.monotonic() - cached[0] < SECRET_TTL_SECONDS:
            return cached[1]
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    response = AuthorizedSession(credentials).get(
        f"https://secretmanager.googleapis.com/v1/{secret}/versions/latest:access", timeout=10
    )
    response.raise_for_status()
    value = base64.b64decode(response.json()["payload"]["data"]).decode().strip()
    with _lock:
        _cache[secret] = (time.monotonic(), value)
    return value


def mcp_toolset(url: str, token_secret: str, transport=None):
    import httpx2
    from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
    from mcp.shared._httpx_utils import create_mcp_http_client

    class SecretBearer(httpx2.Auth):
        # Applied to every request, including tool discovery outside an agent run.
        async def async_auth_flow(self, request):
            token = await asyncio.to_thread(secret_value, token_secret)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request

    def client(headers=None, timeout=None, auth=None):
        if transport is None:
            return create_mcp_http_client(headers, timeout, SecretBearer())
        return httpx2.AsyncClient(
            transport=transport, headers=headers, timeout=timeout, auth=SecretBearer()
        )

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=url, timeout=30, httpx_client_factory=client
        ),
        tool_filter=list(PLANNER_TOOLS),
    )


def planner_from_environment():
    """Build the deployed planner from CLIMATE_CONTROL_* and AGENT_* variables."""
    provider = os.environ.get("AGENT_PROVIDER", "gemini")
    api_key = ""
    if provider == "openrouter":
        api_key = secret_value(os.environ["OPENROUTER_API_KEY_SECRET"])
    model = build_model(
        provider,
        os.environ.get("AGENT_MODEL") or DEFAULT_MODELS[provider],
        api_key=api_key,
        project=os.environ.get("AGENT_MODEL_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        location=os.environ.get("AGENT_MODEL_LOCATION", "global"),
    )
    toolset = mcp_toolset(
        os.environ["CLIMATE_CONTROL_MCP_URL"], os.environ["CLIMATE_CONTROL_TOKEN_SECRET"]
    )
    return build_planner(model, [toolset])
