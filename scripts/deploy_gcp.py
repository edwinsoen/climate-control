"""One-command GCP release orchestration. Secret payloads are never downloaded."""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "infra/gcp/bootstrap"
RUNTIME = ROOT / "infra/gcp/runtime"
VARIABLES = RUNTIME / "deployment.auto.tfvars.json"
BACKEND = RUNTIME / "backend.tf.json"
AGENT_SOURCE = ROOT / "deploy/agent_runtime/climate_planner"
AGENT_RESOURCE = re.compile(r"projects/[^/\s]+/locations/[^/\s]+/reasoningEngines/\d+")
SECRETS = (
    "DATABASE_URL",
    "ENCRYPTION_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_ALLOWED_EMAILS",
    "ADMIN_PASSWORD_HASH",
    "AQICN_API_TOKEN",
    "OPENROUTER_API_KEY",
    "AGENT_SERVICE_TOKEN",
)
IDENTITIES = ("api", "worker", "migrate", "scheduler", "planner", "agent", "cloudbuild")


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


def add_secret_version(platform, key, payload):
    secret_id = platform["secret_ids"][key]
    raw = run(
        [
            "gcloud",
            "secrets",
            "versions",
            "add",
            secret_id,
            "--project",
            platform["project_id"],
            "--data-file=-",
            "--format=json",
        ],
        capture=True,
        input=payload,
    )
    created = json.loads(raw) if raw else {}
    name = created.get("name", "")
    return str(int(name.rsplit("/", 1)[-1])) if "/" in name else "1"


