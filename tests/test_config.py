from conftest import TEST_HASH, TEST_PASSWORD
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from climate_control.config import DatabaseSettings, Settings, WorkerSettings
from climate_control.security import Vault, verify_password


def test_database_secret_file_newline_does_not_change_database_name(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "  postgresql://user:password@localhost/postgres\r\n")
    settings = DatabaseSettings(_env_file=None)
    url = make_url(settings.database_url)
    assert url.database == "postgres"
    assert url.password == "password"


def test_secret_file_whitespace_is_removed_before_security_validation():
    key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///./data/test.db\n",
        encryption_key=key + "\n",
        admin_password_hash="\n" + TEST_HASH + "\n",
        google_client_id="test-client\n",
        google_client_secret="test-secret\r\n",
        google_allowed_emails="owner@example.com",
        aqicn_api_token=SecretStr("test-token\n"),
    )
    assert verify_password(settings.admin_password_hash, TEST_PASSWORD)
    assert (settings.google_client_id, settings.google_client_secret) == (
        "test-client",
        "test-secret",
    )
    assert settings.aqicn_api_token.get_secret_value() == "test-token"
    vault = Vault(key)
    encrypted = vault.seal({"password": "  keep credential whitespace  "})
    assert Vault(settings.encryption_key).open(encrypted) == {
        "password": "  keep credential whitespace  ",
    }


def test_worker_secret_strings_are_trimmed_without_web_authentication():
    key = Fernet.generate_key().decode()
    worker = WorkerSettings(_env_file=None, encryption_key="\n" + key, aqicn_api_token="token\n")
    assert worker.encryption_key == key
    assert worker.aqicn_api_token.get_secret_value() == "token"
