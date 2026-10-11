import asyncio
import math
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from starlette.responses import RedirectResponse

from climate_control.db import Account, Device, LoginSession, LoginThrottle, OAuthFlow, now
from climate_control.history import history_view
from climate_control.outdoor import (
    OutdoorBusy,
    outdoor_history,
    outdoor_view,
    selected_location,
    set_location,
)
from climate_control.providers.aqicn import OutdoorError
from climate_control.providers.base import DeviceCommand, ProviderError
from climate_control.security import create_session, digest, verify_password

router = APIRouter()
SESSION_COOKIE = "climate_session"
FLOW_COOKIE = "climate_oauth"


def timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def authenticated(request: Request) -> LoginSession:
    token = request.cookies.get(SESSION_COOKIE, "")
    with request.app.state.sessions() as db:
        session = db.get(LoginSession, digest(token)) if token else None
        if session is None or session.expires_at <= now():
            raise HTTPException(401, "Sign in to continue.")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("X-CSRF-Token", "")
            origin = request.headers.get("Origin")
            if not secrets.compare_digest(csrf, session.csrf_token) or (
                origin is not None and origin != request.app.state.settings.app_url
            ):
                raise HTTPException(403, "Invalid request verification.")
        return session


def set_session(response: Response, request: Request, token: str):
    settings = request.app.state.settings
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


@router.get("/api/session")
def session_info(request: Request):
    settings = request.app.state.settings
    try:
        session = authenticated(request)
    except HTTPException:
        session = None
    return {
        "authenticated": session is not None,
        "identity": session.identity if session else None,
        "csrf_token": session.csrf_token if session else None,
        "google_enabled": settings.google_enabled,
        "local_enabled": bool(settings.admin_password_hash),
        "poll_interval_seconds": settings.poll_interval_seconds,
    }


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


def reserve_login_attempt(request: Request):
    """Atomic per-client limiter, shared across processes and revisions."""
    current = now()
    address = request.client.host if request.client else "unknown"
    key = digest(f"local-login:{address}")
    sessions = request.app.state.sessions
    with sessions() as db:
        db.execute(delete(LoginThrottle).where(LoginThrottle.expires_at <= current))
        db.commit()
        if db.get(LoginThrottle, key) is None:
            db.add(LoginThrottle(key=key, attempts=0, expires_at=current + 900))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
        allowed = db.execute(
            update(LoginThrottle)
            .where(
                LoginThrottle.key == key,
                LoginThrottle.attempts < 5,
            )
            .values(attempts=LoginThrottle.attempts + 1)
        ).rowcount
        db.commit()
        if not allowed:
            throttle = db.get(LoginThrottle, key)
            retry = max(1, math.ceil(throttle.expires_at - current))
            raise HTTPException(
                429,
                "Too many login attempts. Try again later.",
                headers={"Retry-After": str(retry)},
            )


@router.post("/api/login")
async def local_login(payload: LoginInput, request: Request, response: Response):
    settings = request.app.state.settings
    origin = request.headers.get("Origin")
    if origin is not None and origin != settings.app_url:
        raise HTTPException(403, "Invalid request origin.")
    if not settings.admin_password_hash:
        raise HTTPException(404, "Local login is not enabled.")
    reserve_login_attempt(request)
    correct_password = await asyncio.to_thread(
        verify_password, settings.admin_password_hash, payload.password
    )
    if not correct_password or not secrets.compare_digest(
        payload.username.encode(), settings.admin_username.encode()
    ):
        raise HTTPException(401, "Incorrect username or password.")
    with request.app.state.sessions() as db:
        token, session = create_session(db, settings.admin_username, settings.session_ttl_seconds)
    set_session(response, request, token)
    return {"identity": session.identity, "csrf_token": session.csrf_token}


@router.post("/api/logout")
def logout(
    request: Request,
    response: Response,
    session: Annotated[LoginSession, Depends(authenticated)],
):
    with request.app.state.sessions() as db:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == session.token_hash))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/google/login")
