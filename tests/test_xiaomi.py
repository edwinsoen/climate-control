import base64
import json
import time

from aiohttp import web

from climate_control.db import Account
from climate_control.providers.base import DeviceCommand, DeviceSnapshot, PollResult
from climate_control.providers.xiaomi import (
    MODEL_SPECS,
    XiaomiProvider,
    decode_request_data,
    encrypt_request,
    level_to_rpm,
    normalize_xiaomi_device,
    rc4_encrypt,
    rpm_to_level,
    server_base_url,
    signed_nonce,
)


def test_xiaomi_regional_servers_and_signature():
    assert server_base_url("SG") == "https://sg.api.io.mi.com/app"
    assert server_base_url("DE") == "https://de.api.io.mi.com/app"
    assert server_base_url("US") == "https://us.api.io.mi.com/app"
    assert server_base_url("IN") == "https://in.api.io.mi.com/app"
    assert server_base_url("RU") == "https://ru.api.io.mi.com/app"
    assert server_base_url("TW") == "https://tw.api.io.mi.com/app"
    assert server_base_url("FR") == "https://de.api.io.mi.com/app"
    assert server_base_url("CA") == "https://us.api.io.mi.com/app"

    ssecurity = base64.b64encode(b"0123456789abcdef").decode()
    nonce = base64.b64encode(b"123456789012").decode()
    encrypted = encrypt_request("/home/device_list", {"getVirtualModel": False}, ssecurity, nonce)
    assert encrypted["_nonce"] == nonce
    assert decode_request_data(encrypted, ssecurity) == {"getVirtualModel": False}


