"""Repository administration tasks."""

import subprocess
from pathlib import Path

from invoke import Exit, task

ROOT = Path(__file__).resolve().parent


@task(
    help={
        "dry_run": "Preview deployment using resource metadata without building or deploying.",
        "allowed_emails": "Google login allowlist; saved for subsequent deployments.",
    }
)
def deploy(context, dry_run=False, allowed_emails=""):
    """Build and deploy the API, worker, migrations, and Firebase UI to GCP."""
    command = ["uv", "run", "python", "-m", "scripts.deploy_gcp"]
    if dry_run:
        command.append("--dry-run")
    if allowed_emails:
        command.extend(["--allowed-emails", allowed_emails])
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        raise Exit(code=result.returncode)
