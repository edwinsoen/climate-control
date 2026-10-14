"""Exercise deployment sequencing with process stubs; never modify GCP resources."""

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
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "BOOTSTRAP", bootstrap)
    monkeypatch.setattr(deploy, "RUNTIME", runtime)
    monkeypatch.setattr(deploy, "VARIABLES", runtime / "deployment.auto.tfvars.json")
    monkeypatch.setattr(deploy.shutil, "which", lambda name: name)
    monkeypatch.setattr(deploy, "dotenv_values", lambda path: {})
    monkeypatch.delenv("GOOGLE_ALLOWED_EMAILS", raising=False)
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
        "settings": {"google_allowed_emails": "owner@example.com", "app_url": None},
        "missing_secret": None,
        "readiness": [],
        "repository": repository,
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
                return json.dumps(json.dumps(session["settings"]))
            if "apply" in command and command[1] == f"-chdir={runtime}":
                variables = json.loads(deploy.VARIABLES.read_text())
                session["applies"].append(variables)
                if session["failure"] == "final-apply" and not variables["scheduler_paused"]:
                    raise subprocess.CalledProcessError(1, command)
        if command[:4] == ["gcloud", "secrets", "versions", "list"]:
            if command[4] in ("admin_password_hash", session["missing_secret"]):
                return "[]"
            return json.dumps(
                [
                    {"name": f"{command[4]}/versions/{version}", "state": state}
                    for version, state in [(9, "ENABLED"), (11, "DISABLED"), (10, "ENABLED")]
                ]
            )
        if command[:5] == ["gcloud", "artifacts", "docker", "images", "describe"]:
            return repository + "@sha256:" + "a" * 64
        if command[:5] == ["gcloud", "run", "jobs", "executions", "list"]:
            return json.dumps(
                [{"status": {"conditions": [{"type": "Completed", "status": "False"}]}}]
            )
        if command[:4] == ["gcloud", "run", "jobs", "execute"]:
            if session["failure"] == "migration":
                raise subprocess.CalledProcessError(1, command)
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
    assert "ADMIN_PASSWORD_HASH" not in active["secret_versions"]
    commands = deployment["commands"]
    migrate = next(
        i for i, cmd in enumerate(commands) if cmd[:4] == ["gcloud", "run", "jobs", "execute"]
    )
    publish = next(i for i, cmd in enumerate(commands) if cmd[0] == sys.executable)
    assert migrate < publish
    assert all("access" not in command for command in commands)


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
    deployment["missing_secret"] = "database_url"
    with pytest.raises(deploy.DeploymentError, match="DATABASE_URL"):
        deploy.deploy()
    assert all(cmd[0] not in ("npm", "docker") for cmd in deployment["commands"])


def test_dry_run_only_reads_metadata(deployment, capsys):
    deployment["settings"]["google_allowed_emails"] = ""
    deploy.deploy(dry_run=True)
    assert '"email_prompt_needed": true' in capsys.readouterr().out
    assert not deploy.VARIABLES.exists()
    assert not deployment["applies"]
    assert not deployment["readiness"]
    assert all(cmd[0] not in ("npm", "docker", sys.executable) for cmd in deployment["commands"])
    assert all("apply" not in cmd for cmd in deployment["commands"])


def test_first_run_prompts_once_and_saves_allowlist(deployment, monkeypatch):
    deployment["settings"]["google_allowed_emails"] = ""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return "Owner@Example.com,owner@example.com"

    monkeypatch.setattr("builtins.input", answer)
    deploy.deploy()
    assert len(prompts) == 1
    saved = json.loads(deploy.VARIABLES.read_text())
    assert saved["google_allowed_emails"] == "owner@example.com"
    deployment["settings"]["google_allowed_emails"] = saved["google_allowed_emails"]
    deploy.deploy()
    assert len(prompts) == 1


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
    monkeypatch.setattr(deploy_agent, "version", lambda package: "1.0")

    platform = {
        "project_id": "test-project",
        "secret_ids": {"AGENT_SERVICE_TOKEN": "agent-token", "OPENROUTER_API_KEY": "openrouter"},
    }
    environment = deploy_agent.agent_environment(
        platform, "https://test-site.web.app", "openrouter", ""
    )
    folder = deploy_agent.stage(tmp_path, environment)
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
