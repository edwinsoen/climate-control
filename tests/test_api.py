import json
import time

import pytest
from conftest import TEST_PASSWORD, account_input
from sqlalchemy import select

from climate_control.api import scheduler_identity, validate_google_identity
from climate_control.db import Account, Device, LoginSession, OAuthFlow
from climate_control.main import create_app
from climate_control.security import digest


async def test_private_routes_and_csrf(client):
    assert (await client.get("/api/accounts")).status_code == 401
    assert (await client.get("/api/devices")).status_code == 401
    assert (await client.get("/api/session")).json()["authenticated"] is False
    login = await client.post("/api/login", json={"username": "admin", "password": TEST_PASSWORD})
    assert login.status_code == 200
    assert "httponly" in login.headers["set-cookie"].lower()
    assert (await client.post("/api/accounts", json=account_input())).status_code == 403
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert (
        await client.post(
            "/api/accounts", json=account_input(), headers={"Origin": "https://evil.example"}
        )
    ).status_code == 403
    assert (await client.post("/api/logout")).status_code == 200
    assert (await client.get("/api/accounts")).status_code == 401


async def test_credentials_and_tokens_are_encrypted(signed_client, app, connected):
    with app.state.sessions() as db:
        account = db.get(Account, connected["id"])
        assert "vesync-secret" not in account.credentials
        assert "provider-secret-token" not in account.tokens
        assert app.state.vault.open(account.credentials)["password"] == "vesync-secret"
    for path in ("/api/accounts", "/api/devices", "/api/session"):
        response = await signed_client.get(path)
        assert response.headers["cache-control"] == "no-store"
        assert "vesync-secret" not in response.text
        assert "provider-secret-token" not in response.text


async def test_two_accounts_with_same_device_id_are_isolated(signed_client, app, connected):
    other = await signed_client.post(
        "/api/accounts", json=account_input("Bedroom", "other@example.com")
    )
    assert other.status_code == 201
    devices = (await signed_client.get("/api/devices")).json()
    assert len(devices) == 2
    assert len({device["id"] for device in devices}) == 2
    assert {device["account_label"] for device in devices} == {"Home", "Bedroom"}
    assert len(app.state.poller.provider.calls) == 2
    # Reading status repeatedly never calls the provider.
    await signed_client.get("/api/devices")
    await signed_client.get("/api/devices")
    assert len(app.state.poller.provider.calls) == 2


async def test_duplicate_and_invalid_account(signed_client, connected):
    assert (await signed_client.post("/api/accounts", json=account_input())).status_code == 409
    invalid = account_input("Another", "invalid@example.com")
    invalid["time_zone"] = "Definitely/NotAZone"
    assert (await signed_client.post("/api/accounts", json=invalid)).status_code == 422
    invalid["password"] = "sensitive-validation-input" * 60
    response = await signed_client.post("/api/accounts", json=invalid)
    assert "sensitive-validation-input" not in response.text


async def test_account_provider_selection(signed_client, app):
    missing = account_input()
    del missing["provider"]
    assert (await signed_client.post("/api/accounts", json=missing)).status_code == 422
    payload = {**account_input(), "provider": "unsupported"}
    assert (await signed_client.post("/api/accounts", json=payload)).status_code == 422
    assert not app.state.poller.provider.calls
    payload["provider"] = "vesync"
    response = await signed_client.post("/api/accounts", json=payload)
    assert response.status_code == 201
    assert response.json()["provider"] == "vesync"


async def test_refresh_cooldown_and_token_reuse(signed_client, app, connected):
    response = await signed_client.post(f"/api/accounts/{connected['id']}/refresh")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert len(app.state.poller.provider.calls) == 1
    with app.state.sessions() as db:
        db.get(Account, connected["id"]).next_poll_at = 0
        db.commit()
    assert (await signed_client.post(f"/api/accounts/{connected['id']}/refresh")).status_code == 200
    assert app.state.poller.provider.calls[-1][1]["token"] == "provider-secret-token"


async def test_remove_cascades_credentials_and_devices(signed_client, app, connected):
    assert (await signed_client.delete(f"/api/accounts/{connected['id']}")).status_code == 200
    assert (await signed_client.get("/api/devices")).json() == []
    with app.state.sessions() as db:
        assert db.get(Account, connected["id"]) is None
        assert list(db.scalars(select(Device))) == []


async def test_auth_error_suspends_until_reconnect(signed_client, app):
    response = await signed_client.post("/api/accounts", json=account_input(password="wrong"))
    account = response.json()
    assert account["status"] == "auth_error"
    assert (await app.state.poller.tick())["checked"] == 0
    assert (await signed_client.post(f"/api/accounts/{account['id']}/refresh")).status_code == 409
    response = await signed_client.put(f"/api/accounts/{account['id']}", json=account_input())
    assert response.status_code == 200
    with app.state.sessions() as db:
        db.get(Account, account["id"]).next_poll_at = 0
        db.commit()
    await app.state.poller.tick()
    assert (await signed_client.get("/api/accounts")).json()[0]["status"] == "connected"


