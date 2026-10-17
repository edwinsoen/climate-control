"""Repository administration tasks."""

import subprocess
from pathlib import Path

from invoke import Exit, task

ROOT = Path(__file__).resolve().parent


@task(
    help={
        "dry_run": "Preview deployment using resource metadata without building or deploying.",
    }
)
def deploy(context, dry_run=False):
    """Build and deploy the API, worker, migrations, and Firebase UI to GCP."""
    command = ["uv", "run", "--extra", "agent-runtime", "python", "-m", "scripts.deploy_gcp"]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        raise Exit(code=result.returncode)


@task(
    help={
        "provider": "Model provider for the deployed agent: gemini (default) or openrouter.",
        "model": "Model ID; empty uses the provider's default alias.",
        "dry_run": "Show the agent configuration without deploying.",
    }
)
def deploy_agent(context, provider="gemini", model="", dry_run=False):
    """Deploy the rule planner to Agent Runtime and point the planning job at it."""
    command = ["uv", "run", "--extra", "agent-runtime", "python", "-m", "scripts.deploy_agent"]
    command.extend(["--provider", provider])
    if model:
        command.extend(["--model", model])
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        raise Exit(code=result.returncode)
