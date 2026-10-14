"""One-command GCP release orchestration. Secret payloads are never downloaded."""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "infra/gcp/bootstrap"
RUNTIME = ROOT / "infra/gcp/runtime"
VARIABLES = RUNTIME / "deployment.auto.tfvars.json"


class DeploymentError(Exception):
    pass


def run(command, *, cwd=ROOT, capture=False, input=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        input=input,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def terraform(directory, *arguments, **kwargs):
    return run(["terraform", f"-chdir={directory}", *arguments], **kwargs)


def output(directory, name):
    return json.loads(terraform(directory, "output", "-json", name, capture=True))


def write_json(path, value):
    """Atomically replace non-secret deployment inputs without leaving partial JSON."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        json.dump(value, temporary, indent=2)
        temporary.write("\n")
    Path(temporary.name).replace(path)


def discover_versions(platform):
    versions = {}
    for key, secret_id in platform["secret_ids"].items():
        metadata = json.loads(
            run(
                [
                    "gcloud",
                    "secrets",
                    "versions",
                    "list",
                    secret_id,
                    "--project",
                    platform["project_id"],
                    "--format=json",
                ],
                capture=True,
            )
        )
        enabled = [
            int(version["name"].rsplit("/", 1)[-1])
            for version in metadata
            if version.get("state") == "ENABLED"
        ]
        if enabled:
            versions[key] = str(max(enabled))
    missing = {"DATABASE_URL", "ENCRYPTION_KEY"} - versions.keys()
    if missing:
        raise DeploymentError("Populate enabled secret versions for: " + ", ".join(sorted(missing)))
    google = {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"} & versions.keys()
    if google and len(google) != 2:
        raise DeploymentError("Populate both Google OAuth secrets, or leave both unused.")
    if not google and "ADMIN_PASSWORD_HASH" not in versions:
        raise DeploymentError("Populate Google OAuth secrets or ADMIN_PASSWORD_HASH for login.")
    return versions


def runtime_state():
    state = json.loads(terraform(RUNTIME, "show", "-json", capture=True))
    resources = state.get("values", {}).get("root_module", {}).get("resources", [])
    return {resource["address"]: resource["values"] for resource in resources}


def settings_from_terraform(variables):
    with tempfile.TemporaryDirectory(prefix="climate-deploy-") as temporary:
        path = Path(temporary) / "inputs.tfvars.json"
        path.write_text(json.dumps(variables))
        result = terraform(
            RUNTIME,
            "console",
            f"-var-file={path}",
            capture=True,
            input="jsonencode({google_allowed_emails = var.google_allowed_emails, "
            "app_url = var.app_url})\n",
        )
    return json.loads(json.loads(result))


def allowed_emails(explicit, configured, *, dry_run):
    local = dotenv_values(ROOT / ".env").get("GOOGLE_ALLOWED_EMAILS", "")
    value = explicit or os.environ.get("GOOGLE_ALLOWED_EMAILS") or configured or local
    if not value and not dry_run:
        if not sys.stdin.isatty():
            raise DeploymentError(
                "Set GOOGLE_ALLOWED_EMAILS or use --allowed-emails for Google login."
            )
        value = input("Google login allowed emails (comma-separated): ").strip()
    if not value:
        return ""
    emails = [email.strip().lower() for email in value.split(",") if email.strip()]
    if not emails or any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) for email in emails):
        raise DeploymentError("Supply valid comma-separated Google login email addresses.")
    return ",".join(dict.fromkeys(emails))


def current_api(resources):
    service = resources.get("google_cloud_run_v2_service.api[0]")
    # A failed first startup has no working revision to preserve. Retaining its
    # invalid configuration would prevent corrected secrets from taking effect.
    if not service or not service.get("latest_ready_revision"):
        return None
    container = service["template"][0]["containers"][0]
    versions = {}
    for env in container.get("env", []):
        sources = env.get("value_source", [])
        if sources and sources[0].get("secret_key_ref"):
            versions[env["name"]] = sources[0]["secret_key_ref"][0]["version"]
    return {"image": container["image"], "secret_versions": versions}


def apply(variables):
    write_json(VARIABLES, variables)
    terraform(RUNTIME, "apply", "-input=false", "-auto-approve", f"-var-file={VARIABLES}")


SCHEDULES = {
    "tick": "google_cloud_scheduler_job.tick",
    "plan": "google_cloud_scheduler_job.plan[0]",
}


def scheduler_command(platform, action, kind="tick"):
    return [
        "gcloud",
        "scheduler",
        "jobs",
        action,
        f"{platform['name']}-{kind}",
        "--project",
        platform["project_id"],
        "--location",
        platform["region"],
        "--quiet",
    ]


def execution_finished(execution):
    status = execution.get("status", execution)
    return bool(status.get("completionTime")) or any(
        condition.get("type") == "Completed"
        and condition.get("status") in (True, False, "True", "False")
        for condition in status.get("conditions", [])
    )


def drain_job(platform, kind, *, timeout=2100):
    deadline = time.monotonic() + timeout
    while True:
        executions = json.loads(
            run(
                [
                    "gcloud",
                    "run",
                    "jobs",
                    "executions",
                    "list",
                    "--job",
                    f"{platform['name']}-{kind}",
                    "--project",
                    platform["project_id"],
                    "--region",
                    platform["region"],
                    "--format=json",
                ],
                capture=True,
            )
        )
        active = [execution for execution in executions if not execution_finished(execution)]
        if not active:
            return
        if time.monotonic() >= deadline:
            raise DeploymentError(
                f"Existing {kind} executions have not finished; scheduling stays paused."
            )
        print(f"Waiting for {len(active)} {kind} execution(s) to finish...", flush=True)
        time.sleep(5)


def ready(origin, *, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urlopen(origin.rstrip("/") + "/health/ready", timeout=10) as response:
                if response.status == 200 and json.load(response).get("status") == "ready":
                    return
        except (URLError, TimeoutError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise DeploymentError(f"Readiness check failed for {origin}; scheduling stays paused.")
        time.sleep(5)


def deploy(*, dry_run=False, emails=""):
    missing = [
        tool
        for tool in ("terraform", "gcloud", "docker", "npm", "firebase")
        if shutil.which(tool) is None
    ]
    if missing:
        raise DeploymentError("Install required deployment tools: " + ", ".join(missing))
    terraform(BOOTSTRAP, "init", "-input=false")
    if not dry_run:
        print("Updating bootstrap infrastructure...", flush=True)
        terraform(BOOTSTRAP, "apply", "-input=false", "-auto-approve")
    platform = output(BOOTSTRAP, "platform")
    repository = output(BOOTSTRAP, "image_repository")
    terraform(RUNTIME, "init", "-input=false")
    print("Resolving enabled secret versions (metadata only)...", flush=True)
    versions = discover_versions(platform)
    variables = {
        "platform": platform,
        "image": repository + "@sha256:" + "0" * 64,
        "secret_versions": versions,
        "api_image": None,
        "api_secret_versions": None,
    }
    settings = settings_from_terraform(variables)
    variables["app_url"] = settings["app_url"]
    if "GOOGLE_CLIENT_ID" in versions:
        variables["google_allowed_emails"] = allowed_emails(
            emails,
            settings["google_allowed_emails"],
            dry_run=dry_run,
        )
        if not variables["google_allowed_emails"] and not dry_run:
            raise DeploymentError("Google login needs at least one allowed email.")
    else:
        variables["google_allowed_emails"] = settings["google_allowed_emails"] or ""
    resources = runtime_state()
    previous = current_api(resources)
    if dry_run:
        print(
            json.dumps(
                {
                    "project": platform["project_id"],
                    "region": platform["region"],
                    "site": platform["site_id"],
                    "image_repository": repository,
                    "secret_versions": versions,
                    "google_allowed_emails": variables["google_allowed_emails"],
                    "email_prompt_needed": "GOOGLE_CLIENT_ID" in versions
                    and not variables["google_allowed_emails"],
                    "preserve_existing_api_until_migrated": previous is not None,
                    "steps": [
                        "build UI and backend",
                        "push image and resolve digest",
                        "pause schedules and drain workers and planner",
                        "update jobs",
                        "run migrations",
                        "deploy API and check readiness",
                        "publish Firebase and check readiness",
                        "enable minute schedule",
                    ],
                },
                indent=2,
            )
        )
        return
    print("Building UI...", flush=True)
    run(["npm", "ci"], cwd=ROOT / "frontend")
    run(["npm", "run", "build"], cwd=ROOT / "frontend")
    tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    image = repository + ":" + tag
    run(["gcloud", "auth", "configure-docker", repository.split("/")[0], "--quiet"])
    print("Building and pushing backend...", flush=True)
    run(["docker", "build", "--platform", "linux/amd64", "--target", "backend", "-t", image, "."])
    run(["docker", "push", image])
    digest = run(
        [
            "gcloud",
            "artifacts",
            "docker",
            "images",
            "describe",
            image,
            "--project",
            platform["project_id"],
            "--format=value(image_summary.fully_qualified_digest)",
        ],
        capture=True,
    )
    if not re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", digest):
        raise DeploymentError(
            "Artifact Registry did not return the expected immutable image digest."
        )
    variables.update(
        image=digest,
        api_enabled=previous is not None,
        scheduler_paused=True,
        api_image=previous["image"] if previous else None,
        api_secret_versions=previous["secret_versions"] if previous else None,
    )
    write_json(RUNTIME / "platform.auto.tfvars.json", {"platform": platform})
    write_json(VARIABLES, variables)
    schedules = [kind for kind, address in SCHEDULES.items() if address in resources]
    try:
        if schedules:
            print("Pausing schedules and draining workers...", flush=True)
            for kind in schedules:
                run(scheduler_command(platform, "pause", kind))
        for kind in ("worker", "planner", "migrate"):
            if f'google_cloud_run_v2_job.job["{kind}"]' in resources:
                drain_job(platform, kind)
        print("Updating worker, planner, and migration jobs...", flush=True)
        apply(variables)
        schedules = [kind for kind, address in SCHEDULES.items() if address in runtime_state()]
        print("Running database migrations...", flush=True)
        run(
            [
                "gcloud",
                "run",
                "jobs",
                "execute",
                f"{platform['name']}-migrate",
                "--project",
                platform["project_id"],
                "--region",
                platform["region"],
                "--wait",
                "--quiet",
            ]
        )
        print("Deploying API...", flush=True)
        variables.update(api_enabled=True, api_image=None, api_secret_versions=None)
        apply(variables)
        ready(output(RUNTIME, "api_url"))
        print("Publishing Firebase UI...", flush=True)
        run([sys.executable, str(ROOT / "scripts/release_firebase.py"), "--skip-build"])
        origin = output(RUNTIME, "app_url")
        ready(origin)
        print("Enabling minute schedule...", flush=True)
        variables["scheduler_paused"] = False
        apply(variables)
        print(f"Deployed: {origin}", flush=True)
    except BaseException:
        variables["scheduler_paused"] = True
        write_json(VARIABLES, variables)
        if schedules:
            try:
                for kind in schedules:
                    run(scheduler_command(platform, "pause", kind))
                print(
                    "Deployment stopped; polling is paused. Rerun invoke deploy to retry.",
                    flush=True,
                )
            except subprocess.CalledProcessError:
                print(
                    "Could not pause polling after the failure; check the scheduler.",
                    file=sys.stderr,
                )
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allowed-emails", default="")
    args = parser.parse_args()
    try:
        with (RUNTIME / ".deploy.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError("Another deployment is running in this checkout.") from None
            deploy(dry_run=args.dry_run, emails=args.allowed_emails)
    except (DeploymentError, subprocess.CalledProcessError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
