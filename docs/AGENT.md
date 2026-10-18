# Agents

Agents work with one shared set of tools: device status and control, outdoor AQI, and rule configuration. There are two ways to use them:

- **In-app planner.** A background ADK agent maintains the rules of every device whose owner is *Agent-managed*. It has no chat interface and no settings beyond the model.
- **MCP.** External agents such as Claude Code, Cursor, or another ADK agent call the same tools at `/mcp` using scoped access tokens.

The [automation contract](AUTOMATION.md) still governs every rule change: strict schema validation, capability checks, and generation/revision fencing apply to agents exactly as they apply to the UI.

## In-app planner

When a planning request is pending (see [review triggers](AUTOMATION.md#rule-format-version-1)), the worker does the following:

1. Claims the request.
2. Starts an agent session with a planning brief: the target's capabilities and allowed actions, the active ruleset, planning reasons, current inputs, other devices, outdoor data, recent events, and seven-day hour-of-day profiles of readings and settings (about 2,000 tokens). The rule-engine skill is part of the system instruction.
3. Lets the agent answer directly, or call read-only tools for facts the brief lacks, such as another device's history.
4. Validates the agent's final `{"reason", "ruleset"}` JSON and submits it through the planning protocol.

The active ruleset is the household's current intent. The planner never sees earlier revisions, so it cannot restore them. If you delete the rules and hand the device over, it designs a new ruleset from the device's readings and capabilities, the outdoor station, and established references (WHO guidelines, US AQI breakpoints), citing the source in each rule's explanation.

The lease token never reaches the model.

**How results are handled:**
- **Rejected output:** if the agent's output is not valid JSON, or the ruleset is invalid, the error goes back to the agent. It gets up to two repairs.
- **Superseded request:** if a manual control or ownership change supersedes the request, the proposal is discarded.
- **Repeated failure or timeout:** the request returns to the inbox with the existing backoff. The current rules keep running.
- **Unchanged rules:** a proposal identical to the current rules confirms them without creating a new revision.

**What you see in the rule editor:** while the agent works on a device that has no rules yet, the rules area says it is preparing them. A side panel shows the run's steps as they happen, and stays available afterwards under *How the agent worked*, collapsed until you open it.

**Limits:**
- Each job is bounded by `AGENT_JOB_TIMEOUT_SECONDS` (default 240, which is less than the 5-minute lease) and `AGENT_MAX_LLM_CALLS` (default 20).
- A planning pass stops claiming new requests after `AGENT_TICK_BUDGET_SECONDS`.

**What gets recorded:**
- Each run records its status, model, LLM calls, token counts, and error kind in `agent_runs`, together with an ordered list of steps: the model's reasoning when the provider returns it, the tools it called, and any rules the app rejected. The brief, the full prompt, and the proposed ruleset are not stored as steps.
- Activity shows *Agent updated rules*, *Agent kept rules*, or *Agent planning failed*.
- Revisions name their author: `user`, `agent:planner`, or `token:<name>`.

### Model providers

| `AGENT_PROVIDER` | Model | Credentials |
| --- | --- | --- |
| `auto` (default) | Gemini on GCP when `GOOGLE_CLOUD_PROJECT` is set; otherwise OpenRouter when a key is set; otherwise disabled | — |
| `gemini` | `AGENT_MODEL`, default `gemini-flash-latest`, through Gemini Enterprise Agent Platform | Application Default Credentials with `roles/aiplatform.user`; `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` (default `global`) |
| `openrouter` | `AGENT_MODEL`, default `~google/gemini-flash-latest`, through ADK's LiteLLM adapter | `OPENROUTER_API_KEY` |
| `disabled` | None. Agent-managed devices wait for an external planner. | — |

The defaults are moving aliases. Set `AGENT_MODEL` to pin a specific model.

### Where it runs

Switching a device to agent ownership, or clicking **Request review**, starts planning immediately: locally it wakes the loop, and on GCP the API starts the `planner` Job. The rule page checks every 3 seconds while a review is pending and shows the agent's rules as soon as they are saved. Other reviews start the `planner` Job from the next minute tick that finds claimable work.


| Mode | Planner process |
| --- | --- |
| Local `serve` | An in-process loop beside the poller, checking every 30 seconds. It never delays rule execution. |
| GCP (`AGENT_RUNTIME=remote`, default) | The `planner` Cloud Run Job, started on demand by the API, queries the planner deployed on Agent Runtime (`AGENT_RUNTIME_RESOURCE`). That agent reaches the app's tools through `/mcp` with a read-scoped token (`AGENT_SERVICE_TOKEN`). |
| Embedded (`AGENT_RUNTIME=embedded`) | When no `AGENT_RUNTIME_RESOURCE` is configured, the `planner` Job runs the planner in-process with `DATABASE_URL` and model access, never the encryption key or provider credentials. |

## MCP

Create a token under **Settings → Agent access**. Choose its scopes, and copy the token, which is shown only once. Revoking a token takes effect immediately. The database stores only token hashes.

| Scope | Tools |
| --- | --- |
| `read` | `list_devices`, `get_device`, `get_device_history`, `get_outdoor_air_quality`, `get_outdoor_history`, `get_rule_schema`, `get_device_automation`, `list_rule_events`, `list_rule_revisions`, `preview_rules`, `get_display_settings` |
| `control` | `set_device_power`, `set_device_fan_level`, `set_device_mode`, `set_device_display`, `set_display_warning_threshold`, `refresh_device_status` |
| `rules` | `save_device_rules`, `configure_device_automation`, `pause_device_automation`, `resume_device_automation`, `request_rule_review`, `list_planning_jobs`, `claim_planning_job`, `renew_planning_job`, `submit_planning_result`, `fail_planning_job` |

**How the tools behave:**
- **Control tools** take the same path as the UI controls, so they pause the device's rules and respect provider pacing.
- **Saving rules directly:** `save_device_rules` works only on manually managed devices.
- **Agent-managed devices** accept rules only through the planning job tools.

**Skill resources:** the server exposes the rule-engine skill as `skill://rule-engine/SKILL.md` plus its `references/*` resources. The `configure-device-rules` prompt bundles all of them.

**Transport:** stateless streamable HTTP with JSON responses, so every call fits inside a single bounded request.

**Authentication:** requests must carry `Authorization: Bearer cc_…`. Cookies are ignored, and the endpoint needs no CSRF token.

```sh
claude mcp add --transport http climate https://YOUR_APP/mcp --header "Authorization: Bearer cc_..."
```

From ADK:

```python
McpToolset(connection_params=StreamableHTTPConnectionParams(
    url="https://YOUR_APP/mcp", headers={"Authorization: Bearer cc_..."}))
```

## Deploying to Agent Runtime

GCP releases ([`cloudbuild.yaml`](../cloudbuild.yaml) and `invoke deploy`) deploy the planner to Gemini Enterprise Agent Platform Agent Runtime automatically:

1. On the first release, if `AGENT_SERVICE_TOKEN` has no enabled Secret Manager version, the release generates a `cc_…` token, registers its SHA-256 digest in `access_tokens` with the `read` scope during `migrate`, and stores the token in Secret Manager.
2. For OpenRouter (`_AGENT_PROVIDER=openrouter` or `--provider openrouter`), populate `OPENROUTER_API_KEY` in Secret Manager before deploying.
3. After the API and Firebase Hosting are ready, the release stages `deploy/agent_runtime/climate_planner` with this package and pinned requirements, deploys or updates the Reasoning Engine with `adk deploy agent_engine --otel_to_cloud` as the `agent` service account, records the resource name in `infra/gcp/runtime/agent.auto.tfvars.json`, and wires `agent_runtime_resource` into the `planner` Job and `api` service.

You can also update the Agent Runtime deployment standalone after an initial release with `invoke deploy-agent` (adding `--provider openrouter` and/or `--model ID` if needed).

The deployed agent reads its token and model key from Secret Manager at runtime. Its environment holds only secret resource names. To disable the built-in planner so agent-managed devices wait for an external MCP planner, set `_AGENT_PROVIDER=disabled` on the Cloud Build trigger (or `{"agent_provider": "disabled"}` in `infra/gcp/runtime/agent.auto.tfvars.json`).

## Telemetry

On GCP (`TELEMETRY=auto` detects Cloud Run), the API and planner export data as follows:
- **Traces:** OpenTelemetry traces go to Cloud Trace through the Telemetry API. They include ADK agent, model, and tool spans plus MCP tool-call spans with tool name, scope, and outcome.
- **Metrics:** ADK metrics go to Cloud Monitoring.
- **Logs:** written as JSON with trace correlation, which Cloud Run forwards to Cloud Logging.
- **Content capture:** prompt and response content capture is off by default (`ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false`, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`).

Agent Runtime deployments enable the platform's own telemetry with the same content setting.

Elsewhere, logs stay plain text. Standard `OTEL_EXPORTER_OTLP_*` variables still enable export.