def discover_versions(platform, *, dry_run=False):
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
    if "DATABASE_URL" not in versions:
        raise DeploymentError("Populate enabled secret versions for: DATABASE_URL")
    google_keys = {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_ALLOWED_EMAILS"}
    google = google_keys & versions.keys()
    if google and len(google) != len(google_keys):
        raise DeploymentError(
            "Populate GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_ALLOWED_EMAILS together, "
            "or leave them unused."
        )
    if not google and "ADMIN_PASSWORD_HASH" not in versions:
        raise DeploymentError("Populate Google OAuth secrets or ADMIN_PASSWORD_HASH for login.")
    if "ENCRYPTION_KEY" not in versions:
        if "ENCRYPTION_KEY" not in platform["secret_ids"]:
            raise DeploymentError("Populate enabled secret versions for: ENCRYPTION_KEY")
        if dry_run:
            versions["ENCRYPTION_KEY"] = "1"
        else:
            print("Generating initial ENCRYPTION_KEY in Secret Manager...", flush=True)
            generated_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            versions["ENCRYPTION_KEY"] = add_secret_version(
                platform, "ENCRYPTION_KEY", generated_key
            )
    return versions


def settings_from_terraform(variables):
    with tempfile.TemporaryDirectory(prefix="climate-deploy-") as temporary:
        path = Path(temporary) / "inputs.tfvars.json"
        path.write_text(json.dumps(variables))
        result = terraform(
            RUNTIME,
            "console",
            f"-var-file={path}",
            capture=True,
            input="jsonencode({app_url = var.app_url})\n",
        )
    return json.loads(json.loads(result))


def platform_for_project(
    project_id,
    *,
    region="asia-southeast1",
    name="climate-control",
    site_id="",
    state_bucket="",
):
    region = region or "asia-southeast1"
    name = name or "climate-control"
    return {
        "project_id": project_id,
        "region": region,
        "name": name,
        "site_id": site_id or project_id,
        "state_bucket": state_bucket or f"{project_id}-{name}-tfstate",
        "identities": {
            role: f"{name}-{role}@{project_id}.iam.gserviceaccount.com" for role in IDENTITIES
        },
        "secret_ids": {key: f"{name}-{key.lower().replace('_', '-')}" for key in SECRETS},
    }


def state_resources(directory=RUNTIME):
    state = json.loads(terraform(directory, "show", "-json", capture=True))
    resources = state.get("values", {}).get("root_module", {}).get("resources", [])
    return {resource["address"]: resource["values"] for resource in resources}


def runtime_state():
    return state_resources(RUNTIME)


def init_terraform(directory, state_bucket="", prefix="runtime", *, dry_run=False):
    backend = BACKEND if directory == RUNTIME else directory / "backend.tf.json"
    if state_bucket:
        if not dry_run:
            for stale in (
                directory / ".terraform/terraform.tfstate",
                directory / "terraform.tfstate",
            ):
                stale.unlink(missing_ok=True)
        write_json(
            backend,
            {"terraform": {"backend": {"gcs": {"bucket": state_bucket, "prefix": prefix}}}},
        )
        terraform(directory, "init", "-input=false", "-reconfigure")
    else:
        backend.unlink(missing_ok=True)
        terraform(directory, "init", "-input=false")


def init_runtime(state_bucket="", *, dry_run=False):
    if VARIABLES.is_file():
        try:
            saved = json.loads(VARIABLES.read_text())
            if "google_allowed_emails" in saved:
                saved.pop("google_allowed_emails", None)
                write_json(VARIABLES, saved)
        except ValueError:
            pass
    init_terraform(RUNTIME, state_bucket, "runtime", dry_run=dry_run)


def _resource_name(item):
    return item.get("metadata", {}).get("name") or item.get("name", "").rsplit("/", 1)[-1]


def _try_gcloud_json(command):
    try:
        return json.loads(run([*command, "--quiet", "--format=json"], capture=True) or "[]")
    except subprocess.CalledProcessError:
        return []


def ensure_state_bucket(platform):
    project, region, bucket = (
        platform["project_id"],
        platform["region"],
        platform["state_bucket"],
    )
    existing = json.loads(
        run(
            [
                "gcloud",
                "storage",
                "buckets",
                "list",
                "--project",
                project,
                f"--filter=name={bucket}",
                "--format=json",
            ],
            capture=True,
        )
        or "[]"
    )
    if existing:
        return
    print(f"Creating Terraform state bucket gs://{bucket}...", flush=True)
    run(["gcloud", "services", "enable", "storage.googleapis.com", "--project", project, "--quiet"])
    run(
        [
            "gcloud",
            "storage",
            "buckets",
            "create",
            f"gs://{bucket}",
            "--project",
            project,
            "--location",
            region,
            "--uniform-bucket-level-access",
            "--public-access-prevention",
        ]
    )
    run(
        [
            "gcloud",
            "storage",
            "buckets",
            "update",
            f"gs://{bucket}",
            "--project",
            project,
            "--versioning",
        ]
    )


def adopt_existing_bootstrap(platform, variables, current=None):
    current = current or {}
    project, region, name, site = (
        platform["project_id"],
        platform["region"],
        platform["name"],
        platform["site_id"],
    )
    expected = (
        {
            "google_storage_bucket.state",
            "google_artifact_registry_repository.images",
            "google_firebase_project.app",
            "google_firebase_hosting_site.app",
        }
        | {f'google_service_account.runtime["{role}"]' for role in platform["identities"]}
        | {f'google_secret_manager_secret.config["{key}"]' for key in platform["secret_ids"]}
    )
    if expected <= current.keys():
        return
    repos = {
        _resource_name(item)
        for item in _try_gcloud_json(
            [
                "gcloud",
                "artifacts",
                "repositories",
                "list",
                "--project",
                project,
                "--location",
                region,
            ]
        )
    }
    accounts = {
        item.get("email", "")
        for item in _try_gcloud_json(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "list",
                "--project",
                project,
            ]
        )
    }
    secrets_found = {
        _resource_name(item)
        for item in _try_gcloud_json(["gcloud", "secrets", "list", "--project", project])
    }
    imports = [("google_storage_bucket.state", platform["state_bucket"])]
    if name in repos:
        imports.append(
            (
                "google_artifact_registry_repository.images",
                f"projects/{project}/locations/{region}/repositories/{name}",
            )
        )
    for role, email in platform["identities"].items():
        if email in accounts:
            imports.append(
                (
                    f'google_service_account.runtime["{role}"]',
                    f"projects/{project}/serviceAccounts/{email}",
                )
            )
    for key, secret_id in platform["secret_ids"].items():
        if secret_id in secrets_found:
            imports.append(
                (
                    f'google_secret_manager_secret.config["{key}"]',
                    f"projects/{project}/secrets/{secret_id}",
                )
            )
    missing = [(address, target) for address, target in imports if address not in current]
    firebase_missing = [
        (address, target)
        for address, target in (
            ("google_firebase_project.app", f"projects/{project}"),
            ("google_firebase_hosting_site.app", f"projects/{project}/sites/{site}"),
        )
        if address not in current
    ]
    if not missing and not firebase_missing:
        return
    print("Adopting existing GCP bootstrap resources into Terraform state...", flush=True)
    with tempfile.TemporaryDirectory(prefix="climate-bootstrap-") as temporary:
        path = Path(temporary) / "bootstrap.tfvars.json"
        path.write_text(json.dumps(variables))
        for address, target in missing:
            terraform(BOOTSTRAP, "import", "-input=false", f"-var-file={path}", address, target)
        for address, target in firebase_missing:
            try:
                terraform(BOOTSTRAP, "import", "-input=false", f"-var-file={path}", address, target)
            except subprocess.CalledProcessError:
                pass


