import asyncio
import time

import httpx
import pytest
from conftest import TEST_PASSWORD
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_automation import setup_rules
from test_outdoor import FakeOutdoor

from climate_control import cli
from climate_control.config import DatabaseSettings, Settings, WorkerSettings
from climate_control.db import (
    Account,
    Automation,
    Device,
    LoginSession,
    LoginThrottle,
    OAuthFlow,
    OutdoorConfiguration,
)
from climate_control.main import create_api_app
from climate_control.runtime import create_runtime, run_tick
from climate_control.security import digest


class FirebaseTransport(httpx.ASGITransport):
    async def handle_async_request(self, request):
        cookies = request.headers.get("cookie", "").split(";")
        request.headers["cookie"] = ";".join(
            cookie for cookie in cookies if cookie.strip().startswith("__session=")
        )
        return await super().handle_async_request(request)


async def test_api_mode_does_not_collect_or_serve_ui(app):
    app.state.settings.aqicn_api_token = SecretStr("fake-token")
    api = create_api_app(app.state.settings, app.state.poller.provider, FakeOutdoor())
    async with api.router.lifespan_context(api):
        async with httpx.AsyncClient(
            transport=FirebaseTransport(app=api), base_url="http://localhost:8080"
        ) as client:
            assert (await client.get("/")).status_code == 404
            assert (await client.post("/internal/poll")).status_code == 404
            login = await client.post(
                "/api/login", json={"username": "admin", "password": TEST_PASSWORD}
            )
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            assert (await client.get("/api/accounts")).status_code == 200
            assert (
                await client.post("/api/outdoor/stations/search", json={"query": "test"})
            ).status_code == 200
            assert (
                await client.put("/api/outdoor/location", json={"location_id": 1})
            ).status_code == 200
            assert api.state.outdoor.provider.calls == [("search", "test")]
            assert not api.state.poller.provider.calls
            assert not hasattr(api.state.poller, "automation")
            assert (await client.post("/api/logout")).status_code == 200
            assert (await client.get("/api/accounts")).status_code == 401


async def test_tick_collects_applies_rules_and_cleans_expired_records(
    app,
    signed_client,
    controlled_device,
):
    await setup_rules(signed_client, controlled_device)
    with app.state.sessions() as db:
        account = db.get(Account, controlled_device["account_id"])
        account.next_poll_at = account.next_control_at = 0
        db.add(
            LoginSession(
                token_hash=digest("expired"),
                identity="expired",
                csrf_token="expired",
                expires_at=0,
            )
        )
        db.add(OAuthFlow(state="expired", browser_hash="expired", payload={}, expires_at=0))
        db.add(LoginThrottle(key="expired", expires_at=0))
        db.commit()
    settings = WorkerSettings(
        _env_file=None,
        database_url=app.state.settings.database_url,
        encryption_key=app.state.settings.encryption_key,
        aqicn_api_token="fake-token",
    )
    with app.state.sessions() as db:
        from climate_control.outdoor import set_location

        db.get(OutdoorConfiguration, 1).discovery = {
            "stations": [{"location_id": 1, **FakeOutdoor().details(1)}],
        }
        set_location(db, 1)
    outdoor = FakeOutdoor()
    result = await run_tick(settings, app.state.poller.provider, outdoor)
    assert result == {
        "checked": 1,
        "refreshed": 1,
        "outdoor_checked": 1,
        "automation_checked": 1,
    }
    assert outdoor.calls == [("feed", 1)]
    with app.state.sessions() as db:
        assert db.get(Device, controlled_device["id"]).state["fan_level"] == 3
        assert db.get(LoginSession, digest("expired")) is None
        assert db.get(OAuthFlow, "expired") is None
        assert db.get(LoginThrottle, "expired") is None


async def test_api_refresh_and_separate_worker_share_account_lease(app, connected, monkeypatch):
    provider = app.state.poller.provider
    started, release = asyncio.Event(), asyncio.Event()
    original = provider.poll

    async def slow_poll(*args):
        started.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(provider, "poll", slow_poll)
    with app.state.sessions() as db:
        db.get(Account, connected["id"]).next_poll_at = 0
        db.commit()
    runtime = create_runtime(app.state.settings, provider, FakeOutdoor())
    try:
        refresh = asyncio.create_task(app.state.poller.refresh(connected["id"]))
        await started.wait()
        result = await runtime.poller.tick()
        assert result["checked"] == 0
        release.set()
        assert (await refresh).status == "refreshed"
        assert len(provider.calls) == 2  # Initial connection and the single claimed refresh.
    finally:
        release.set()
        runtime.engine.dispose()


