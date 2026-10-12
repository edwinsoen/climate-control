"""Release checks use process stubs; never publish a real Firebase release."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_firebase",
    Path(__file__).resolve().parents[1] / "scripts/release_firebase.py",
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def release_environment(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "frontend/dist").mkdir(parents=True)
    (root / "LICENSE").write_text("test-license")
    monkeypatch.setattr(release, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["release_firebase.py"])
    outputs = {
        "api_url": "https://api.example.run.app",
        "project_id": "test-project",
        "firebase_config": {"hosting": {"site": "test-site", "public": "frontend/dist"}},
    }
    monkeypatch.setattr(release, "terraform_output", lambda directory, name: outputs[name])
    return root, outputs


def test_release_refuses_disabled_api_and_failed_build(release_environment, monkeypatch):
    root, outputs = release_environment
    outputs["api_url"] = None
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(SystemExit):
        release.main()
    assert not commands
    outputs["api_url"] = "https://api.example.run.app"
    with pytest.raises(subprocess.CalledProcessError):
        release.main()
    assert all(command[0] != "firebase" for command in commands)


def test_release_targets_configured_project_and_packages_license(release_environment, monkeypatch):
    root, outputs = release_environment
    published = []

    def run(command, **kwargs):
        if command[0] != "firebase":
            return
        assert command[command.index("--project") + 1] == "test-project"
        config = json.loads(Path(command[command.index("--config") + 1]).read_text())
        assert config["hosting"]["site"] == "test-site"
        public = Path(config["hosting"]["public"])
        assert public == root / "frontend/dist"
        assert (public / "LICENSE").read_text() == "test-license"
        published.append(config)

    monkeypatch.setattr(release.subprocess, "run", run)
    release.main()
    assert len(published) == 1


def test_prebuilt_release_requires_ui_and_does_not_rebuild(release_environment, monkeypatch):
    root, _ = release_environment
    monkeypatch.setattr("sys.argv", ["release_firebase.py", "--skip-build"])
    commands = []
    monkeypatch.setattr(
        release.subprocess, "run", lambda command, **kwargs: commands.append(command)
    )
    with pytest.raises(SystemExit):
        release.main()
    assert not commands
    (root / "frontend/dist/index.html").write_text("<html></html>")
    release.main()
    assert len(commands) == 1 and commands[0][0] == "firebase"
    assert (root / "frontend/dist/LICENSE").is_file()