async def google_login(request: Request):
    settings = request.app.state.settings
    if not settings.google_enabled:
        raise HTTPException(404, "Google login is not enabled.")
    browser_token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    async with AsyncOAuth2Client(
        settings.google_client_id,
        scope="openid email",
        redirect_uri=settings.app_url + "/auth/google/callback",
        code_challenge_method="S256",
    ) as client:
        url, state = client.create_authorization_url(
            "https://accounts.google.com/o/oauth2/v2/auth",
            nonce=nonce,
            code_verifier=verifier,
        )
    with request.app.state.sessions() as db:
        db.execute(delete(OAuthFlow).where(OAuthFlow.expires_at <= now()))
        db.add(
            OAuthFlow(
                state=state,
                browser_hash=digest(browser_token),
                payload={"nonce": nonce, "verifier": verifier},
                expires_at=now() + 600,
            )
        )
        db.commit()
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        FLOW_COOKIE,
        browser_token,
        max_age=600,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    return response


def validate_google_identity(claims: dict, nonce: str, allowed: set[str]) -> str:
    email = str(claims.get("email", "")).lower()
    if (
        claims.get("email_verified") is not True
        or email not in allowed
        or not claims.get("sub")
        or not secrets.compare_digest(str(claims.get("nonce", "")), nonce)
    ):
        raise ValueError("Google identity is not authorized")
    return email


@router.get("/auth/google/callback")
async def google_callback(request: Request, state: str = "", code: str = ""):
    settings = request.app.state.settings
    if not settings.google_enabled:
        raise HTTPException(404, "Google login is not enabled.")
    with request.app.state.sessions() as db:
        flow = db.get(OAuthFlow, state)
        if (
            flow is None
            or flow.expires_at <= now()
            or not secrets.compare_digest(
                flow.browser_hash, digest(request.cookies.get(FLOW_COOKIE, ""))
            )
        ):
            raise HTTPException(400, "Google login expired. Please start again.")
        # Consume atomically so simultaneous callbacks cannot reuse an authorization flow.
        removed = db.execute(delete(OAuthFlow).where(OAuthFlow.state == state)).rowcount
        db.commit()
        if not removed:
            raise HTTPException(400, "Google login expired. Please start again.")
    try:
        if not code:
            raise ValueError("Missing authorization code")
        async with AsyncOAuth2Client(
            settings.google_client_id,
            settings.google_client_secret,
            redirect_uri=settings.app_url + "/auth/google/callback",
            timeout=15,
        ) as client:
            token = await client.fetch_token(
                "https://oauth2.googleapis.com/token",
                code=code,
                code_verifier=flow.payload["verifier"],
            )
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            token["id_token"],
            GoogleRequest(),
            settings.google_client_id,
        )
        email = validate_google_identity(claims, flow.payload["nonce"], settings.allowed_emails)
    except Exception:
        response = RedirectResponse("/login?error=google", status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/")
        return response
    with request.app.state.sessions() as db:
        session_token, _ = create_session(db, email, settings.session_ttl_seconds)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(FLOW_COOKIE, path="/")
    set_session(response, request, session_token)
    return response


class AccountInput(BaseModel):
    provider: Literal["vesync"]
    label: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=1, max_length=1024)
    country_code: str = Field(default="US", pattern=r"^[a-zA-Z]{2}$")
    time_zone: str = Field(default="UTC", max_length=100)

    @field_validator("label", "email", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("time_zone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA time zone, such as Asia/Singapore") from exc
        return value


def account_view(account: Account) -> dict:
    return {
        "id": account.id,
        "provider": account.provider,
        "label": account.label,
        "email": account.email,
        "country_code": account.country_code,
        "time_zone": account.time_zone,
        "status": account.status,
        "error": account.error,
        "last_attempt_at": timestamp(account.last_attempt_at),
        "last_success_at": timestamp(account.last_success_at),
        "next_refresh_at": timestamp(max(account.next_poll_at, account.lease_until)),
        "refreshing": account.lease_until > now(),
    }


@router.get("/api/accounts", dependencies=[Depends(authenticated)])
def list_accounts(request: Request):
    with request.app.state.sessions() as db:
        return [
            account_view(account)
            for account in db.scalars(select(Account).order_by(Account.created_at))
        ]


@router.post("/api/accounts", status_code=201, dependencies=[Depends(authenticated)])
async def connect_account(payload: AccountInput, request: Request):
    data = payload.model_dump()
    data["email"] = data["email"].lower()
    data["country_code"] = data["country_code"].upper()
    account = Account(
        label=data["label"],
        provider=data["provider"],
        email=data["email"],
        country_code=data["country_code"],
        time_zone=data["time_zone"],
        credentials=request.app.state.vault.seal(data),
    )
    with request.app.state.sessions() as db:
        db.add(account)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "This account is already connected.") from exc
        account_id = account.id
    await request.app.state.poller.refresh(account_id)
    with request.app.state.sessions() as db:
        saved = db.get(Account, account_id)
        if saved is None:
            raise HTTPException(404, "Account was removed while connecting.")
        return account_view(saved)


