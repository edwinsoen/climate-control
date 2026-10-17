"""Deploy the rule planner to Agent Runtime and point the planning job at it.

Run `invoke deploy` first. Secret payloads are never downloaded: the deployed agent reads
its MCP token and model key from Secret Manager with its own service account.
"""

import argparse
import json
import subprocess
import sys

from scripts.deploy_gcp import (
    BOOTSTRAP,
    RUNTIME,
    VARIABLES,
    DeploymentError,
    agent_environment,
    apply,
    current_api,
    deploy_agent_runtime,
    discover_versions,
    init_runtime,
    output,
    runtime_state,
)

AGENT_VARIABLES = RUNTIME / "agent.auto.tfvars.json"


def deploy_agent(*, provider="gemini", model="", dry_run=False):
    if not VARIABLES.exists():
        raise DeploymentError("Run invoke deploy before deploying the agent.")
    platform = output(BOOTSTRAP, "platform")
    init_runtime(platform.get("state_bucket", ""), dry_run=dry_run)
    app_url = output(RUNTIME, "app_url")
    versions = discover_versions(platform, dry_run=dry_run)
    required = {"AGENT_SERVICE_TOKEN"} | (
        {"OPENROUTER_API_KEY"} if provider == "openrouter" else set()
    )
    missing = required - versions.keys()
    if missing:
        raise DeploymentError(
            "Populate enabled secret versions for: " + ", ".join(sorted(missing)) + ". "
            "Create a read-scoped access token on the Accounts page for AGENT_SERVICE_TOKEN."
        )
    variables = json.loads(VARIABLES.read_text())
    saved_agent = json.loads(AGENT_VARIABLES.read_text()) if AGENT_VARIABLES.exists() else {}
    previous = current_api(runtime_state())
    previous_env = previous.get("env", {}) if previous else {}
    existing = (
        saved_agent.get("agent_runtime_resource")
        or variables.get("agent_runtime_resource")
        or previous_env.get("AGENT_RUNTIME_RESOURCE")
        or ""
    )
    environment = agent_environment(platform, app_url, provider, model)
    if dry_run:
        print(json.dumps({"update": existing or None, "environment": environment}, indent=2))
        return
    resource = deploy_agent_runtime(
        platform,
        app_url,
        provider=provider,
        model=model,
        existing=existing,
    )
    variables["agent_runtime_resource"] = resource
    variables["agent_provider"] = provider
    if model:
        variables["agent_model"] = model
    else:
        variables.pop("agent_model", None)
    print("Pointing the planning job at the deployed agent...", flush=True)
    apply(variables)
    print(f"Deployed planner: {resource}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["gemini", "openrouter"], default="gemini")
    parser.add_argument("--model", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        deploy_agent(provider=args.provider, model=args.model, dry_run=args.dry_run)
    except (DeploymentError, subprocess.CalledProcessError) as exc:
        print(f"Agent deployment failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
