"""Scoped bearer tokens for MCP clients, managed from an authenticated browser session."""

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update

from climate_control.agent.tools import SCOPES
from climate_control.api import authenticated
from climate_control.db import AccessToken
from climate_control.security import digest

TOKEN_PREFIX = "cc_"
LAST_USED_INTERVAL = 60
router = APIRouter(dependencies=[Depends(authenticated)])


class TokenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1, max_length=len(SCOPES))
    expires_in_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("scopes")
    @classmethod
    def known_scopes(cls, value):
        if not set(value) <= set(SCOPES) or len(set(value)) != len(value):
            raise ValueError(f"Choose distinct scopes from {', '.join(SCOPES)}")
        return [scope for scope in SCOPES if scope in value]


def token_view(row: AccessToken) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "scopes": row.scopes,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "expires_at": row.expires_at,
    }


def issue_token(db, body: TokenInput) -> tuple[str, AccessToken]:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = AccessToken(
        name=body.name,
        token_hash=digest(token),
        scopes=body.scopes,
        expires_at=time.time() + body.expires_in_days * 86400 if body.expires_in_days else None,
    )
    db.add(row)
    db.commit()
    return token, row


def verify_access_token(sessions, token: str) -> dict | None:
    """Return the token's name and scopes, or None when unknown, revoked, or expired."""
    if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        return None
    current = time.time()
    with sessions() as db:
        # The lookup is by SHA-256 digest, so response timing does not reveal token bytes.
        row = db.scalar(select(AccessToken).where(AccessToken.token_hash == digest(token)))
        if (
            row is None
            or row.revoked_at is not None
            or (row.expires_at is not None and row.expires_at <= current)
        ):
            return None
        if row.last_used_at is None or current - row.last_used_at >= LAST_USED_INTERVAL:
            db.execute(
                update(AccessToken).where(AccessToken.id == row.id).values(last_used_at=current)
            )
            db.commit()
        return {"id": row.id, "name": row.name, "scopes": list(row.scopes)}


@router.get("/api/access-tokens")
def list_tokens(request: Request):
    with request.app.state.sessions() as db:
        rows = db.scalars(
            select(AccessToken)
            .where(AccessToken.revoked_at.is_(None))
            .order_by(AccessToken.created_at)
        )
        return [token_view(row) for row in rows]


@router.post("/api/access-tokens", status_code=201)
def create_token(body: TokenInput, request: Request):
    with request.app.state.sessions() as db:
        token, row = issue_token(db, body)
        return {**token_view(row), "token": token}


@router.delete("/api/access-tokens/{token_id}")
def revoke_token(token_id: str, request: Request):
    with request.app.state.sessions() as db:
        revoked = db.execute(
            update(AccessToken)
            .where(AccessToken.id == token_id, AccessToken.revoked_at.is_(None))
            .values(revoked_at=time.time())
        ).rowcount
        db.commit()
    if not revoked:
        raise HTTPException(404, "Access token not found.")
    return {"ok": True}


@router.get("/api/agent/status")
def agent_status(request: Request):
    settings = request.app.state.settings
    provider = settings.model_provider
    return {
        "enabled": provider is not None,
        "provider": provider,
        "runtime": settings.agent_runtime,
        "model": settings.model_name if provider not in {None, "remote"} else None,
        "mcp_url": settings.app_url + "/mcp",
        "scopes": list(SCOPES),
    }
