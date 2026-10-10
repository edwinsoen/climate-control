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
}
browser = requests.Session()


def start():
    process = subprocess.Popen(["python", "-m", "climate_control.cli", "serve"], env=environment)
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
print("Container smoke test passed: non-root runtime, migrations, UI, authentication, and restart.")
