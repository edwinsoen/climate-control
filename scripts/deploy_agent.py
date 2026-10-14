"""Deploy the rule planner to Agent Runtime and point the planning job at it.

Run `invoke deploy` first. Secret payloads are never downloaded: the deployed agent reads
its MCP token and model key from Secret Manager with its own service account.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from scripts.deploy_gcp import (
    BOOTSTRAP,
    ROOT,
    RUNTIME,
    VARIABLES,
    DeploymentError,
    discover_versions,
    output,
    run,
    terraform,
    write_json,
)

AGENT_SOURCE = ROOT / "deploy/agent_runtime/climate_planner"
AGENT_VARIABLES = RUNTIME / "agent.auto.tfvars.json"
RESOURCE = re.compile(r"projects/[^/\s]+/locations/[^/\s]+/reasoningEngines/\d+")


def requirements() -> str:
    pins = {
        "google-adk[mcp]": version("google-adk"),
        "google-cloud-agentplatform[agent-engines]": version("google-cloud-agentplatform"),
        "litellm": version("litellm"),
        "httpx2": version("httpx2"),
    }
    return "".join(f"{package}=={pinned}\n" for package, pinned in pins.items())


def secret_name(project, secret_id):
    return f"projects/{project}/secrets/{secret_id}"


def agent_environment(platform, app_url, provider, model):
    project, secrets = platform["project_id"], platform["secret_ids"]
    environment = {
        "AGENT_PROVIDER": provider,
        "AGENT_MODEL": model,
        "AGENT_MODEL_PROJECT": project,
        "CLIMATE_CONTROL_MCP_URL": app_url.rstrip("/") + "/mcp",
        "CLIMATE_CONTROL_TOKEN_SECRET": secret_name(project, secrets["AGENT_SERVICE_TOKEN"]),
    }
    if provider == "openrouter":
        environment["OPENROUTER_API_KEY_SECRET"] = secret_name(
            project, secrets["OPENROUTER_API_KEY"]
        )
    return environment


def stage(directory: Path, environment: dict) -> Path:
    target = directory / AGENT_SOURCE.name
    shutil.copytree(AGENT_SOURCE, target, ignore=shutil.ignore_patterns("__pycache__"))
    (target / "requirements.txt").write_text(requirements())
    (target / ".env").write_text("".join(f"{k}={v}\n" for k, v in environment.items() if v))
    return target


def deploy_agent(*, provider="gemini", model="", dry_run=False):
    if not VARIABLES.exists():
        raise DeploymentError("Run invoke deploy before deploying the agent.")
    platform = output(BOOTSTRAP, "platform")
    app_url = output(RUNTIME, "app_url")
    versions = discover_versions(platform)
    required = {"AGENT_SERVICE_TOKEN"} | (
        {"OPENROUTER_API_KEY"} if provider == "openrouter" else set()
    )
    missing = required - versions.keys()
    if missing:
        raise DeploymentError(
            "Populate enabled secret versions for: " + ", ".join(sorted(missing)) + ". "
            "Create a read-scoped access token on the Accounts page for AGENT_SERVICE_TOKEN."
        )
    existing = (
        json.loads(AGENT_VARIABLES.read_text()).get("agent_runtime_resource", "")
        if AGENT_VARIABLES.exists()
        else ""
    )
    environment = agent_environment(platform, app_url, provider, model)
    if dry_run:
        print(json.dumps({"update": existing or None, "environment": environment}, indent=2))
        return
    with tempfile.TemporaryDirectory(prefix="climate-agent-") as temporary:
        folder = stage(Path(temporary), environment)
        command = [
            str(Path(sys.executable).parent / "adk"),
            "deploy",
            "agent_engine",
            "--project",
            platform["project_id"],
            "--region",
            platform["region"],
            "--display_name",
            f"{platform['name']} planner",
            "--description",
            "Plans Climate Control device rules.",
            "--service_account",
            platform["identities"]["agent"],
            "--otel_to_cloud",
            "--extra_packages",
            str(ROOT / "src/climate_control"),
            "--temp_folder",
            str(Path(temporary) / "build"),
        ]
        if existing:
            command += ["--agent_engine_id", existing.rsplit("/", 1)[-1]]
        print("Deploying planner to Agent Runtime...", flush=True)
        result = run([*command, str(folder)], capture=True)
    print(result, flush=True)
    resources = set(RESOURCE.findall(result)) or ({existing} if existing else set())
    if len(resources) != 1:
        raise DeploymentError("Could not determine the Agent Runtime resource name.")
    resource = resources.pop()
    write_json(AGENT_VARIABLES, {"agent_runtime_resource": resource, "agent_provider": provider})
    print("Pointing the planning job at the deployed agent...", flush=True)
    terraform(RUNTIME, "apply", "-input=false", "-auto-approve", f"-var-file={VARIABLES}")
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
