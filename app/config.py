"""Application configuration loaded from environment variables."""

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class Settings:
    monzo_client_id: str
    monzo_client_secret: str = field(repr=False)
    monzo_redirect_uri: str
    database_url: str = "sqlite:///./monzo_scheduler.db"
    jwt_secret_key: str = field(default="", repr=False)
    token_encryption_key: str = field(default="", repr=False)
    bff_api_key: str = field(default="", repr=False)
    jwt_expiration_seconds: int = 900
    environment: str = "development"

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(dotenv_path=ENV_FILE, override=False)
        return cls(
            environment=os.getenv("APP_ENV", "development"),
            monzo_client_id=os.getenv("MONZO_CLIENT_ID", ""),
            monzo_client_secret=os.getenv("MONZO_CLIENT_SECRET", ""),
            monzo_redirect_uri=os.getenv(
                "MONZO_REDIRECT_URI", "http://127.0.0.1:8000/monzo-callback"
            ),
            database_url=os.getenv("DATABASE_URL", "sqlite:///./monzo_scheduler.db"),
            jwt_secret_key=os.getenv("JWT_SECRET_KEY", ""),
            token_encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY", ""),
            bff_api_key=os.getenv("BFF_API_KEY", ""),
            jwt_expiration_seconds=int(os.getenv("JWT_EXPIRATION_SECONDS", "900")),
        )


def validate_settings(settings: Settings) -> None:
    """Validate startup requirements before constructing runtime resources."""
    if not settings.token_encryption_key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    try:
        Fernet(settings.token_encryption_key.encode())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc
    if len(settings.jwt_secret_key.encode()) < 32:
        raise RuntimeError("JWT_SECRET_KEY must contain at least 32 bytes")
    if len(settings.bff_api_key.encode()) < 32:
        raise RuntimeError("BFF_API_KEY must contain at least 32 bytes")
    if not 1 <= settings.jwt_expiration_seconds <= 3600:
        raise RuntimeError("JWT_EXPIRATION_SECONDS must be between 1 and 3600")
    if settings.environment not in {"development", "production"}:
        raise RuntimeError("APP_ENV must be development or production")
    if settings.environment == "production":
        if urlparse(settings.monzo_redirect_uri).scheme != "https":
            raise RuntimeError("MONZO_REDIRECT_URI must use HTTPS in production")
        if settings.jwt_secret_key.startswith(("replace-", "your-", "test-")):
            raise RuntimeError(
                "Production secrets must not be placeholders or test credentials"
            )
        if (
            len(
                {
                    settings.jwt_secret_key,
                    settings.token_encryption_key,
                    settings.bff_api_key,
                }
            ) != 3
        ):
            raise RuntimeError("Signing, encryption, and BFF keys must be distinct")
        if base64.urlsafe_b64decode(settings.token_encryption_key) in {
            b"0" * 32,
            b"\0" * 32,
        }:
            raise RuntimeError("Production encryption key must not be a test key")