def bootstrap_inputs():
    """Read local bootstrap settings so local and Cloud Build runs share one GCS state."""
    path = BOOTSTRAP / "terraform.tfvars"
    values = (
        dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]*)"', path.read_text(), re.MULTILINE))
        if path.is_file()
        else {}
    )
    if not values.get("project_id"):
        raise DeploymentError(f"Set project_id in {path} (see terraform.example.tfvars).")
    return {
        key: values.get(key, "")
        for key in ("project_id", "region", "name", "site_id", "state_bucket")
    }


def run_bootstrap(platform, *, dry_run=False):
    if not dry_run:
        ensure_state_bucket(platform)
    init_terraform(BOOTSTRAP, platform["state_bucket"], "bootstrap", dry_run=dry_run)
    variables = {
        "project_id": platform["project_id"],
        "region": platform["region"],
        "name": platform["name"],
        "site_id": platform["site_id"],
        "state_bucket": platform["state_bucket"],
    }
    tfvars = BOOTSTRAP / "bootstrap.auto.tfvars.json"
    write_json(tfvars, variables)
    if not dry_run:
        adopt_existing_bootstrap(platform, variables, state_resources(BOOTSTRAP))
        print("Updating bootstrap infrastructure...", flush=True)
        terraform(BOOTSTRAP, "apply", "-input=false", "-auto-approve", f"-var-file={tfvars}")