@router.put("/api/accounts/{account_id}", dependencies=[Depends(authenticated)])
async def reconnect_account(account_id: str, payload: AccountInput, request: Request):
    data = payload.model_dump()
    data["email"] = data["email"].lower()
    data["country_code"] = data["country_code"].upper()
    with request.app.state.sessions() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(404, "Account not found.")
        if account.provider != data["provider"]:
            raise HTTPException(422, "Connect a separate account to use a different provider.")
        if account.email != data["email"]:
            raise HTTPException(422, "Connect a separate account to use a different email.")
        # Update atomically against refresh claims, preserving all polling/backoff deadlines.
        changed = db.execute(
            update(Account)
            .where(
                Account.id == account_id,
                Account.lease_until <= now(),
            )
            .values(
                label=data["label"],
                country_code=data["country_code"],
                time_zone=data["time_zone"],
                credentials=request.app.state.vault.seal(data),
                tokens=None,
                status="pending",
                error=None,
                lease_token=None,
                lease_until=0,
            )
        ).rowcount
        if not changed:
            db.rollback()
            raise HTTPException(409, "A refresh is running. Try again when it finishes.")
        db.commit()
    await request.app.state.poller.refresh(account_id)
    with request.app.state.sessions() as db:
        saved = db.get(Account, account_id)
        if saved is None:
            raise HTTPException(404, "Account was removed while reconnecting.")
        return account_view(saved)


@router.delete("/api/accounts/{account_id}", dependencies=[Depends(authenticated)])
def disconnect_account(account_id: str, request: Request):
    with request.app.state.sessions() as db:
        removed = db.execute(delete(Account).where(Account.id == account_id)).rowcount
        db.commit()
        if not removed:
            raise HTTPException(404, "Account not found.")
    return {"ok": True}


@router.get("/api/devices", dependencies=[Depends(authenticated)])
def list_devices(request: Request):
    with request.app.state.sessions() as db:
        devices = db.execute(
            select(Device, Account).join(Account).order_by(Account.label, Device.name)
        )
        return [device_view(device, account, request) for device, account in devices]


def device_view(device: Device, account: Account, request: Request) -> dict:
    from climate_control.db import Automation, PlanningJob

    with request.app.state.sessions() as db:
        automation = db.get(Automation, device.id)
        job = db.get(PlanningJob, device.id)
        automation_view = (
            {
                "owner": automation.owner,
                "enabled": automation.enabled,
                "paused_until": automation.paused_until,
                "summary": automation.runtime.get("summary"),
                "pending_review": bool(job and job.status in {"pending", "claimed"}),
            }
            if automation
            else None
        )
    current = time.time()
    stale_after = max(
        request.app.state.settings.stale_after_seconds,
        request.app.state.settings.poll_interval_seconds * 3,
    )
    return {
        "id": device.id,
        "account_id": account.id,
        "automation": automation_view,
        "account_label": account.label,
        "name": device.name,
        "model": device.model,
        "supported": device.supported,
        "capabilities": device.capabilities,
        "controls": device.controls,
        "control_available": account.provider == "vesync"
        and account.status != "auth_error"
        and not (account.status == "rate_limited" and account.next_poll_at > current),
        "control_retry_at": timestamp(account.next_control_at),
        "state": device.state,
        "retrieved_at": timestamp(device.retrieved_at),
        "stale": device.retrieved_at is None or current - device.retrieved_at > stale_after,
        "error": device.error or account.error,
    }


class ControlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    power: StrictBool | None = None
    mode: Literal["auto", "sleep"] | None = None
    fan_level: StrictInt | None = None

    @model_validator(mode="after")
    def one_control(self):
        if sum(value is not None for value in (self.power, self.mode, self.fan_level)) != 1:
            raise ValueError("Choose one power, mode, or fan-level change.")
        return self


@router.post("/api/devices/{device_id}/control", dependencies=[Depends(authenticated)])
async def control_device(device_id: str, payload: ControlInput, request: Request):
    with request.app.state.sessions() as db:
        device = db.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "Device not found.")
        account = db.get(Account, device.account_id)
        if account.provider != "vesync":
            raise HTTPException(422, "This provider does not support device controls.")
        options = device.controls
        if (
            not device.supported
            or payload.power is not None
            and not options.get("power")
            or payload.mode is not None
            and payload.mode not in options.get("modes", [])
            or payload.fan_level is not None
            and payload.fan_level not in options.get("fan_levels", [])
        ):
            raise HTTPException(422, "This control is not supported by the device.")
        if device.state.get("connectivity") == "offline":
            raise HTTPException(409, "This device is offline.")
        if payload.power is None and device.state.get("power") != "on":
            raise HTTPException(409, "Turn on the device first.")
        from climate_control.automation_api import pause_for_manual

        pause_for_manual(db, device_id)
        db.commit()
    result = await request.app.state.poller.queued_control(
        device_id, DeviceCommand(**payload.model_dump())
    )
    return control_response(device_id, result, request)


@router.post("/api/devices/{device_id}/status", dependencies=[Depends(authenticated)])
async def retrieve_device_status(device_id: str, request: Request):
    with request.app.state.sessions() as db:
        device = db.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "Device not found.")
        account = db.get(Account, device.account_id)
        if not device.supported or account.provider != "vesync":
            raise HTTPException(422, "This device does not support status retrieval.")
    result = await request.app.state.poller.control(device_id, None)
    return control_response(device_id, result, request)


def control_response(device_id, result, request):
    if result.status == "missing":
        raise HTTPException(404, "Device not found.")
    if result.status == "busy":
        raise HTTPException(409, "An account update is running. Try again shortly.")
    if result.status == "auth_error":
        raise HTTPException(409, "Reconnect the account to control this device.")
    if result.status == "cooldown":
        raise HTTPException(
            429,
            "Wait a moment before another change.",
            headers={"Retry-After": str(result.retry_after)},
        )
    if result.status != "controlled":
        status = (
            429
            if result.status == "rate_limit"
            else 422
            if result.status == "invalid_control"
            else 502
        )
        headers = None
        if result.status == "rate_limit":
            with request.app.state.sessions() as db:
                device = db.get(Device, device_id)
                account = db.get(Account, device.account_id) if device else None
                retry = max(1, math.ceil(account.next_poll_at - time.time())) if account else 300
            headers = {"Retry-After": str(retry)}
        raise HTTPException(status, str(ProviderError(result.status)), headers=headers)
    with request.app.state.sessions() as db:
        device = db.get(Device, device_id)
        if device is None:
            raise HTTPException(404, "Device was removed while changing its settings.")
        return {
            **device_view(device, db.get(Account, device.account_id), request),
            "control_confirmed": result.control_confirmed,
        }


class OutdoorCoordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False, strict=True)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False, strict=True)


class OutdoorLocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: StrictInt = Field(gt=0)


class OutdoorSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=2, max_length=120)
    ]


