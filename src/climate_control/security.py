import hashlib
import json
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from sqlalchemy import delete

from climate_control.db import LoginSession, OAuthFlow, now

password_hasher = PasswordHasher()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Vault:
    def __init__(self, key: str):
        self.fernet = Fernet(key.encode())

    def seal(self, value: dict) -> str:
        return self.fernet.encrypt(json.dumps(value).encode()).decode()

    def open(self, value: str) -> dict:
        return json.loads(self.fernet.decrypt(value.encode()))


def verify_password(encoded: str, password: str) -> bool:
    try:
        return password_hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def create_session(db, identity: str, ttl: int) -> tuple[str, LoginSession]:
    token = secrets.token_urlsafe(32)
    session = LoginSession(
        token_hash=digest(token),
        identity=identity,
        csrf_token=secrets.token_urlsafe(32),
        expires_at=now() + ttl,
    )
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= now()))
    db.execute(delete(OAuthFlow).where(OAuthFlow.expires_at <= now()))
    db.add(session)
    db.commit()
    return token, session