def adopt_existing_runtime(platform, variables, current=None):
    current = current or {}
    project, region, name = platform["project_id"], platform["region"], platform["name"]
    run_jobs = {
        _resource_name(item)
        for item in json.loads(
            run(
                [
                    "gcloud",
                    "run",
                    "jobs",
                    "list",
                    "--project",
                    project,
                    "--region",
                    region,
                    "--format=json",
                ],
                capture=True,
            )
            or "[]"
        )
    }
    services = {
        _resource_name(item)
        for item in json.loads(
            run(
                [
                    "gcloud",
                    "run",
                    "services",
                    "list",
                    "--project",
                    project,
                    "--region",
                    region,
                    "--format=json",
                ],
                capture=True,
            )
            or "[]"
        )
    }
    schedules = {
        _resource_name(item)
        for item in json.loads(
            run(
                [
                    "gcloud",
                    "scheduler",
                    "jobs",
                    "list",
                    "--project",
                    project,
                    "--location",
                    region,
                    "--format=json",
                ],
                capture=True,
            )
            or "[]"
        )
    }
    candidates = [
        (
            f'google_cloud_run_v2_job.job["{kind}"]',
            f"{name}-{kind}",
            run_jobs,
            f"projects/{project}/locations/{region}/jobs/{name}-{kind}",
        )
        for kind in ("worker", "migrate", "planner")
    ] + [
        (
            "google_cloud_run_v2_service.api[0]",
            f"{name}-api",
            services,
            f"projects/{project}/locations/{region}/services/{name}-api",
        ),
        (
            "google_cloud_scheduler_job.tick",
            f"{name}-tick",
            schedules,
            f"projects/{project}/locations/{region}/jobs/{name}-tick",
        ),
        (
            "google_cloud_scheduler_job.plan[0]",
            f"{name}-plan",
            schedules,
            f"projects/{project}/locations/{region}/jobs/{name}-plan",
        ),
    ]
    existing = [
        (address, target)
        for address, short, found, target in candidates
        if short in found and address not in current
    ]
    if not existing:
        return current
    print("Adopting existing GCP runtime resources into Terraform state...", flush=True)
    import_vars = {
        **variables,
        "api_enabled": True,
    }
    with tempfile.TemporaryDirectory(prefix="climate-import-") as temporary:
        path = Path(temporary) / "import.tfvars.json"
        path.write_text(json.dumps(import_vars))
        for address, target in existing:
            terraform(RUNTIME, "import", "-input=false", f"-var-file={path}", address, target)
    return runtime_state()


def current_api(resources):
    service = resources.get("google_cloud_run_v2_service.api[0]")
    # A failed first startup has no working revision to preserve. Retaining its
    # invalid configuration would prevent corrected secrets from taking effect.
    if not service or not service.get("latest_ready_revision"):
        return None
    container = service["template"][0]["containers"][0]
    versions = {}
    values = {}
    for env in container.get("env", []):
        sources = env.get("value_source", [])
        if sources and sources[0].get("secret_key_ref"):
            versions[env["name"]] = sources[0]["secret_key_ref"][0]["version"]
        elif "value" in env:
            values[env["name"]] = env["value"]
    return {"image": container["image"], "secret_versions": versions, "env": values}


def resolve_digest(repository, project_id, image):
    if re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", image):
        return image
    digest = run(
        [
            "gcloud",
            "artifacts",
            "docker",
            "images",
            "describe",
            image,
            "--project",
            project_id,
            "--format=value(image_summary.fully_qualified_digest)",
        ],
        capture=True,
    )
    if not re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", digest):
        raise DeploymentError(
            "Artifact Registry did not return the expected immutable image digest."
        )
    return digest


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


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        lock = AGENT_SOURCE.parents[2] / "uv.lock"
        if lock.is_file():
            for entry in tomllib.loads(lock.read_text()).get("package", []):
                if entry.get("name") == package and "version" in entry:
                    return entry["version"]
        raise


def agent_requirements() -> str:
    pins = {
        "google-adk[mcp]": _package_version("google-adk"),
        "google-cloud-agentplatform[agent-engines]": _package_version(
            "google-cloud-agentplatform"
        ),
        "litellm": _package_version("litellm"),
        "httpx2": _package_version("httpx2"),
    }
    return "".join(f"{package}=={pinned}\n" for package, pinned in pins.items())


def secret_name(project, secret_id):
    return f"projects/{project}/secrets/{secret_id}"


def agent_environment(platform, app_url, provider, model):
    project, secrets_map = platform["project_id"], platform["secret_ids"]
    environment = {
        "AGENT_PROVIDER": provider,
        "AGENT_MODEL": model,
        "AGENT_MODEL_PROJECT": project,
        "CLIMATE_CONTROL_MCP_URL": app_url.rstrip("/") + "/mcp",
        "CLIMATE_CONTROL_TOKEN_SECRET": secret_name(project, secrets_map["AGENT_SERVICE_TOKEN"]),
    }
    if provider == "openrouter":
        environment["OPENROUTER_API_KEY_SECRET"] = secret_name(
            project, secrets_map["OPENROUTER_API_KEY"]
        )
    return environment


