"""Exercise deployment sequencing with process stubs; never modify GCP resources."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "deploy_gcp",
    Path(__file__).resolve().parents[1] / "scripts/deploy_gcp.py",
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    bootstrap = tmp_path / "infra/bootstrap"
    runtime = tmp_path / "infra/runtime"
    bootstrap.mkdir(parents=True)
    runtime.mkdir(parents=True)
    (bootstrap / "terraform.tfvars").write_text(
        'project_id = "test-project"\n# site_id = "ignored"\nsite_id = "test-site"\n'
    )
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "BOOTSTRAP", bootstrap)
    monkeypatch.setattr(deploy, "RUNTIME", runtime)
    monkeypatch.setattr(deploy, "VARIABLES", runtime / "deployment.auto.tfvars.json")
    monkeypatch.setattr(deploy, "BACKEND", runtime / "backend.tf.json")
    monkeypatch.setattr(deploy.shutil, "which", lambda name: name)
    repository = "asia-southeast1-docker.pkg.dev/test-project/climate-control/app"
    platform = {
        "project_id": "test-project",
        "region": "asia-southeast1",
        "name": "climate-control",
        "site_id": "test-site",
        "identities": {},
        "secret_ids": {
            key: key.lower()
            for key in (
                "DATABASE_URL",
                "ENCRYPTION_KEY",
                "GOOGLE_CLIENT_ID",
                "GOOGLE_CLIENT_SECRET",
                "GOOGLE_ALLOWED_EMAILS",
                "ADMIN_PASSWORD_HASH",
                "AQICN_API_TOKEN",
            )
        },
    }
    session = {
        "commands": [],
        "applies": [],
        "resources": [],
        "failure": None,
        "settings": {"app_url": None},
        "console_output": "{value}",
        "missing_secret": None,
        "readiness": [],
        "repository": repository,
        "platform": platform,
    }

    def run(command, **kwargs):
        session["commands"].append(command)
        if command[0] == "terraform":
            if "output" in command:
                return json.dumps(
                    {
                        "platform": platform,
                        "image_repository": repository,
                        "api_url": "https://api.example.run.app",
                        "app_url": "https://test-site.web.app",
                    }[command[-1]]
                )
            if "show" in command:
                return json.dumps({"values": {"root_module": {"resources": session["resources"]}}})
            if "console" in command:
                value = json.dumps(json.dumps(session["settings"]))
                return session["console_output"].format(value=value)
            if "apply" in command and command[1] == f"-chdir={runtime}":
                variables = json.loads(deploy.VARIABLES.read_text())
                session["applies"].append(variables)
                if session["failure"] == "final-apply" and not variables["scheduler_paused"]:
                    raise subprocess.CalledProcessError(1, command)
        if command[:4] == ["gcloud", "secrets", "versions", "list"]:
            missing = session["missing_secret"]
            if command[4] in ("admin_password_hash", missing) or (
                isinstance(missing, set) and command[4] in missing
            ):
                return "[]"
            return json.dumps(
                [
                    {"name": f"{command[4]}/versions/{version}", "state": state}
                    for version, state in [(9, "ENABLED"), (11, "DISABLED"), (10, "ENABLED")]
                ]
            )
        if command[:4] == ["gcloud", "secrets", "versions", "add"]:
            session.setdefault("added_secrets", {})[command[4]] = kwargs.get("input", "")
            return json.dumps({"name": f"projects/test-project/secrets/{command[4]}/versions/1"})
        if command[:5] == ["gcloud", "artifacts", "docker", "images", "describe"]:
            return repository + "@sha256:" + "a" * 64
        if command[:5] == ["gcloud", "run", "jobs", "executions", "list"]:
            return json.dumps(
                [{"status": {"conditions": [{"type": "Completed", "status": "False"}]}}]
            )
        if command[:4] == ["gcloud", "run", "jobs", "execute"]:
            if session["failure"] == "migration":
                raise subprocess.CalledProcessError(1, command)
        if Path(command[0]).name == "adk":
            if "--agent_engine_id" in command:
                engine_id = command[command.index("--agent_engine_id") + 1]
                return (
                    f"projects/test-project/locations/asia-southeast1/reasoningEngines/{engine_id}"
                )
            return "projects/test-project/locations/asia-southeast1/reasoningEngines/1"
        if command[:3] == ["npm", "run", "build"] and session["failure"] == "build":
            raise subprocess.CalledProcessError(1, command)
        if command[0] == sys.executable and session["failure"] == "firebase":
            raise subprocess.CalledProcessError(1, command)
        return ""

    monkeypatch.setattr(deploy, "run", run)
    monkeypatch.setattr(deploy, "ready", session["readiness"].append)
    return session


def existing_runtime(session):
    old_image = session["repository"] + "@sha256:" + "b" * 64
    session["resources"] = [
        {
            "address": "google_cloud_run_v2_service.api[0]",
            "values": {
                "latest_ready_revision": "climate-control-api-ready",
                "template": [
                    {
                        "containers": [
                            {
                                "image": old_image,
                                "env": [
                                    {
                                        "name": name,
                                        "value_source": [{"secret_key_ref": [{"version": "1"}]}],
                                    }
                                    for name in (
                                        "DATABASE_URL",
                                        "ENCRYPTION_KEY",
                                        "GOOGLE_CLIENT_ID",
                                        "GOOGLE_CLIENT_SECRET",
                                        "GOOGLE_ALLOWED_EMAILS",
                                    )
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        {"address": "google_cloud_scheduler_job.tick", "values": {}},
        {"address": "google_cloud_scheduler_job.plan[0]", "values": {}},
        {"address": 'google_cloud_run_v2_job.job["worker"]', "values": {}},
        {"address": 'google_cloud_run_v2_job.job["planner"]', "values": {}},
        {"address": 'google_cloud_run_v2_job.job["migrate"]', "values": {}},
    ]
    return old_image


def test_first_deployment_enables_api_after_migration_and_schedule_after_ready(deployment):
    deploy.deploy()
    staged, api, active = deployment["applies"]
    assert not staged["api_enabled"] and staged["scheduler_paused"]
    assert api["api_enabled"] and api["scheduler_paused"]
    assert not active["scheduler_paused"]
    assert deployment["readiness"] == ["https://api.example.run.app", "https://test-site.web.app"]
    assert active["secret_versions"]["DATABASE_URL"] == "10"
    assert active["secret_versions"]["GOOGLE_ALLOWED_EMAILS"] == "10"
    assert "ADMIN_PASSWORD_HASH" not in active["secret_versions"]
    commands = deployment["commands"]
    migrate = next(
        i for i, cmd in enumerate(commands) if cmd[:4] == ["gcloud", "run", "jobs", "execute"]
    )
    publish = next(i for i, cmd in enumerate(commands) if cmd[0] == sys.executable)
    assert migrate < publish
    assert all("access" not in command for command in commands)


def test_local_bootstrap_uses_shared_gcs_state(deployment):
    deploy.deploy(bootstrap_only=True)
    backend = json.loads((deploy.BOOTSTRAP / "backend.tf.json").read_text())
    assert backend["terraform"]["backend"]["gcs"] == {
        "bucket": "test-project-climate-control-tfstate",
        "prefix": "bootstrap",
    }
    assert json.loads((deploy.BOOTSTRAP / "bootstrap.auto.tfvars.json").read_text())[
        "site_id"
    ] == "test-site"
    assert any(
        cmd[0] == "terraform" and f"-chdir={deploy.BOOTSTRAP}" in cmd and "apply" in cmd
        for cmd in deployment["commands"]
    )


def test_agent_runtime_failure_keeps_previous_planner_and_resumes_schedules(
    deployment, monkeypatch
):
    existing_runtime(deployment)
    previous = "projects/test-project/locations/asia-southeast1/reasoningEngines/42"
    container = deployment["resources"][0]["values"]["template"][0]["containers"][0]
    container["env"].append({"name": "AGENT_RUNTIME_RESOURCE", "value": previous})
    deployment["platform"]["identities"]["agent"] = "agent@test-project.iam.gserviceaccount.com"
    deployment["platform"]["secret_ids"]["AGENT_SERVICE_TOKEN"] = "agent_service_token"
    base_run = deploy.run

    def failing_adk(command, **kwargs):
        if Path(command[0]).name == "adk":
            raise subprocess.CalledProcessError(1, command)
        return base_run(command, **kwargs)

    monkeypatch.setattr(deploy, "run", failing_adk)
    deploy.deploy()
    _, _, active = deployment["applies"]
    assert not active["scheduler_paused"]
    assert active["agent_runtime_resource"] == previous


def test_settings_ignore_state_lock_messages_around_console_value(deployment):
    deployment["console_output"] = (
        "Acquiring state lock. This may take a few moments...\n"
        "{value}\n"
        "Releasing state lock. This may take a few moments..."
    )
    deployment["settings"] = {"app_url": "https://climate.example.com"}
    deploy.deploy()
    assert deployment["applies"][-1]["app_url"] == "https://climate.example.com"


def test_update_retains_api_and_secret_versions_until_migrations_succeed(deployment):
    old_image = existing_runtime(deployment)
    deploy.deploy()
    staged, api, active = deployment["applies"]
    assert staged["api_enabled"] and staged["api_image"] == old_image
    assert staged["api_secret_versions"]["DATABASE_URL"] == "1"
    assert staged["secret_versions"]["DATABASE_URL"] == "10"
    assert api["api_image"] is None and api["api_secret_versions"] is None
    commands = deployment["commands"]
    pauses = [
        i for i, cmd in enumerate(commands) if cmd[:4] == ["gcloud", "scheduler", "jobs", "pause"]
    ]
    assert {commands[i][4] for i in pauses} == {"climate-control-tick", "climate-control-plan"}
    drain = next(
        i
        for i, cmd in enumerate(commands)
        if cmd[:5] == ["gcloud", "run", "jobs", "executions", "list"]
    )
    assert max(pauses) < drain
    assert {cmd[cmd.index("--job") + 1] for cmd in commands if "--job" in cmd} == {
        "climate-control-worker",
        "climate-control-planner",
        "climate-control-migrate",
    }


def test_retry_does_not_preserve_secrets_from_an_api_that_never_started(deployment):
    existing_runtime(deployment)
    deployment["resources"][0]["values"]["latest_ready_revision"] = None
    deploy.deploy()
    staged, api, active = deployment["applies"]
    assert not staged["api_enabled"]
    assert staged["api_image"] is None and staged["api_secret_versions"] is None
    assert api["api_enabled"] and api["secret_versions"]["ENCRYPTION_KEY"] == "10"
    assert not active["scheduler_paused"]


@pytest.mark.parametrize("failure", ["migration", "firebase", "final-apply"])
def test_failure_keeps_schedule_paused_and_inputs_resumable(deployment, failure):
    old_image = existing_runtime(deployment)
    deployment["failure"] = failure
    with pytest.raises(subprocess.CalledProcessError):
        deploy.deploy()
    variables = json.loads(deploy.VARIABLES.read_text())
    assert variables["scheduler_paused"]
    assert deployment["commands"][-1][:4] == ["gcloud", "scheduler", "jobs", "pause"]
    if failure == "migration":
        assert variables["api_image"] == old_image
        assert len(deployment["applies"]) == 1
        assert all(cmd[0] != sys.executable for cmd in deployment["commands"])


def test_build_failure_does_not_interrupt_current_service(deployment):
    existing_runtime(deployment)
    deployment["failure"] = "build"
    with pytest.raises(subprocess.CalledProcessError):
        deploy.deploy()
    assert not deployment["applies"]
    assert all(
        cmd[:4] != ["gcloud", "scheduler", "jobs", "pause"] for cmd in deployment["commands"]
    )


def test_missing_required_secret_stops_before_building(deployment):
    deployment["missing_secret"] = {"database_url", "encryption_key"}
    with pytest.raises(deploy.DeploymentError, match="DATABASE_URL"):
        deploy.deploy()
    assert "added_secrets" not in deployment
    assert all(cmd[0] not in ("npm", "docker") for cmd in deployment["commands"])


def test_dry_run_only_reads_metadata(deployment, capsys):
    # Upgrading from local state: no GCS state bucket and no bootstrap outputs yet.
    deployment["missing_secret"] = "climate-control-encryption-key"
    deploy.deploy(dry_run=True)
    output_text = capsys.readouterr().out
    assert '"GOOGLE_ALLOWED_EMAILS": "10"' in output_text
    assert '"ENCRYPTION_KEY": "1"' in output_text
    assert "added_secrets" not in deployment
    assert not deploy.VARIABLES.exists()
    assert not deployment["applies"]
    assert not deployment["readiness"]
    assert all(cmd[0] not in ("npm", "docker", sys.executable) for cmd in deployment["commands"])
    assert all("apply" not in cmd for cmd in deployment["commands"])
    assert all(f"-chdir={deploy.BOOTSTRAP}" not in cmd for cmd in deployment["commands"])
    assert ["terraform", f"-chdir={deploy.RUNTIME}", "init", "-input=false"] in deployment[
        "commands"
    ]


def test_incomplete_google_oauth_secrets_stop_before_building(deployment):
    deployment["missing_secret"] = "google_allowed_emails"
    with pytest.raises(deploy.DeploymentError, match="GOOGLE_ALLOWED_EMAILS"):
        deploy.deploy()
    assert all(cmd[0] not in ("npm", "docker") for cmd in deployment["commands"])


def test_worker_drain_times_out_without_cancelling_jobs(deployment, monkeypatch):
    commands = []

    def running(command, **kwargs):
        commands.append(command)
        return json.dumps([{"status": {"runningCount": 1}}])

    monkeypatch.setattr(deploy, "run", running)
    with pytest.raises(deploy.DeploymentError, match="not finished"):
        deploy.drain_job(
            {"name": "climate", "project_id": "test", "region": "region"}, "worker", timeout=0
        )
    assert all("cancel" not in command for command in commands)


def test_agent_deployment_stages_secret_references_not_values(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from scripts import deploy_agent

    # The Agent Platform SDK is an optional extra; pin a placeholder version.
    monkeypatch.setattr(deploy, "version", lambda package: "1.0")

    platform = {
        "project_id": "test-project",
        "secret_ids": {"AGENT_SERVICE_TOKEN": "agent-token", "OPENROUTER_API_KEY": "openrouter"},
    }
    environment = deploy_agent.agent_environment(
        platform, "https://test-site.web.app", "openrouter", ""
    )
    folder = deploy.stage_agent(tmp_path, environment)
    staged = (folder / ".env").read_text()
    assert "CLIMATE_CONTROL_MCP_URL=https://test-site.web.app/mcp" in staged
    assert "OPENROUTER_API_KEY_SECRET=projects/test-project/secrets/openrouter" in staged
    assert "AGENT_MODEL=" not in staged
    assert "google-adk[mcp]==1.0" in (folder / "requirements.txt").read_text()
    assert (folder / "agent.py").exists()

    from climate_control.agent import remote

    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(remote, "secret_value", lambda secret: "secret-from-manager")
    agent = remote.planner_from_environment()
    assert agent.model.model == "openrouter/~google/gemini-flash-latest"
    assert [type(tool).__name__ for tool in agent.tools] == ["McpToolset"]


def test_cloud_build_mode_uses_gcs_state_prebuilt_artifacts_and_state_settings(
    deployment, monkeypatch
):
    (deploy.ROOT / "frontend/dist").mkdir(parents=True)
    (deploy.ROOT / "frontend/dist/index.html").write_text("<html></html>")
    old_image = existing_runtime(deployment)
    deployment["settings"] = {"app_url": None}
    container = deployment["resources"][0]["values"]["template"][0]["containers"][0]
    container["env"].extend(
        [
            {"name": "APP_URL", "value": "https://climate.example.com"},
            {"name": "AGENT_PROVIDER", "value": "openrouter"},
            {"name": "AGENT_MODEL", "value": "custom/model"},
            {
                "name": "AGENT_RUNTIME_RESOURCE",
                "value": "projects/test-project/locations/asia-southeast1/reasoningEngines/42",
            },
        ]
    )
    monkeypatch.setattr(
        deploy.shutil,
        "which",
        lambda name: None if name in ("docker", "npm") else name,
    )
    deploy.deploy(
        skip_build=True,
        project_id="test-project",
        region="asia-southeast1",
        name="climate-control",
        site_id="test-site",
        image=f"{deployment['repository']}:build-123",
    )
    backend = json.loads(deploy.BACKEND.read_text())
    assert backend == {
        "terraform": {
            "backend": {
                "gcs": {
                    "bucket": "test-project-climate-control-tfstate",
                    "prefix": "runtime",
                }
            }
        }
    }
    commands = deployment["commands"]
    assert all(f"-chdir={deploy.BOOTSTRAP}" not in cmd for cmd in commands)
    assert all(cmd[0] not in ("npm", "docker") for cmd in commands)
    assert [
        "terraform",
        f"-chdir={deploy.RUNTIME}",
        "init",
        "-input=false",
        "-reconfigure",
    ] in commands
    staged, api, active = deployment["applies"]
    assert staged["api_image"] == old_image
    assert active["secret_versions"]["GOOGLE_ALLOWED_EMAILS"] == "10"
    assert active["app_url"] == "https://climate.example.com"
    assert active["agent_provider"] == "openrouter"
    assert active["agent_model"] == "custom/model"
    assert (
        active["agent_runtime_resource"]
        == "projects/test-project/locations/asia-southeast1/reasoningEngines/42"
    )
    assert json.loads((deploy.RUNTIME / "agent.auto.tfvars.json").read_text()) == {
        "agent_runtime_resource": (
            "projects/test-project/locations/asia-southeast1/reasoningEngines/42"
        ),
        "agent_provider": "openrouter",
        "agent_model": "custom/model",
    }
    assert not active["scheduler_paused"]


def test_local_agent_tfvars_overrides_state_and_supports_disabled_provider(deployment):
    (deploy.ROOT / "frontend/dist").mkdir(parents=True)
    (deploy.ROOT / "frontend/dist/index.html").write_text("<html></html>")
    existing_runtime(deployment)
    container = deployment["resources"][0]["values"]["template"][0]["containers"][0]
    container["env"].extend(
        [
            {"name": "AGENT_PROVIDER", "value": "openrouter"},
            {"name": "AGENT_MODEL", "value": "openrouter/model"},
            {
                "name": "AGENT_RUNTIME_RESOURCE",
                "value": "projects/test-project/locations/asia-southeast1/reasoningEngines/42",
            },
        ]
    )
    (deploy.RUNTIME / "agent.auto.tfvars.json").write_text(
        json.dumps(
            {
                "agent_provider": "gemini",
                "agent_model": "gemini-pro",
                "agent_runtime_resource": (
                    "projects/test-project/locations/asia-southeast1/reasoningEngines/99"
                ),
            }
        )
    )
    deploy.deploy(
        skip_build=True,
        project_id="test-project",
        region="asia-southeast1",
        name="climate-control",
        site_id="test-site",
        image=f"{deployment['repository']}:build-disabled",
        agent_provider="disabled",
    )
    _, _, active = deployment["applies"]
    assert active["agent_provider"] == "disabled"
    assert "agent_runtime_resource" not in active
    assert "agent_model" not in active
    assert json.loads((deploy.RUNTIME / "agent.auto.tfvars.json").read_text()) == {
        "agent_provider": "disabled",
    }
    assert all(Path(cmd[0]).name != "adk" for cmd in deployment["commands"])


def test_cloud_build_mode_adopts_existing_gcp_resources_when_gcs_state_is_empty(
    deployment, monkeypatch
):
    (deploy.ROOT / "frontend/dist").mkdir(parents=True)
    (deploy.ROOT / "frontend/dist/index.html").write_text("<html></html>")
    old_image = existing_runtime(deployment)
    adopted_resources = list(deployment["resources"])
    deployment["resources"] = []
    base_run = deploy.run

    def run_with_discovery(command, **kwargs):
        if command[:4] == ["gcloud", "run", "jobs", "list"]:
            return json.dumps(
                [{"metadata": {"name": f"climate-control-{k}"}} for k in ("worker", "migrate")]
            )
        if command[:4] == ["gcloud", "run", "services", "list"]:
            return json.dumps([{"metadata": {"name": "climate-control-api"}}])
        if command[:4] == ["gcloud", "scheduler", "jobs", "list"]:
            return json.dumps([{"name": "projects/p/locations/r/jobs/climate-control-tick"}])
        if command[0] == "terraform" and "import" in command:
            deployment["commands"].append(command)
            deployment["resources"] = adopted_resources
            return ""
        return base_run(command, **kwargs)

    monkeypatch.setattr(deploy, "run", run_with_discovery)
    deploy.deploy(
        skip_build=True,
        project_id="test-project",
        region="asia-southeast1",
        name="climate-control",
        site_id="test-site",
        image=f"{deployment['repository']}:build-456",
    )
    imports = [
        (cmd[-2], cmd[-1])
        for cmd in deployment["commands"]
        if cmd[0] == "terraform" and "import" in cmd
    ]
    assert imports == [
        (
            'google_cloud_run_v2_job.job["worker"]',
            "projects/test-project/locations/asia-southeast1/jobs/climate-control-worker",
        ),
        (
            'google_cloud_run_v2_job.job["migrate"]',
            "projects/test-project/locations/asia-southeast1/jobs/climate-control-migrate",
        ),
        (
            "google_cloud_run_v2_service.api[0]",
            "projects/test-project/locations/asia-southeast1/services/climate-control-api",
        ),
        (
            "google_cloud_scheduler_job.tick",
            "projects/test-project/locations/asia-southeast1/jobs/climate-control-tick",
        ),
    ]
    staged, api, active = deployment["applies"]
    assert staged["api_enabled"] and staged["api_image"] == old_image
    assert not active["scheduler_paused"]


def test_cloud_build_bootstrap_creates_state_bucket_and_adopts_existing_resources(
    deployment, monkeypatch
):
    base_run = deploy.run

    def run_bootstrap_commands(command, **kwargs):
        deployment["commands"].append(command)
        if command[:4] == ["gcloud", "storage", "buckets", "list"]:
            return "[]"
        if command[:4] == ["gcloud", "artifacts", "repositories", "list"]:
            return json.dumps([{"name": "projects/p/locations/r/repositories/climate-control"}])
        if command[:4] == ["gcloud", "iam", "service-accounts", "list"]:
            return json.dumps(
                [{"email": "climate-control-api@test-project.iam.gserviceaccount.com"}]
            )
        if command[:3] == ["gcloud", "secrets", "list"]:
            return json.dumps([{"name": "projects/p/secrets/climate-control-database-url"}])
        if command[0] == "terraform" and "show" in command:
            return json.dumps({"values": {"root_module": {"resources": []}}})
        if command[0] == "terraform":
            return ""
        return base_run(command, **kwargs)

    monkeypatch.setattr(deploy, "run", run_bootstrap_commands)
    deploy.deploy(
        bootstrap_only=True,
        project_id="test-project",
        region="asia-southeast1",
        name="climate-control",
        site_id="test-site",
    )
    commands = deployment["commands"]
    assert any(
        cmd[:4] == ["gcloud", "storage", "buckets", "create"]
        and "gs://test-project-climate-control-tfstate" in cmd
        for cmd in commands
    )
    imports = [
        (cmd[-2], cmd[-1])
        for cmd in commands
        if cmd[0] == "terraform" and f"-chdir={deploy.BOOTSTRAP}" in cmd and "import" in cmd
    ]
    assert ("google_storage_bucket.state", "test-project-climate-control-tfstate") in imports
    assert (
        "google_artifact_registry_repository.images",
        "projects/test-project/locations/asia-southeast1/repositories/climate-control",
    ) in imports
    assert (
        'google_service_account.runtime["api"]',
        "projects/test-project/serviceAccounts/climate-control-api@test-project.iam.gserviceaccount.com",
    ) in imports
    assert (
        'google_secret_manager_secret.config["DATABASE_URL"]',
        "projects/test-project/secrets/climate-control-database-url",
    ) in imports
    assert any(
        cmd[0] == "terraform" and f"-chdir={deploy.BOOTSTRAP}" in cmd and "apply" in cmd
        for cmd in commands
    )


def test_first_time_deploy_provisions_internal_secrets_and_deploys_agent_runtime(deployment):
    (deploy.ROOT / "frontend/dist").mkdir(parents=True)
    (deploy.ROOT / "frontend/dist/index.html").write_text("<html></html>")
    deployment["missing_secret"] = {
        "climate-control-encryption-key",
        "climate-control-agent-service-token",
    }
    deploy.deploy(
        skip_build=True,
        project_id="test-project",
        region="asia-southeast1",
        name="climate-control",
        site_id="test-site",
        image=f"{deployment['repository']}:build-first",
    )
    added = deployment["added_secrets"]
    assert set(added) == {
        "climate-control-encryption-key",
        "climate-control-agent-service-token",
    }
    raw_token = added["climate-control-agent-service-token"]
    assert raw_token.startswith("cc_")
    expected_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    commands = deployment["commands"]
    migrate_cmd = next(
        cmd for cmd in commands if cmd[:4] == ["gcloud", "run", "jobs", "execute"]
    )
    assert (
        f"--args=-m,climate_control.cli,migrate,--agent-token-hash,{expected_hash}"
        in migrate_cmd
    )
    adk_cmd = next(cmd for cmd in commands if Path(cmd[0]).name == "adk")
    assert adk_cmd[1:3] == ["deploy", "agent_engine"]
    assert "--agent_engine_id" not in adk_cmd
    _, _, active = deployment["applies"]
    assert active["secret_versions"]["ENCRYPTION_KEY"] == "1"
    assert active["secret_versions"]["AGENT_SERVICE_TOKEN"] == "1"
    assert (
        active["agent_runtime_resource"]
        == "projects/test-project/locations/asia-southeast1/reasoningEngines/1"
    )
    assert active["agent_provider"] == "gemini"
    assert not active["scheduler_paused"]


def test_migrate_ensure_service_token_hash_registers_read_scoped_mcp_token(
    tmp_path, monkeypatch
):
    from climate_control import cli
    from climate_control.agent.access import ensure_service_token_hash, verify_access_token
    from climate_control.db import database
    from climate_control.security import digest

    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("ENCRYPTION_KEY", "c" * 43 + "=")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$test$test")
    raw_token = "cc_test_agent_runtime_secret_token"
    token_hash = digest(raw_token)
    monkeypatch.setattr(
        sys, "argv", ["climate_control", "migrate", "--agent-token-hash", token_hash]
    )
    cli.main()
    engine, sessions = database(db_url)
    try:
        verified = verify_access_token(sessions, raw_token)
        assert verified is not None
        assert verified["name"] == "agent-runtime"
        assert verified["scopes"] == ["read"]
        with sessions() as db, pytest.raises(ValueError, match="SHA-256"):
            ensure_service_token_hash(db, "not-a-valid-hex-digest")
    finally:
        engine.dispose()
