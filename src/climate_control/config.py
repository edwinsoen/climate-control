from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/climate.db"
    encryption_key: str
    app_url: str = "http://localhost:8080"
    admin_username: str = "admin"
    admin_password_hash: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_allowed_emails: str = ""
    scheduler_enabled: bool = True
    scheduler_service_account: str = ""
    scheduler_audience: str = ""
    poll_interval_seconds: int = Field(default=60, ge=60, le=3600)
    request_spacing_seconds: float = Field(default=1.0, ge=1.0)
    stale_after_seconds: int = Field(default=180, ge=180)
    frontend_dir: Path = Path("frontend/dist")
    session_ttl_seconds: int = Field(default=86400, ge=300, le=604800)

    @property
    def secure_cookies(self) -> bool:
        return self.app_url.startswith("https://")

    @property
    def allowed_emails(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.google_allowed_emails.split(",")
            if email.strip()
        }

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.allowed_emails)

    @model_validator(mode="after")
    def validate_security(self):
        Fernet(self.encryption_key.encode())
        self.app_url = self.app_url.rstrip("/")
        origin = urlsplit(self.app_url)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path
            or origin.query
            or origin.fragment
            or (origin.scheme == "http" and origin.hostname not in {"localhost", "127.0.0.1"})
        ):
            raise ValueError("APP_URL must use HTTPS, except localhost development")
        if not self.admin_password_hash and not self.google_enabled:
            raise ValueError("Configure an administrator password hash or Google login")
        if self.admin_password_hash and not self.admin_password_hash.startswith("$argon2id$"):
            raise ValueError("ADMIN_PASSWORD_HASH must be an Argon2id hash")
        if bool(self.google_client_id) != bool(self.google_client_secret):
            raise ValueError("Google client ID and secret must be configured together")
        if self.google_client_id and not self.allowed_emails:
            raise ValueError("GOOGLE_ALLOWED_EMAILS is required for Google login")
        if not self.scheduler_enabled and not (
            self.scheduler_service_account and self.scheduler_audience
        ):
            raise ValueError("External scheduling requires its service account and audience")
        return self