def test_xiaomi_model_normalization_across_international_models():
    # Smart Air Purifier 4 Pro: 1..14 favorite levels, PM2.5, PM10, temp, humidity, standard display
    spec_4pro = MODEL_SPECS["zhimi.airpurifier.vb4"]
    snap_4pro = normalize_xiaomi_device(
        {"did": "mi-4pro", "name": "Living Room 4 Pro", "model": "zhimi.airpurifier.vb4"},
        spec_4pro,
        {
            (2, 1): True,
            (2, 4): 2,
            (4, 1): 88,
            (13, 2): 1,
            (9, 11): 9,
            (3, 4): 18,
            (3, 8): 42,
            (3, 7): 24.6,
            (3, 1): 56,
        },
    )
    assert snap_4pro.model == "Smart Air Purifier 4 Pro"
    assert set(snap_4pro.capabilities) == {
        "power",
        "mode",
        "fan_level",
        "filter_life",
        "display",
        "air_quality",
        "pm25",
        "pm10",
        "temperature",
        "humidity",
    }
    assert snap_4pro.controls == {
        "power": True,
        "modes": ["auto", "sleep"],
        "fan_levels": list(range(1, 15)),
        "display": True,
        "display_states": ["bright", "dim", "off"],
        "display_modes": ["manual", "warnings_only_bright", "warnings_only_dim"],
    }
    assert snap_4pro.state["power"] == "on"
    assert snap_4pro.state["mode"] == "manual"
    assert snap_4pro.state["fan_level"] == 9
    assert snap_4pro.state["display"] == "dim"
    assert snap_4pro.state["pm25"] == 18
    assert snap_4pro.state["pm10"] == 42
    assert snap_4pro.state["temperature"] == 24.6
    assert snap_4pro.state["humidity"] == 56
    assert snap_4pro.state["air_quality"] == "good"

    # Mi Air Purifier 3H: inverted display map (0=bright, 2=off) and 0-indexed favorite -> 1
    spec_3h = MODEL_SPECS["zhimi.airpurifier.mb3"]
    snap_3h = normalize_xiaomi_device(
        {"did": "mi-3h", "name": "Bedroom 3H", "model": "zhimi.airpurifier.mb3"},
        spec_3h,
        {
            (2, 2): True,
            (2, 5): 0,
            (4, 3): 75,
            (6, 1): 0,
            (10, 10): 0,
            (3, 6): 8,
            (3, 8): 22.1,
            (3, 7): 49,
        },
    )
    assert snap_3h.state["mode"] == "auto"
    assert snap_3h.state["fan_level"] == 1
    assert snap_3h.state["display"] == "bright"
    assert snap_3h.state["air_quality"] == "excellent"

    # Smart Air Purifier 4 Lite (rma1): stepless RPM quantized into 1..10 (10 rpm steps)
    spec_4lite = MODEL_SPECS["zhimi.airpurifier.rma1"]
    for lvl in range(1, 11):
        for rpm_range in ((300, 2000), (300, 2200)):
            rpm = level_to_rpm(lvl, rpm_range)
            assert rpm % 10 == 0
            assert rpm_to_level(rpm, rpm_range) == lvl
    rpm_for_6 = level_to_rpm(6, spec_4lite.rpm_range)
    assert rpm_to_level(rpm_for_6, spec_4lite.rpm_range) == 6
    snap_4lite = normalize_xiaomi_device(
        {"did": "mi-4lite", "name": "Study 4 Lite", "model": "zhimi.airpurifier.rma1"},
        spec_4lite,
        {
            (2, 1): True,
            (2, 4): 2,
            (4, 1): 92,
            (13, 2): 2,
            (9, 10): rpm_for_6,
            (3, 4): 11,
            (3, 7): 23.0,
            (3, 1): 51,
        },
    )
    assert snap_4lite.controls["fan_levels"] == list(range(1, 11))
    assert snap_4lite.state["fan_level"] == 6
    assert snap_4lite.state["display"] == "bright"

    # International .airp. models: 4 Lite (zhimi.airp.rmb1) and 4 Compact (zhimi.airp.cpa4)
    spec_rmb1 = MODEL_SPECS["zhimi.airp.rmb1"]
    snap_rmb1 = normalize_xiaomi_device(
        {"did": "645608278", "name": "MBR Air Purifier", "model": "zhimi.airp.rmb1"},
        spec_rmb1,
        {
            (2, 1): True,
            (2, 4): 2,
            (4, 1): 86,
            (13, 2): 0,
            (9, 11): 10,
            (3, 4): 4,
            (3, 7): 26.0,
            (3, 1): 53,
        },
    )
    assert snap_rmb1.model == "Smart Air Purifier 4 Lite"
    assert snap_rmb1.state["fan_level"] == 10
    assert snap_rmb1.state["pm25"] == 4
    assert snap_rmb1.state["temperature"] == 26.0
    assert snap_rmb1.state["humidity"] == 53
    assert snap_rmb1.state["display"] == "off"

    spec_cpa4 = MODEL_SPECS["zhimi.airp.cpa4"]
    snap_cpa4 = normalize_xiaomi_device(
        {"did": "648503333", "name": "CBR Air Purifier", "model": "zhimi.airp.cpa4"},
        spec_cpa4,
        {
            (2, 1): True,
            (2, 4): 0,
            (4, 1): 95,
            (13, 2): 1,
            (9, 11): 5,
            (3, 4): 12,
        },
    )
    assert snap_cpa4.model == "Smart Air Purifier 4 Compact"
    assert snap_cpa4.state["pm25"] == 12
    assert snap_cpa4.state["air_quality"] == "excellent"
    assert snap_cpa4.state["temperature"] is None
    assert snap_cpa4.state["humidity"] is None

    # Negative indoor temperature (-5.5 C) is preserved; out-of-range humidity (>100%) is rejected
    # MIoT mode 4 (Medium preset) maps to preset so manual fan commands are not skipped
    # Unrecognized raw_aq (-1) falls back to pm25
    snap_subzero = normalize_xiaomi_device(
        {"did": "645608278", "name": "Cold Room", "model": "zhimi.airp.rmb1"},
        spec_rmb1,
        {
            (2, 1): True,
            (2, 4): 4,
            (4, 1): 80,
            (13, 2): 1,
            (9, 11): 5,
            (3, 4): 6,
            (3, 6): -1,
            (3, 7): -5.5,
            (3, 1): 150,
        },
    )
    assert snap_subzero.state["mode"] == "preset"
    assert not XiaomiProvider.matches_command(snap_subzero, DeviceCommand(fan_level=5))
    assert snap_subzero.state["air_quality"] == "excellent"
    assert snap_subzero.state["temperature"] == -5.5
    assert snap_subzero.state["humidity"] is None

    # 3C display writes canonical 0/1/2 values even though readback map includes 4 and 8
    spec_3c = MODEL_SPECS["zhimi.airp.mb4"]
    assert XiaomiProvider.build_writes("did-3c", spec_3c, DeviceCommand(), "dim") == [
        {"did": "did-3c", "siid": 7, "piid": 2, "value": 1}
    ]
    assert XiaomiProvider.build_writes("did-3c", spec_3c, DeviceCommand(), "bright") == [
        {"did": "did-3c", "siid": 7, "piid": 2, "value": 2}
    ]


