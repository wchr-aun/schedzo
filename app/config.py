"""Application configuration loaded from environment variables."""

import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

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

    otel_enabled: bool = False
    otel_service_name: str = "schedzo"
    otel_endpoint: str = ""
    otel_headers: str = field(default="", repr=False)
    otel_trace_sample_ratio: float = 1.0

    @classmethod
    def from_environment(cls) -> Settings:
        load_dotenv(dotenv_path=ENV_FILE, override=False)
        return cls(
            otel_enabled=os.getenv("OTEL_ENABLED", "false").lower() == "true",
            otel_service_name=os.getenv("OTEL_SERVICE_NAME", "schedzo"),
            otel_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
            otel_headers=os.getenv("OTEL_EXPORTER_OTLP_HEADERS", ""),
            otel_trace_sample_ratio=float(os.getenv("OTEL_TRACE_SAMPLE_RATIO", "1.0")),
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
            )
            != 3
        ):
            raise RuntimeError("Signing, encryption, and BFF keys must be distinct")
        if base64.urlsafe_b64decode(settings.token_encryption_key) in {
            b"0" * 32,
            b"\0" * 32,
        }:
            raise RuntimeError("Production encryption key must not be a test key")

    if settings.otel_enabled:
        endpoint = urlparse(settings.otel_endpoint)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise RuntimeError(
                "Telemetry requires an HTTPS OTLP base endpoint without credentials or query parameters"
            )
        if (
            not settings.otel_headers
            or any(
                "=" not in item
                or not item.split("=", 1)[0].strip()
                or not item.split("=", 1)[1].strip()
                for item in settings.otel_headers.split(",")
            )
            or "\n" in settings.otel_headers
            or "\r" in settings.otel_headers
        ):
            raise RuntimeError(
                "OTEL_EXPORTER_OTLP_HEADERS must contain nonempty key=value entries"
            )
        for entry in settings.otel_headers.split(","):
            name, value = entry.split("=", 1)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name.strip()) or any(
                ord(char) < 32 or ord(char) == 127 for char in unquote(value)
            ):
                raise RuntimeError("OTLP header names or values are invalid")
        if not 0 <= settings.otel_trace_sample_ratio <= 1:
            raise RuntimeError("OTEL_TRACE_SAMPLE_RATIO must be between 0 and 1")
        if not settings.otel_service_name or len(settings.otel_service_name) > 128:
            raise RuntimeError("OTEL_SERVICE_NAME must contain 1 to 128 characters")
