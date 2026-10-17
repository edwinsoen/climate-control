import os
import time
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from climate_control.config import Settings
from climate_control.db import Account
from climate_control.main import create_app
from climate_control.providers.base import DeviceSnapshot, PollResult, ProviderError
from climate_control.security import password_hasher

TEST_PASSWORD = "test-admin-password"
TEST_HASH = password_hasher.hash(TEST_PASSWORD)


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.results = []
        self.control_calls = []
        self.control_results = []
        self.status_calls = []
        self.states = {}

    async def poll(self, credentials, tokens):
        self.calls.append((credentials, tokens))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if credentials["password"] == "wrong":
            raise ProviderError("auth")
        return self.snapshot(credentials)

    def _key(self, credentials) -> str:
        provider = credentials.get("provider", "vesync")
        if provider == "vesync":
            return credentials["email"]
        country = credentials.get("country_code", "")
        return f"{provider}:{credentials['email']}:{country}"

    def _state(self, credentials) -> dict:
        key = self._key(credentials)
        return {
            **self.states.get(credentials["email"], {}),
            **self.states.get(key, {}),
        }

    def snapshot(self, credentials):
        if credentials.get("provider") == "xiaomi":
            return PollResult(
                [
                    DeviceSnapshot(
                        provider_id="xiaomi-provider-id",
                        name=f"{credentials['label']} purifier",
                        model="Smart Air Purifier 4 Pro",
                        capabilities=[
                            "power",
                            "mode",
                            "fan_level",
                            "pm25",
                            "pm10",
                            "temperature",
                            "humidity",
                            "filter_life",
                            "air_quality",
                            "display",
                        ],
                        controls={
                            "power": True,
                            "modes": ["auto", "sleep"],
                            "fan_levels": list(range(1, 15)),
                            "display": True,
                            "display_states": ["bright", "dim", "off"],
                            "display_modes": [
                                "manual",
                                "warnings_only_bright",
                                "warnings_only_dim",
                            ],
                        },
                        state={
                            "connectivity": "online",
                            "power": "on",
                            "mode": "manual",
                            "fan_level": 6,
                            "reported_fan_level": 6,
                            "pm25": 11,
                            "pm10": 24,
                            "temperature": 24.5,
                            "humidity": 55,
                            "filter_life": 92,
                            "air_quality": "excellent",
                            "display": "bright",
                            **self._state(credentials),
                        },
                        retrieved_at=time.time(),
                    ),
                ],
                {
                    "user_id": "xiaomi-user",
                    "ssecurity": "xiaomi-ssec",
                    "service_token": "provider-secret-token",
                    "country_code": credentials.get("country_code", "SG"),
                },
            )
        return PollResult(
            [
                DeviceSnapshot(
                    provider_id="same-provider-id",
                    name=f"{credentials['label']} purifier",
                    model="Core 400S",
                    capabilities=[
                        "power",
                        "mode",
                        "fan_level",
                        "pm25",
                        "filter_life",
                        "air_quality",
                        "display",
                    ],
                    controls={
                        "power": True,
                        "modes": ["auto", "sleep"],
                        "fan_levels": [1, 2, 3, 4],
                        "display": True,
                        "display_modes": ["manual", "warnings_only"],
                    },
                    state={
                        "connectivity": "online",
                        "power": "on",
                        "mode": "manual",
                        "fan_level": 2,
                        "pm25": 8,
                        "filter_life": 86,
                        "air_quality": "good",
                        "display": "on",
                        **self._state(credentials),
                    },
                    retrieved_at=time.time(),
                ),
            ],
            {
                "token": "provider-secret-token",
                "account_id": "vendor-account",
                "country_code": "US",
                "current_region": "US",
            },
        )

    async def control(self, credentials, tokens, provider_id, command):
        self.control_calls.append((credentials, tokens, provider_id, command))
        if self.control_results:
            result = self.control_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        key = self._key(credentials)
        state = self.states.setdefault(key, {})
        if command.power is not None:
            state["power"] = "on" if command.power else "off"
        if command.mode is not None:
            state["mode"] = command.mode
        if command.fan_level is not None:
            state.update(mode="manual", fan_level=command.fan_level)
        if command.display is not None:
            state["display"] = (
                ("on" if command.display else "off")
                if isinstance(command.display, bool)
                else command.display
            )
        return self.snapshot(credentials)

    async def status(self, credentials, tokens, provider_id):
        self.status_calls.append((credentials, tokens, provider_id))
        return self.snapshot(credentials)


@pytest.fixture(params=["sqlite"] + (["postgres"] if os.getenv("TEST_POSTGRES_URL") else []))
def app(request, tmp_path):
    postgres = None
    schema = None
    if request.param == "postgres":
        url = make_url(os.environ["TEST_POSTGRES_URL"])
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg")
        postgres = create_engine(url)
        schema = "cc_test_" + uuid4().hex
        with postgres.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        database_url = url.update_query_dict(
            {"options": f"-csearch_path={schema}"}
        ).render_as_string(hide_password=False)
    else:
        database_url = f"sqlite:///{tmp_path / 'test.db'}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        encryption_key=Fernet.generate_key().decode(),
        admin_password_hash=TEST_HASH,
        scheduler_enabled=False,
        scheduler_service_account="scheduler@example.iam.gserviceaccount.com",
        scheduler_audience="https://climate.example.com",
        aqicn_api_token="",
        agent_provider="disabled",
    )
    application = create_app(settings, FakeProvider())
    yield application
    application.state.engine.dispose()
    if postgres is not None:
        with postgres.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        postgres.dispose()


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        yield client


@pytest.fixture
async def signed_client(client):
    response = await client.post(
        "/api/login", json={"username": "admin", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


def account_input(label="Home", email="home@example.com", password="vesync-secret"):
    return {
        "provider": "vesync",
        "label": label,
        "email": email,
        "password": password,
        "country_code": "SG",
        "time_zone": "Asia/Singapore",
    }


@pytest.fixture
async def connected(signed_client):
    response = await signed_client.post("/api/accounts", json=account_input())
    assert response.status_code == 201
    return response.json()


@pytest.fixture
async def controlled_device(app, connected, signed_client):
    with app.state.sessions() as db:
        account = db.get(Account, connected["id"])
        account.next_control_at = 0
        db.commit()
    return (await signed_client.get("/api/devices")).json()[0]
