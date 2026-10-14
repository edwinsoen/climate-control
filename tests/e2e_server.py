"""Isolated browser-test server. Never connects to VeSync or uses deployment secrets."""

import tempfile
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from conftest import TEST_HASH, FakeProvider
from cryptography.fernet import Fernet
from test_outdoor import FakeOutdoor

from climate_control.config import Settings
from climate_control.main import create_app

with tempfile.TemporaryDirectory(prefix="climate-e2e-") as directory:
    url = f"sqlite:///{directory}/climate.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    settings = Settings(
        _env_file=None,
        database_url=url,
        encryption_key=Fernet.generate_key().decode(),
        admin_password_hash=TEST_HASH,
        app_url="http://127.0.0.1:8091",
        frontend_dir=Path("frontend/dist"),
        aqicn_api_token="e2e-fake-key",
        # Browser tests never call model APIs, whatever the developer environment holds.
        agent_provider="disabled",
    )
    app = create_app(settings, FakeProvider(), outdoor_provider=FakeOutdoor())

    @app.middleware("http")
    async def isolate_test_client(request, call_next):
        # Each browser test has its own login throttle, like independent real clients.
        # This header is recognized only by this isolated test server.
        if client_id := request.headers.get("X-Test-Client"):
            request.scope["client"] = (client_id, 0)
        return await call_next(request)

    uvicorn.run(app, host="127.0.0.1", port=8091, access_log=False)
