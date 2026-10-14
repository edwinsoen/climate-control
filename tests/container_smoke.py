"""Run inside the built image to verify migration, serving, login, and restart persistence."""

import os
import re
import subprocess
import time

import requests
from argon2 import PasswordHasher
from cryptography.fernet import Fernet

assert os.getuid() != 0, "The container must run as a non-root user"
environment = {
    **os.environ,
    "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "ADMIN_PASSWORD_HASH": PasswordHasher().hash("container-smoke-password"),
    "ADMIN_USERNAME": "admin",
    "APP_URL": "http://127.0.0.1:8080",
    "DATABASE_URL": "sqlite:////app/data/smoke.db",
    "AUTO_MIGRATE": "true",
    "SCHEDULER_ENABLED": "true",
    "AQICN_API_TOKEN": "",
}
api_only = os.environ.get("SMOKE_API_ONLY") == "true"
browser = requests.Session()

# Neither migrations nor worker ticks need web login configuration.
worker_environment = {
    **environment,
    "ADMIN_PASSWORD_HASH": "",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "GOOGLE_ALLOWED_EMAILS": "",
}
subprocess.run(
    ["python", "-m", "climate_control.cli", "migrate"],
    env={**worker_environment, "ENCRYPTION_KEY": ""},
    check=True,
)
subprocess.run(["python", "-m", "climate_control.cli", "tick"], env=worker_environment, check=True)
# Planning needs only the database; it starts without encryption or provider secrets.
subprocess.run(
    ["python", "-m", "climate_control.cli", "plan"],
    env={**worker_environment, "ENCRYPTION_KEY": "", "AGENT_PROVIDER": "disabled"},
    check=True,
)
subprocess.run(["python", "-c", "import agentplatform, climate_control.agent.remote"], check=True)


def start():
    process = subprocess.Popen(
        ["python", "-m", "climate_control.cli", "serve-api" if api_only else "serve"],
        env=environment,
    )
    for _ in range(80):
        if process.poll() is not None:
            raise RuntimeError("Container service exited during startup")
        try:
            if browser.get("http://127.0.0.1:8080/health/ready", timeout=1).status_code == 200:
                return process
        except requests.RequestException:
            pass
        time.sleep(0.25)
    process.terminate()
    raise RuntimeError("Container service did not become ready")


def stop(process):
    process.terminate()
    process.wait(timeout=10)


server = start()
try:
    page = browser.get("http://127.0.0.1:8080/", timeout=5)
    if api_only:
        assert page.status_code == 404
        assert browser.post("http://127.0.0.1:8080/internal/poll", timeout=5).status_code == 404
    else:
        assert page.status_code == 200
        asset = re.search(r'src="(/assets/[^\"]+)"', page.text)
        assert asset is not None
        assert browser.get("http://127.0.0.1:8080" + asset.group(1), timeout=5).status_code == 200
    assert browser.get("http://127.0.0.1:8080/api/devices", timeout=5).status_code == 401
    login = browser.post(
        "http://127.0.0.1:8080/api/login",
        timeout=5,
        json={"username": "admin", "password": "container-smoke-password"},
    )
    assert login.status_code == 200
    assert browser.get("http://127.0.0.1:8080/api/accounts", timeout=5).json() == []
finally:
    stop(server)

server = start()
try:
    assert (
        browser.get("http://127.0.0.1:8080/api/session", timeout=5).json()["authenticated"] is True
    )
finally:
    stop(server)
print(
    "Container smoke test passed: migrations, worker tick, planner, web mode, login, and restart."
)