def stage_agent(directory: Path, environment: dict, *, requirements_text: str = "") -> Path:
    target = directory / AGENT_SOURCE.name
    shutil.copytree(AGENT_SOURCE, target, ignore=shutil.ignore_patterns("__pycache__"))
    (target / "requirements.txt").write_text(requirements_text or agent_requirements())
    (target / ".env").write_text("".join(f"{k}={v}\n" for k, v in environment.items() if v))
    return target


def adk_command() -> str | None:
    candidate = Path(sys.executable).parent / "adk"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("adk")


def deploy_agent_runtime(platform, app_url, *, provider="gemini", model="", existing=""):
    adk = adk_command()
    if not adk:
        raise DeploymentError(
            "Install google-adk (uv sync --extra agent-runtime) to deploy the agent."
        )
    environment = agent_environment(platform, app_url, provider, model)
    with tempfile.TemporaryDirectory(prefix="climate-agent-") as temporary:
        folder = stage_agent(Path(temporary), environment)
        command = [
            adk,
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
            str(AGENT_SOURCE.parents[2] / "src/climate_control"),
            "--temp_folder",
            str(Path(temporary) / "build"),
        ]
        if existing:
            command += ["--agent_engine_id", existing.rsplit("/", 1)[-1]]
        print("Deploying planner to Agent Runtime...", flush=True)
        result = run([*command, str(folder)], capture=True)
    if result:
        print(result, flush=True)
    resources = set(AGENT_RESOURCE.findall(result)) or ({existing} if existing else set())
    if len(resources) != 1:
        raise DeploymentError("Could not determine the Agent Runtime resource name.")
    resource = resources.pop()
    saved_agent = {"agent_runtime_resource": resource, "agent_provider": provider}
    if model:
        saved_agent["agent_model"] = model
    write_json(RUNTIME / "agent.auto.tfvars.json", saved_agent)
    return resource


