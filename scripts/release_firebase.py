"""Build and publish the UI using the Firebase config produced by Terraform."""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def terraform_output(directory: Path, name: str):
    return json.loads(
        subprocess.check_output(
            ["terraform", f"-chdir={directory}", "output", "-json", name],
            text=True,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / "infra/gcp/runtime")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print config without building or deploying"
    )
    parser.add_argument("--skip-build", action="store_true", help="Publish an already built UI")
    args = parser.parse_args()
    directory = args.runtime_dir.resolve()
    if not terraform_output(directory, "api_url"):
        parser.error("Enable the API after successful migrations before publishing Hosting.")
    config = terraform_output(directory, "firebase_config")
    project = terraform_output(directory, "project_id")
    config["hosting"]["public"] = str(ROOT / "frontend/dist")
    if args.dry_run:
        print(json.dumps(config, indent=2))
        return
    if args.skip_build:
        if not (ROOT / "frontend/dist/index.html").is_file():
            parser.error("The compiled UI is missing. Build it before publishing.")
    else:
        subprocess.run(["npm", "ci"], cwd=ROOT / "frontend", check=True)
        subprocess.run(["npm", "run", "build"], cwd=ROOT / "frontend", check=True)
    shutil.copyfile(ROOT / "LICENSE", ROOT / "frontend/dist/LICENSE")
    with tempfile.TemporaryDirectory(prefix="climate-firebase-") as temporary:
        path = Path(temporary) / "firebase.json"
        path.write_text(json.dumps(config, indent=2) + "\n")
        subprocess.run(
            [
                "firebase",
                "deploy",
                "--only",
                "hosting",
                "--project",
                project,
                "--config",
                str(path),
                "--non-interactive",
            ],
            cwd=ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