async def test_stale_status_and_restart_persistence(signed_client, app, connected):
    with app.state.sessions() as db:
        device = db.scalars(select(Device)).one()
        device.retrieved_at = time.time() - 181
        db.commit()
    device = (await signed_client.get("/api/devices")).json()[0]
    assert device["stale"] is True
    assert device["state"]["pm25"] == 8
    restarted = create_app(app.state.settings, app.state.poller.provider)
    with restarted.state.sessions() as db:
        assert db.get(Account, connected["id"]).provider == "vesync"
        assert db.scalars(select(Device)).one().state["pm25"] == 8
    restarted.state.engine.dispose()


async def test_sessions_expire(client, signed_client, app):
    with app.state.sessions() as db:
        session = db.get(LoginSession, digest(client.cookies["__session"]))
        session.expires_at = time.time() - 1
        db.commit()
    assert (await client.get("/api/accounts")).status_code == 401


async def test_login_throttling(client):
    for _ in range(5):
        assert (
            await client.post("/api/login", json={"username": "admin", "password": "incorrect"})
        ).status_code == 401
    assert (
        await client.post("/api/login", json={"username": "admin", "password": "incorrect"})
    ).status_code == 429


async def test_google_flow_state_binding(client, app):
    settings = app.state.settings
    settings.google_client_id = "google-client"
    settings.google_client_secret = "google-secret"
    settings.google_allowed_emails = "owner@example.com"
    response = await client.get("/auth/google/login")
    assert response.status_code == 302
    assert "code_challenge=" in response.headers["location"]
    with app.state.sessions() as db:
        flow = db.scalars(select(OAuthFlow)).one()
        state = flow.state
    client.cookies.clear()
    assert (await client.get(f"/auth/google/callback?state={state}&code=code")).status_code == 400
    assert (await client.get("/auth/google/callback?state=unknown&code=code")).status_code == 400


@pytest.mark.parametrize(
    "change",
    [
        {"email": "other@example.com"},
        {"email_verified": False},
        {"nonce": "wrong"},
        {"sub": ""},
    ],
)
def test_google_rejects_unapproved_identity(change):
    claims = {"email": "owner@example.com", "email_verified": True, "nonce": "nonce", "sub": "123"}
    with pytest.raises(ValueError):
        validate_google_identity({**claims, **change}, "nonce", {"owner@example.com"})


def test_google_allowed_identity():
    assert (
        validate_google_identity(
            {"email": "owner@example.com", "email_verified": True, "nonce": "nonce", "sub": "123"},
            "nonce",
            {"owner@example.com"},
        )
        == "owner@example.com"
    )


async def test_scheduler_identity(client, app, monkeypatch):
    assert (await client.post("/internal/poll")).status_code == 401
    from climate_control import api

    monkeypatch.setattr(
        api.id_token,
        "verify_oauth2_token",
        lambda token, request, audience: {
            "email": app.state.settings.scheduler_service_account,
            "email_verified": True,
        },
    )
    response = await client.post(
        "/internal/poll", headers={"Authorization": "Bearer valid-test-token"}
    )
    assert response.status_code == 200
    assert response.json()["checked"] == 0
    with pytest.raises(ValueError):
        scheduler_identity(
            {"email": "stranger@example.com", "email_verified": True},
            app.state.settings.scheduler_service_account,
        )


async def test_health(client):
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 200
    assert (await client.get("/api/missing")).status_code == 404


async def test_error_redaction(signed_client, app, caplog):
    app.state.poller.provider.results.append(
        RuntimeError("vesync-secret and provider-secret-token")
    )
    response = await signed_client.post("/api/accounts", json=account_input())
    assert response.json()["status"] == "unavailable"
    assert "vesync-secret" not in json.dumps(response.json())
    assert "vesync-secret" not in caplog.text


async def test_google_callback_creates_session_and_consumes_flow(client, app, monkeypatch):
    from climate_control import api

    app.state.settings.google_client_id = "google-client"
    app.state.settings.google_client_secret = "google-secret"
    app.state.settings.google_allowed_emails = "owner@example.com"
    await client.get("/auth/google/login")
    with app.state.sessions() as db:
        flow = db.scalars(select(OAuthFlow)).one()
        state, nonce, verifier = flow.state, flow.payload["nonce"], flow.payload["verifier"]

    class OAuthClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fetch_token(self, endpoint, **kwargs):
            assert kwargs["code_verifier"] == verifier
            assert kwargs["code"] == "authorization-code"
            return {"id_token": "verified-test-token"}

    def verify_token(token, request, audience):
        assert audience == "google-client"
        assert token == "verified-test-token"
        return {
            "email": "owner@example.com",
            "email_verified": True,
            "nonce": nonce,
            "sub": "google-user-id",
        }

    monkeypatch.setattr(api, "AsyncOAuth2Client", OAuthClient)
    monkeypatch.setattr(api.id_token, "verify_oauth2_token", verify_token)
    response = await client.get(f"/auth/google/callback?state={state}&code=authorization-code")
    assert response.status_code == 303
    assert (await client.get("/api/session")).json()["identity"] == "owner@example.com"
    response = await client.get(f"/auth/google/callback?state={state}&code=authorization-code")
    assert response.status_code == 400