@router.post("/api/outdoor/stations/search", dependencies=[Depends(authenticated)])
async def search_outdoor_stations(body: OutdoorSearchInput, request: Request):
    try:
        stations = await request.app.state.outdoor.search(body.query)
    except OutdoorBusy as exc:
        raise HTTPException(
            429,
            "Outdoor updates are busy. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except OutdoorError as exc:
        raise HTTPException(503, str(exc)) from None
    return {"stations": stations}


@router.post("/api/outdoor/stations/nearby", dependencies=[Depends(authenticated)])
async def nearby_outdoor_stations(body: OutdoorCoordinates, request: Request):
    try:
        stations = await request.app.state.outdoor.nearby(body.latitude, body.longitude)
    except OutdoorBusy as exc:
        raise HTTPException(
            429,
            "Outdoor updates are busy. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except OutdoorError as exc:
        raise HTTPException(503, str(exc)) from None
    return {"stations": stations}


@router.put("/api/outdoor/location", dependencies=[Depends(authenticated)])
def save_outdoor_location(
    body: OutdoorLocationInput, request: Request, background_tasks: BackgroundTasks
):
    with request.app.state.sessions() as db:
        try:
            set_location(db, body.location_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        result = outdoor_view(db, request.app.state.settings)
    background_tasks.add_task(request.app.state.outdoor.tick)
    return result


@router.delete("/api/outdoor/location", dependencies=[Depends(authenticated)])
def clear_outdoor_location(request: Request):
    with request.app.state.sessions() as db:
        set_location(db, None)
        return outdoor_view(db, request.app.state.settings)


@router.get("/api/outdoor", dependencies=[Depends(authenticated)])
def current_outdoor(request: Request):
    with request.app.state.sessions() as db:
        return outdoor_view(db, request.app.state.settings)


@router.get("/api/outdoor/history", dependencies=[Depends(authenticated)])
def current_outdoor_history(request: Request, hours: int = 24):
    if hours not in {24, 168, 720}:
        raise HTTPException(422, "Choose a history period of 24 hours, 7 days, or 30 days.")
    with request.app.state.sessions() as db:
        return outdoor_history(db, selected_location(db), hours, time.time())


@router.get("/api/devices/{device_id}/history", dependencies=[Depends(authenticated)])
def device_history(
    device_id: str,
    request: Request,
    hours: int = 24,
    raw: bool = False,
    after: float | None = Query(default=None, ge=0, allow_inf_nan=False),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    if hours not in {24, 168, 720}:
        raise HTTPException(422, "Choose a history period of 24 hours, 7 days, or 30 days.")
    with request.app.state.sessions() as db:
        if db.get(Device, device_id) is None:
            raise HTTPException(404, "Device not found.")
        return history_view(
            db,
            device_id,
            hours,
            time.time(),
            raw=raw,
            gap_seconds=max(180, request.app.state.settings.poll_interval_seconds * 3),
            after=after,
            limit=limit,
        )


@router.post("/api/accounts/{account_id}/refresh", dependencies=[Depends(authenticated)])
async def refresh_account(account_id: str, request: Request):
    result = await request.app.state.poller.refresh(account_id)
    if result.status == "missing":
        raise HTTPException(404, "Account not found.")
    if result.status == "auth_error":
        raise HTTPException(409, "Update this account's credentials to reconnect.")
    if result.status == "cooldown":
        from starlette.responses import JSONResponse

        return JSONResponse(
            {"status": "cooldown", "retry_after": result.retry_after},
            status_code=429,
            headers={"Retry-After": str(result.retry_after)},
        )
    with request.app.state.sessions() as db:
        account = db.get(Account, account_id)
        return {"status": "refreshed", "account": account_view(account) if account else None}


def scheduler_identity(claims: dict, expected_email: str):
    if claims.get("email_verified") is not True or claims.get("email") != expected_email:
        raise ValueError("Invalid scheduler identity")


@router.post("/internal/poll")
async def scheduler_poll(request: Request):
    settings = request.app.state.settings
    if not settings.scheduler_service_account or not settings.scheduler_audience:
        raise HTTPException(404, "External scheduling is not configured.")
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Scheduler authentication required.")
    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            authorization[7:],
            GoogleRequest(),
            settings.scheduler_audience,
        )
        scheduler_identity(claims, settings.scheduler_service_account)
    except Exception as exc:
        raise HTTPException(401, "Invalid scheduler identity.") from exc
    return await request.app.state.poller.tick()
