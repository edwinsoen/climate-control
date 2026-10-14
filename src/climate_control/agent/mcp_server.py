"""MCP endpoint exposing the shared agent tools and the rule-engine skill.

Stateless JSON responses keep each call within a single bounded HTTP request, which fits
the Firebase Hosting proxy. Requests authenticate with scoped bearer tokens, not cookies,
so browser CSRF protections do not apply and cookies are ignored.
"""

import asyncio
import inspect
import logging
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from mcp.server.mcpserver.resources import TextResource
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from opentelemetry import trace
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from climate_control import __version__
from climate_control.agent import tools
from climate_control.agent.access import verify_access_token

SKILL_DIR = Path(__file__).parent / "skills" / "rule-engine"
ACCESS_SCOPE_KEY = "climate_control.access"
tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)

INSTRUCTIONS = """Climate Control monitors and controls household air purifiers.
Readings are cached; control tools pause a device's rules like a manual change.
Before writing rules, read the skill resource skill://rule-engine/SKILL.md (or use the
configure-device-rules prompt), inspect get_device_automation, and validate with
preview_rules."""


def skill_files() -> dict[str, str]:
    files = {"SKILL.md": (SKILL_DIR / "SKILL.md").read_text()}
    for path in sorted((SKILL_DIR / "references").glob("*.md")):
        files[f"references/{path.name}"] = path.read_text()
    return files


def mcp_tool(spec: tools.ToolSpec, state):
    """Wrap a shared tool with the caller's token scopes and MCP error reporting."""

    async def call(**arguments):
        context = arguments.pop("context")
        access = context.request_context.request.scope[ACCESS_SCOPE_KEY]
        ctx = tools.ToolContext(
            sessions=state.sessions,
            settings=state.settings,
            poller=state.poller,
            planning_trigger=getattr(state, "planning_trigger", None),
            actor=f"token:{access['name']}",
            scopes=frozenset(access["scopes"]),
        )
        with tracer.start_as_current_span(f"mcp.tool {spec.name}") as span:
            span.set_attribute("mcp.tool.name", spec.name)
            span.set_attribute("mcp.tool.scope", spec.scope)
            try:
                result = await tools.invoke(spec, ctx, arguments)
            except tools.ToolError as exc:
                span.set_attribute("mcp.tool.outcome", exc.kind)
                raise McpToolError(tools.text(exc.payload())) from None
            span.set_attribute("mcp.tool.outcome", "ok")
        return tools.text(result)

    context = inspect.Parameter("context", inspect.Parameter.KEYWORD_ONLY, annotation=Context)
    call.__name__ = spec.name
    call.__doc__ = spec.fn.__doc__
    call.__signature__ = inspect.Signature([*spec.parameters, context], return_annotation=str)
    call.__annotations__ = {
        **{p.name: p.annotation for p in spec.parameters},
        "context": Context,
        "return": str,
    }
    return call


def create_mcp_server(state) -> MCPServer:
    server = MCPServer(
        name="climate-control",
        title="Climate Control",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level="WARNING",
    )
    for spec in tools.TOOLS.values():
        server.add_tool(
            mcp_tool(spec, state),
            name=spec.name,
            description=inspect.cleandoc(spec.fn.__doc__ or ""),
            annotations=ToolAnnotations(
                read_only_hint=not spec.writes,
                destructive_hint=spec.writes,
                idempotent_hint=not spec.writes,
                open_world_hint=spec.scope == "control",
            ),
            structured_output=False,
        )
    for name, content in skill_files().items():
        server.add_resource(
            TextResource(
                uri=f"skill://rule-engine/{name}",
                name=f"rule-engine/{name}",
                description="How to configure Climate Control device rules.",
                mime_type="text/markdown",
                text=content,
            )
        )

    @server.prompt(
        name="configure-device-rules",
        description="Review and configure the rules for one device using the rule-engine skill.",
    )
    def configure_device_rules(device_id: str) -> str:
        files = skill_files()
        references = "\n\n".join(
            files[name] for name in sorted(files) if name.startswith("references/")
        )
        return (
            f"{files['SKILL.md']}\n\n{references}\n\n"
            f"Review the rules for device {device_id}. Start with get_device_automation."
        )

    return server


class BearerAuth:
    """ASGI wrapper that admits only active scoped access tokens."""

    def __init__(self, app, sessions):
        self.app, self.sessions = app, sessions

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        authorization = Headers(scope=scope).get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        access = None
        if scheme.lower() == "bearer" and token.strip():
            access = await asyncio.to_thread(verify_access_token, self.sessions, token.strip())
        if access is None:
            response = JSONResponse(
                {"detail": "A valid access token is required."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
            return await response(scope, receive, send)
        scope[ACCESS_SCOPE_KEY] = access
        await self.app(scope, receive, send)


def mcp_app(server: MCPServer, sessions):
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Host checks guard ambient (cookie or local) credentials against DNS rebinding.
        # This endpoint has none: every request needs an explicit bearer token, and proxies
        # such as Firebase Hosting rewrite the Host header.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerAuth(app, sessions)