async def test_xiaomi_provider_login_poll_and_control(monkeypatch):
    ssecurity = base64.b64encode(b"xiaomi-test-ssec").decode()
    device_props = {
        (2, 1): True,
        (2, 4): 0,
        (4, 1): 90,
        (13, 2): 2,
        (9, 11): 5,
        (3, 4): 14,
        (3, 7): 25.2,
        (3, 1): 60,
    }
    set_calls = []
    auth2_calls = 0

    async def login_step1(request):
        if request.cookies.get("passToken") == "saved-pass-token":
            return web.Response(
                text="&&&START&&&"
                + json.dumps(
                    {
                        "userId": 123456,
                        "ssecurity": ssecurity,
                        "nonce": 1000,
                        "location": str(request.url.with_path("/sts")),
                    }
                )
            )
        return web.Response(
            text='&&&START&&&{"_sign":"sig-1","qs":"qs-1","callback":"cb-1"}'
        )

    async def login_step2(request):
        nonlocal auth2_calls
        auth2_calls += 1
        post = await request.post()
        if post.get("user") != "mi@example.com":
            return web.Response(text='&&&START&&&{"code":70016}')
        return web.Response(
            text=json.dumps(
                {
                    "userId": 123456,
                    "ssecurity": ssecurity,
                    "passToken": "saved-pass-token",
                    "nonce": 999,
                    "location": str(request.url.with_path("/sts")),
                }
            )
        )

    async def sts_handler(request):
        resp = web.Response(text="ok")
        resp.set_cookie("serviceToken", "valid-service-token")
        return resp

    async def device_list_handler(request):
        if request.cookies.get("serviceToken") != "valid-service-token":
            return web.json_response({"code": 401, "message": "unauthorized"}, status=401)
        return web.json_response(
            {
                "code": 0,
                "result": {
                    "list": [
                        {
                            "did": "did-mb5",
                            "name": "Xiaomi 4",
                            "model": "zhimi.airpurifier.mb5",
                            "isOnline": True,
                        },
                        {
                            "did": "did-legacy",
                            "name": "Old Purifier",
                            "model": "zhimi.airpurifier.m1",
                            "isOnline": True,
                        },
                    ]
                },
            }
        )

    async def prop_get_handler(request):
        post = await request.post()
        params = decode_request_data(post, ssecurity)["params"]
        snonce = signed_nonce(ssecurity, post["_nonce"])
        payload = json.dumps(
            {
                "code": 0,
                "result": [
                    {
                        "did": item["did"],
                        "siid": item["siid"],
                        "piid": item["piid"],
                        "code": 0,
                        "value": device_props.get((item["siid"], item["piid"])),
                    }
                    for item in params
                ],
            }
        )
        return web.Response(text=rc4_encrypt(snonce, payload))

    async def prop_set_handler(request):
        post = await request.post()
        params = decode_request_data(post, ssecurity)["params"]
        set_calls.append(params)
        for item in params:
            device_props[(item["siid"], item["piid"])] = item["value"]
        snonce = signed_nonce(ssecurity, post["_nonce"])
        payload = json.dumps(
            {
                "code": 0,
                "result": [
                    {"did": item["did"], "siid": item["siid"], "piid": item["piid"], "code": 0}
                    for item in params
                ],
            }
        )
        return web.Response(text=rc4_encrypt(snonce, payload))

    test_app = web.Application()
    test_app.router.add_get("/pass/serviceLogin", login_step1)
    test_app.router.add_post("/pass/serviceLoginAuth2", login_step2)
    test_app.router.add_get("/sts", sts_handler)
    test_app.router.add_post("/app/home/device_list", device_list_handler)
    test_app.router.add_post("/app/miotspec/prop/get", prop_get_handler)
    test_app.router.add_post("/app/miotspec/prop/set", prop_set_handler)

    runner = web.AppRunner(test_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    monkeypatch.setattr(
        "climate_control.providers.xiaomi.XIAOMI_LOGIN_URL", f"{base}/pass/serviceLogin"
    )
    monkeypatch.setattr(
        "climate_control.providers.xiaomi.XIAOMI_AUTH_URL", f"{base}/pass/serviceLoginAuth2"
    )
    monkeypatch.setattr(
        "climate_control.providers.xiaomi.server_base_url", lambda _code: f"{base}/app"
    )

    try:
        provider = XiaomiProvider(spacing=0)
        creds = {
            "email": "mi@example.com",
            "password": "secret-password",
            "country_code": "SG",
        }
        polled = await provider.poll(creds, None)
        assert polled.failure is None
        assert polled.tokens and polled.tokens["service_token"] == "valid-service-token"
        assert len(polled.devices) == 2

        supported = next(d for d in polled.devices if d.supported)
        unsupported = next(d for d in polled.devices if not d.supported)
        assert supported.provider_id == "did-mb5"
        assert supported.state["pm25"] == 14
        assert supported.state["temperature"] == 25.2
        assert supported.state["humidity"] == 60
        assert supported.state["display"] == "bright"
        assert not unsupported.supported

        # Control fan_level -> sets Favorite mode (2) and favorite-level (11)
        controlled = await provider.control(
            creds, polled.tokens, "did-mb5", DeviceCommand(fan_level=11)
        )
        assert controlled.failure is None
        assert controlled.control_confirmed is True
        assert controlled.devices[0].state["mode"] == "manual"
        assert controlled.devices[0].state["fan_level"] == 11
        assert set_calls[-1] == [
            {"did": "did-mb5", "siid": 2, "piid": 4, "value": 2},
            {"did": "did-mb5", "siid": 9, "piid": 11, "value": 11},
        ]

        # Control display to dim -> writes brightness=1
        dimmed = await provider.control(
            creds, polled.tokens, "did-mb5", DeviceCommand(display="dim")
        )
        assert dimmed.failure is None
        assert dimmed.control_confirmed is True
        assert dimmed.devices[0].state["display"] == "dim"

        # Expired token reauthenticates via pass_token in step1 without posting password again
        assert auth2_calls == 1
        expired_tokens = {**polled.tokens, "service_token": "expired"}
        refreshed = await provider.status(creds, expired_tokens, "did-mb5")
        assert refreshed.failure is None
        assert refreshed.tokens["service_token"] == "valid-service-token"
        assert auth2_calls == 1
    finally:
        await runner.cleanup()


async def test_xiaomi_display_warning_modes_and_same_email_across_providers(app, signed_client):
    # Connect a VeSync account and a Xiaomi account using the exact same email address
    vesync_res = await signed_client.post(
        "/api/accounts",
        json={
            "provider": "vesync",
            "label": "Levoit Home",
            "email": "shared@example.com",
            "password": "vesync-password",
            "country_code": "SG",
            "time_zone": "Asia/Singapore",
        },
    )
    assert vesync_res.status_code == 201

    app.state.poller.provider.results.append(
        PollResult(
            [
                DeviceSnapshot(
                    provider_id="xiaomi-purifier-1",
                    name="Xiaomi 4 Pro",
                    model="Smart Air Purifier 4 Pro",
                    capabilities=[
                        "power",
                        "mode",
                        "fan_level",
                        "filter_life",
                        "display",
                        "air_quality",
                        "pm25",
                        "pm10",
                        "temperature",
                        "humidity",
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
                        "mode": "auto",
                        "fan_level": 5,
                        "reported_fan_level": 5,
                        "filter_life": 95,
                        "display": "off",
                        "air_quality": "good",
                        "pm25": 22,
                        "pm10": 35,
                        "temperature": 24.5,
                        "humidity": 55,
                    },
                    retrieved_at=time.time(),
                )
            ],
            {"user_id": "1", "ssecurity": "s", "service_token": "t", "country_code": "SG"},
        )
    )
    xiaomi_res = await signed_client.post(
        "/api/accounts",
        json={
            "provider": "xiaomi",
            "label": "Xiaomi Home",
            "email": "shared@example.com",
            "password": "xiaomi-password",
            "country_code": "SG",
            "time_zone": "Asia/Singapore",
        },
    )
    assert xiaomi_res.status_code == 201
    xiaomi_account_id = xiaomi_res.json()["id"]

    devices = (await signed_client.get("/api/devices")).json()
    xiaomi_dev = next(d for d in devices if d["name"] == "Xiaomi 4 Pro")
    device_id = xiaomi_dev["id"]

    # Configure warnings_only_dim on the Xiaomi device
    mode_res = await signed_client.put(
        f"/api/devices/{device_id}/display-settings",
        json={"mode": "warnings_only_dim", "warning_threshold": "who_2021_24h"},
    )
    assert mode_res.status_code == 200
    assert mode_res.json()["display"]["mode"] == "warnings_only_dim"

    # Next poll with PM2.5 = 22 (> 15 WHO 2021 24h) actuates display="dim"
    with app.state.sessions() as db:
        acc = db.get(Account, xiaomi_account_id)
        acc.next_poll_at = 0
        acc.next_control_at = 0
        db.commit()

    app.state.poller.provider.results.append(
        PollResult(
            [
                DeviceSnapshot(
                    provider_id="xiaomi-purifier-1",
                    name="Xiaomi 4 Pro",
                    model="Smart Air Purifier 4 Pro",
                    capabilities=xiaomi_dev["capabilities"],
                    controls=xiaomi_dev["controls"],
                    state={**xiaomi_dev["state"], "pm25": 22, "display": "off"},
                    retrieved_at=time.time(),
                )
            ]
        )
    )
    app.state.poller.provider.control_results.append(
        PollResult(
            [
                DeviceSnapshot(
                    provider_id="xiaomi-purifier-1",
                    name="Xiaomi 4 Pro",
                    model="Smart Air Purifier 4 Pro",
                    capabilities=xiaomi_dev["capabilities"],
                    controls=xiaomi_dev["controls"],
                    state={**xiaomi_dev["state"], "pm25": 22, "display": "dim"},
                    retrieved_at=time.time(),
                )
            ]
        )
    )
    await app.state.poller.refresh(xiaomi_account_id)
    assert app.state.poller.provider.control_calls[-1][-1] == DeviceCommand(display="dim")

    # History records temperature and humidity
    history = (await signed_client.get(f"/api/devices/{device_id}/history?hours=24")).json()
    assert history["metrics"]["temperature"][0]["value"] == 24.5
    assert history["metrics"]["humidity"][0]["value"] == 55

    # Rules can evaluate indoor temperature and humidity
    preview = await signed_client.post(
        f"/api/devices/{device_id}/automation/preview",
        json={
            "version": 1,
            "timezone": "Asia/Singapore",
            "rules": [
                {
                    "id": "warm-humid",
                    "explanation": "Run fan 8 when warm and humid",
                    "windows": [],
                    "conditions": [
                        {
                            "source": {
                                "kind": "device",
                                "device_id": device_id,
                                "metric": "temperature",
                            },
                            "test": "range",
                            "lower": 24,
                        },
                        {
                            "source": {
                                "kind": "device",
                                "device_id": device_id,
                                "metric": "humidity",
                            },
                            "test": "range",
                            "lower": 50,
                        },
                    ],
                    "action": {"power": "on", "fan": 8},
                }
            ],
            "fallback": {"power": "hold"},
        },
    )
    assert preview.status_code == 200
    assert preview.json()["decision"]["rule_id"] == "warm-humid"


async def test_xiaomi_account_id_and_email_2fa_verification(monkeypatch):
    ssecurity = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    sent_tickets: list[dict] = []
    verified_tickets: list[dict] = []

    async def login_step1(request):
        return web.Response(
            text='&&&START&&&{"sid":"xiaomiio","_sign":"sign-token","qs":"%3Fsid%3Dxiaomiio"}'
        )

    async def login_step2(request):
        post = await request.post()
        assert post.get("user") == "alice@example.com"
        base = f"http://127.0.0.1:{request.url.port}"
        notif_url = f"{base}/fe/service/identity/authStart?sid=xiaomiio&context=ctx-2fa"
        return web.Response(
            text="&&&START&&&"
            + json.dumps(
                {
                    "code": 0,
                    "securityStatus": 16,
                    "notificationUrl": notif_url,
                }
            )
        )

    async def auth_start(request):
        resp = web.Response(text="ok")
        resp.set_cookie("identity_session", "id-sess-1")
        resp.set_cookie("ick", "ick-token")
        return resp

    async def identity_list(request):
        return web.Response(
            text='&&&START&&&{"code":2,"flag":8,"option":8,"options":[8],"externalId":"1583126662"}'
        )

    async def send_email_ticket(request):
        post = await request.post()
        sent_tickets.append(dict(post))
        return web.Response(text='&&&START&&&{"code":0}')

    async def verify_email(request):
        post = await request.post()
        verified_tickets.append(dict(post))
        if post.get("ticket") != "654321":
            return web.Response(text='&&&START&&&{"code":70014}')
        base = f"http://127.0.0.1:{request.url.port}"
        return web.Response(
            text="&&&START&&&"
            + json.dumps({"code": 0, "location": f"{base}/identity/result/check?context=ctx-2fa"})
        )

    async def result_check(request):
        base = f"http://127.0.0.1:{request.url.port}"
        raise web.HTTPFound(location=f"{base}/pass/serviceLoginAuth2/end")

    async def auth2_end(request):
        base = f"http://127.0.0.1:{request.url.port}"
        return web.Response(
            status=302,
            headers={
                "extension-pragma": json.dumps({"ssecurity": ssecurity}),
                "Location": f"{base}/sts",
            },
        )

    async def sts_handler(request):
        resp = web.Response(text="ok")
        resp.set_cookie("serviceToken", "2fa-service-token")
        resp.set_cookie("passToken", "trusted-pass-token")
        return resp

    async def device_list_handler(request):
        assert request.cookies.get("userId") == "1583126662"
        return web.json_response({"code": 0, "result": {"list": []}})

    test_app = web.Application()
    test_app.router.add_get("/pass/serviceLogin", login_step1)
    test_app.router.add_post("/pass/serviceLoginAuth2", login_step2)
    test_app.router.add_get("/fe/service/identity/authStart", auth_start)
    test_app.router.add_get("/identity/list", identity_list)
    test_app.router.add_post("/identity/auth/sendEmailTicket", send_email_ticket)
    test_app.router.add_post("/identity/auth/verifyEmail", verify_email)
    test_app.router.add_get("/identity/result/check", result_check)
    test_app.router.add_get("/pass/serviceLoginAuth2/end", auth2_end)
    test_app.router.add_get("/sts", sts_handler)
    test_app.router.add_post("/app/home/device_list", device_list_handler)

    runner = web.AppRunner(test_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    monkeypatch.setattr("climate_control.providers.xiaomi.XIAOMI_ACCOUNT_BASE", base)
    monkeypatch.setattr(
        "climate_control.providers.xiaomi.XIAOMI_LOGIN_URL", f"{base}/pass/serviceLogin"
    )
    monkeypatch.setattr(
        "climate_control.providers.xiaomi.XIAOMI_AUTH_URL", f"{base}/pass/serviceLoginAuth2"
    )
    monkeypatch.setattr(
        "climate_control.providers.xiaomi.server_base_url", lambda _code: f"{base}/app"
    )

    try:
        provider = XiaomiProvider(spacing=0)
        creds = {
            "email": "alice@example.com",
            "password": "secret-password",
            "country_code": "SG",
        }
        # First poll triggers 2FA email ticket and returns pending_2fa tokens
        first = await provider.poll(creds, None)
        assert first.failure is not None
        assert first.failure.kind == "auth_2fa"
        assert first.tokens and "pending_2fa" in first.tokens
        assert len(sent_tickets) == 1

        # Wrong code returns auth_2fa_invalid while preserving pending_2fa tokens
        wrong = await provider.poll(
            {**creds, "verification_code": "000000"},
            first.tokens,
        )
        assert wrong.failure is not None
        assert wrong.failure.kind == "auth_2fa_invalid"
        assert wrong.tokens and "pending_2fa" in wrong.tokens

        # Second poll with verification_code completes 2FA and obtains service_token
        second = await provider.poll(
            {**creds, "verification_code": "654321"},
            first.tokens,
        )
        assert second.failure is None
        assert second.tokens["service_token"] == "2fa-service-token"
        assert second.tokens["pass_token"] == "trusted-pass-token"
        assert second.tokens["user_id"] == "1583126662"
        assert verified_tickets[-1]["trust"] == "false"
    finally:
        await runner.cleanup()


async def test_api_xiaomi_2fa_reconnect_runs_immediately_without_cooldown(app, signed_client):
    from climate_control.providers.base import ProviderError

    provider = app.state.poller.provider
    provider.results.append(
        PollResult(
            [],
            tokens={"pending_2fa": {"context": "ctx-1", "device_id": "DEV1", "flag": "8"}},
            failure=ProviderError("auth_2fa"),
        )
    )
    created = await signed_client.post(
        "/api/accounts",
        json={
            "provider": "xiaomi",
            "label": "Xiaomi Home",
            "email": "1583126662",
            "password": "xiaomi-secret-password",
            "country_code": "SG",
            "time_zone": "Asia/Singapore",
        },
    )
    assert created.status_code == 201
    account = created.json()
    assert account["status"] == "auth_error"
    assert "verification code" in account["error"]

    # Submitting the verification code via PUT immediately refreshes without waiting 60s
    reconnected = await signed_client.put(
        f"/api/accounts/{account['id']}",
        json={
            "provider": "xiaomi",
            "label": "Xiaomi Home",
            "email": "1583126662",
            "password": "",
            "verification_code": "654321",
            "country_code": "SG",
            "time_zone": "Asia/Singapore",
        },
    )
    assert reconnected.status_code == 200
    assert reconnected.json()["status"] == "connected"
    with app.state.sessions() as db:
        saved = db.get(Account, account["id"])
        stored_creds = app.state.vault.open(saved.credentials)
        assert stored_creds["verification_code"] == ""
