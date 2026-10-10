from climate_control.db import Account
from climate_control.providers.base import PollResult


async def test_unconfirmed_control_returns_explicit_flag_and_target_status_read(
    app, signed_client, controlled_device
):
    provider = app.state.poller.provider
    credentials = {"label": "Home", "email": "home@example.com"}
    stale = provider.snapshot(credentials)
    provider.control_results.append(
        PollResult(stale.devices, stale.tokens, control_confirmed=False)
    )
    response = await signed_client.post(
        f"/api/devices/{controlled_device['id']}/control", json={"mode": "sleep"}
    )
    assert response.status_code == 200
    assert response.json()["control_confirmed"] is False
    assert response.json()["state"]["mode"] == "manual"
    url = f"/api/devices/{controlled_device['id']}/status"
    assert (await signed_client.post(url)).status_code == 429
    provider.states["home@example.com"] = {"mode": "sleep"}
    with app.state.sessions() as db:
        db.get(Account, controlled_device["account_id"]).next_control_at = 0
        db.commit()
    calls = len(provider.calls)
    current = await signed_client.post(url)
    assert current.status_code == 200
    assert current.json()["state"]["mode"] == "sleep"
    assert len(provider.control_calls) == 1
    assert len(provider.status_calls) == 1
    assert len(provider.calls) == calls
    assert provider.status_calls[0][2] == "same-provider-id"
    with app.state.sessions() as db:
        assert db.get(Account, controlled_device["account_id"]).next_control_at == 0


async def test_status_requires_authentication_and_csrf(client, signed_client, controlled_device):
    url = f"/api/devices/{controlled_device['id']}/status"
    csrf = signed_client.headers.pop("X-CSRF-Token")
    assert (await signed_client.post(url)).status_code == 403
    signed_client.headers["X-CSRF-Token"] = csrf
    assert (await signed_client.post("/api/devices/missing/status")).status_code == 404
    signed_client.cookies.clear()
    assert (await client.post(url)).status_code == 401