def deploy(
    *,
    dry_run=False,
    bootstrap_only=False,
    skip_build=False,
    project_id="",
    region="asia-southeast1",
    name="climate-control",
    site_id="",
    state_bucket="",
    image="",
    app_url="",
    agent_provider="",
    agent_model="",
    agent_runtime_resource="",
):
    if bootstrap_only:
        required_tools = ("terraform", "gcloud")
    elif skip_build:
        required_tools = ("terraform", "gcloud", "firebase")
    else:
        required_tools = ("terraform", "gcloud", "docker", "npm", "firebase")
    missing = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing:
        raise DeploymentError("Install required deployment tools: " + ", ".join(missing))
    if project_id:
        platform = platform_for_project(
            project_id,
            region=region,
            name=name,
            site_id=site_id,
            state_bucket=state_bucket,
        )
        if bootstrap_only:
            run_bootstrap(platform, dry_run=dry_run)
            return
        repository = (
            f"{platform['region']}-docker.pkg.dev/{platform['project_id']}/{platform['name']}/app"
        )
    else:
        inputs = bootstrap_inputs()
        run_bootstrap(
            platform_for_project(
                inputs["project_id"],
                region=inputs["region"],
                name=inputs["name"],
                site_id=inputs["site_id"],
                state_bucket=state_bucket or inputs["state_bucket"],
            ),
            dry_run=dry_run,
        )
        if bootstrap_only:
            return
        platform = output(BOOTSTRAP, "platform")
        repository = output(BOOTSTRAP, "image_repository")
    bucket = state_bucket or platform.get("state_bucket", "")
    init_runtime(bucket, dry_run=dry_run)
    print("Resolving enabled secret versions (metadata only)...", flush=True)
    versions = discover_versions(platform, dry_run=dry_run)
    variables = {
        "platform": platform,
        "image": repository + "@sha256:" + "0" * 64,
        "secret_versions": versions,
        "api_image": None,
        "api_secret_versions": None,
    }
    settings = settings_from_terraform(variables)
    resources = runtime_state()
    if bucket and not dry_run and "google_cloud_scheduler_job.tick" not in resources:
        resources = adopt_existing_runtime(platform, variables, resources)
    previous = current_api(resources)
    if (
        previous
        and "GOOGLE_CLIENT_ID" in previous["secret_versions"]
        and "GOOGLE_ALLOWED_EMAILS" not in previous["secret_versions"]
        and "GOOGLE_ALLOWED_EMAILS" in versions
    ):
        previous["secret_versions"]["GOOGLE_ALLOWED_EMAILS"] = versions["GOOGLE_ALLOWED_EMAILS"]
    previous_env = previous.get("env", {}) if previous else {}
    default_app_url = f"https://{platform['site_id']}.web.app"
    saved_app_url = previous_env.get("APP_URL")
    if saved_app_url == default_app_url:
        saved_app_url = None
    variables["app_url"] = app_url or settings.get("app_url") or saved_app_url
    saved_agent = {}
    agent_tfvars = RUNTIME / "agent.auto.tfvars.json"
    if agent_tfvars.is_file():
        try:
            saved_agent = json.loads(agent_tfvars.read_text())
        except ValueError:
            saved_agent = {}
    resolved_provider = (
        agent_provider
        or saved_agent.get("agent_provider")
        or previous_env.get("AGENT_PROVIDER")
    )
    if resolved_provider:
        variables["agent_provider"] = resolved_provider
    saved_model = (
        saved_agent.get("agent_model")
        if not agent_provider or agent_provider == saved_agent.get("agent_provider")
        else None
    )
    previous_model = (
        previous_env.get("AGENT_MODEL")
        if not resolved_provider or resolved_provider == previous_env.get("AGENT_PROVIDER")
        else None
    )
    resolved_model = agent_model or saved_model or previous_model
    if resolved_model:
        variables["agent_model"] = resolved_model
    resolved_resource = (
        agent_runtime_resource
        or saved_agent.get("agent_runtime_resource")
        or previous_env.get("AGENT_RUNTIME_RESOURCE")
    )
    if resolved_resource and resolved_provider != "disabled":
        variables["agent_runtime_resource"] = resolved_resource
    resolved_agent_provider = variables.get("agent_provider") or "gemini"
    if (
        resolved_agent_provider == "openrouter"
        and "OPENROUTER_API_KEY" in platform.get("secret_ids", {})
        and "OPENROUTER_API_KEY" not in versions
    ):
        raise DeploymentError("Populate enabled secret versions for: OPENROUTER_API_KEY")
    raw_agent_token = ""
    agent_token_hash = ""
    if (
        not dry_run
        and resolved_agent_provider != "disabled"
        and "AGENT_SERVICE_TOKEN" in platform.get("secret_ids", {})
        and "AGENT_SERVICE_TOKEN" not in versions
    ):
        raw_agent_token = "cc_" + secrets.token_urlsafe(32)
        agent_token_hash = hashlib.sha256(raw_agent_token.encode("utf-8")).hexdigest()
    if dry_run:
        print(
            json.dumps(
                {
                    "project": platform["project_id"],
                    "region": platform["region"],
                    "site": platform["site_id"],
                    "image_repository": repository,
                    "secret_versions": versions,
                    "preserve_existing_api_until_migrated": previous is not None,
                    "steps": [
                        "build UI and backend",
                        "push image and resolve digest",
                        "pause schedules and drain workers and planner",
                        "update jobs",
                        "run migrations",
                        "deploy API and check readiness",
                        "publish Firebase and check readiness",
                        "deploy planner to Agent Runtime",
                        "enable minute schedule",
                    ],
                },
                indent=2,
            )
        )
        return
    if skip_build:
        if not image:
            raise DeploymentError("Supply --image when using --skip-build.")
        if not (ROOT / "frontend/dist/index.html").is_file():
            raise DeploymentError("Build the UI before running with --skip-build.")
    else:
        print("Building UI...", flush=True)
        run(["npm", "ci"], cwd=ROOT / "frontend")
        run(["npm", "run", "build"], cwd=ROOT / "frontend")
        if not image:
            tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
            image = repository + ":" + tag
            run(["gcloud", "auth", "configure-docker", repository.split("/")[0], "--quiet"])
            print("Building and pushing backend...", flush=True)
            run(
                [
                    "docker",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--target",
                    "backend",
                    "-t",
                    image,
                    ".",
                ]
            )
            run(["docker", "push", image])
    digest = resolve_digest(repository, platform["project_id"], image)
    variables.update(
        image=digest,
        api_enabled=previous is not None,
        scheduler_paused=True,
        api_image=previous["image"] if previous else None,
        api_secret_versions=previous["secret_versions"] if previous else None,
    )
    if agent_tfvars.is_file():
        synced_agent = {"agent_provider": resolved_agent_provider}
        if "agent_model" in variables:
            synced_agent["agent_model"] = variables["agent_model"]
        if "agent_runtime_resource" in variables:
            synced_agent["agent_runtime_resource"] = variables["agent_runtime_resource"]
        write_json(agent_tfvars, synced_agent)
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
        migrate_command = [
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
        if agent_token_hash:
            migrate_command.append(
                f"--args=-m,climate_control.cli,migrate,--agent-token-hash,{agent_token_hash}"
            )
        run(migrate_command)
        if raw_agent_token:
            print("Provisioning AGENT_SERVICE_TOKEN in Secret Manager...", flush=True)
            versions["AGENT_SERVICE_TOKEN"] = add_secret_version(
                platform, "AGENT_SERVICE_TOKEN", raw_agent_token
            )
            variables["secret_versions"] = versions
        print("Deploying API...", flush=True)
        variables.update(api_enabled=True, api_image=None, api_secret_versions=None)
        apply(variables)
        ready(output(RUNTIME, "api_url"))
        print("Publishing Firebase UI...", flush=True)
        run([sys.executable, str(ROOT / "scripts/release_firebase.py"), "--skip-build"])
        origin = output(RUNTIME, "app_url")
        ready(origin)
        if (
            resolved_agent_provider != "disabled"
            and "agent" in platform.get("identities", {})
            and "AGENT_SERVICE_TOKEN" in versions
        ):
            # The planner is optional; its failure must not keep device polling paused.
            try:
                resource = deploy_agent_runtime(
                    platform,
                    origin,
                    provider=resolved_agent_provider,
                    model=variables.get("agent_model", ""),
                    existing=variables.get("agent_runtime_resource", ""),
                )
            except (DeploymentError, subprocess.CalledProcessError) as exc:
                print(
                    f"WARNING: Agent Runtime deploy failed; keeping the previous planner: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                variables["agent_runtime_resource"] = resource
                variables["agent_provider"] = resolved_agent_provider
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
    parser.add_argument("--bootstrap-only", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--region", default="asia-southeast1")
    parser.add_argument("--name", default="climate-control")
    parser.add_argument("--site-id", default="")
    parser.add_argument("--state-bucket", default="")
    parser.add_argument("--image", default="")
    parser.add_argument("--app-url", default="")
    parser.add_argument("--agent-provider", default="")
    parser.add_argument("--agent-model", default="")
    parser.add_argument("--agent-runtime-resource", default="")
    args = parser.parse_args()
    try:
        with (RUNTIME / ".deploy.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError("Another deployment is running in this checkout.") from None
            deploy(
                dry_run=args.dry_run,
                bootstrap_only=args.bootstrap_only,
                skip_build=args.skip_build,
                project_id=args.project_id,
                region=args.region,
                name=args.name,
                site_id=args.site_id,
                state_bucket=args.state_bucket,
                image=args.image,
                app_url=args.app_url,
                agent_provider=args.agent_provider,
                agent_model=args.agent_model,
                agent_runtime_resource=args.agent_runtime_resource,
            )
    except (DeploymentError, subprocess.CalledProcessError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