async def test_manual_timeout_preserves_cached_state_and_pause(
    app,
    signed_client,
    controlled_device,
    monkeypatch,
):
    await setup_rules(signed_client, controlled_device)
    app.state.settings.api_operation_timeout_seconds = 0.01
    with app.state.sessions() as db:
        before = db.get(Device, controlled_device["id"])
        state, retrieved = dict(before.state), before.retrieved_at

    async def slow_control(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.poller.provider, "control", slow_control)
    response = await signed_client.post(
        f"/api/devices/{controlled_device['id']}/control",
        json={"fan_level": 4},
    )
    assert response.status_code == 504
    with app.state.sessions() as db:
        device = db.get(Device, controlled_device["id"])
        assert device.state == state and device.retrieved_at == retrieved
        account = db.get(Account, device.account_id)
        assert account.lease_token is None and account.next_poll_at > time.time()
        assert db.get(Automation, device.id).paused_until > time.time()


@pytest.mark.parametrize("logged_in", [False, True])
async def test_oauth_failure_through_firebase_preserves_existing_session(app, logged_in):
    settings = app.state.settings
    settings.google_client_id = "fake-client"
    settings.google_client_secret = "fake-secret"
    settings.google_allowed_emails = "owner@example.com"
    async with httpx.AsyncClient(
        transport=FirebaseTransport(app=app),
        base_url="http://localhost:8080",
    ) as client:
        if logged_in:
            await client.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})
        await client.get("/auth/google/login")
        assert (await client.get("/api/session")).json()["authenticated"] is logged_in
        with app.state.sessions() as db:
            flow = db.scalars(select(OAuthFlow)).one()
            assert flow.browser_hash == digest(client.cookies["__session"])
            state = flow.state
        assert (await client.get(f"/auth/google/callback?state={state}")).status_code == 303
        assert (await client.get("/api/session")).json()["authenticated"] is logged_in
        assert (await client.get(f"/auth/google/callback?state={state}")).status_code == 400


def test_worker_and_migration_configuration_do_not_require_web_credentials(app):
    assert DatabaseSettings(_env_file=None).database_url
    assert WorkerSettings(
        _env_file=None,
        encryption_key=app.state.settings.encryption_key,
    ).encryption_key
    Settings(
        _env_file=None,
        encryption_key=app.state.settings.encryption_key,
        admin_password_hash=app.state.settings.admin_password_hash,
        scheduler_enabled=False,
    )


def test_tick_failure_is_sanitized_and_exits_nonzero(monkeypatch, caplog):
    async def fail(settings):
        raise RuntimeError("postgresql://secret-password")

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(cli, "run_tick", fail)
    monkeypatch.setattr("sys.argv", ["climate", "tick"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    assert "worker_tick_failed" in caplog.text
    assert "secret-password" not in caplog.text


async def test_google_success_through_firebase_rotates_cookie_and_enforces_csrf(app, monkeypatch):
    from climate_control import api

    settings = app.state.settings
    settings.google_client_id = "fake-client"
    settings.google_client_secret = "fake-secret"
    settings.google_allowed_emails = "owner@example.com"
    async with httpx.AsyncClient(
        transport=FirebaseTransport(app=app),
        base_url="http://localhost:8080",
    ) as client:
        await client.get("/auth/google/login")
        temporary = client.cookies["__session"]
        with app.state.sessions() as db:
            flow = db.scalars(select(OAuthFlow)).one()
            state, nonce = flow.state, flow.payload["nonce"]

        class OAuthClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def fetch_token(self, *args, **kwargs):
                return {"id_token": "fake-token"}

        monkeypatch.setattr(api, "AsyncOAuth2Client", OAuthClient)
        monkeypatch.setattr(
            api.id_token,
            "verify_oauth2_token",
            lambda *args: {
                "email": "owner@example.com",
                "email_verified": True,
                "sub": "fake-user",
                "nonce": nonce,
            },
        )
        assert (
            await client.get(f"/auth/google/callback?state={state}&code=fake")
        ).status_code == 303
        assert client.cookies["__session"] != temporary
        session = (await client.get("/api/session")).json()
        assert session["identity"] == "owner@example.com"
        assert (await client.post("/api/logout")).status_code == 403
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        assert (
            await client.post(
                "/api/logout",
                headers={"Origin": "https://untrusted.example"},
            )
        ).status_code == 403
        assert (
            await client.get(f"/auth/google/callback?state={state}&code=fake")
        ).status_code == 400
        assert (await client.post("/api/logout")).status_code == 200
        assert (await client.get("/api/accounts")).status_code == 401
