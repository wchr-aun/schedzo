"""Token encryption, signing, and hashing without persistence or session policy."""

from datetime import datetime, timedelta
from hashlib import sha256
from typing import overload

import jwt
from cryptography.fernet import Fernet

from app.config import Settings
from app.domain.errors import AccessTokenExpiredError, SessionAuthenticationError


def hash_refresh_token(refresh_token: str) -> str:
    return sha256(refresh_token.encode()).hexdigest()


def encode_access_token(
    user_id: str,
    session_version: int,
    session_id: str,
    settings: Settings,
    now: datetime,
) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "ver": session_version,
            "sid": session_id,
            "iat": now,
            "exp": now + timedelta(seconds=settings.jwt_expiration_seconds),
        },
        settings.jwt_secret_key,
        algorithm="HS256",
    )


@overload
def encrypt_token(value: str, settings: Settings) -> str: ...


@overload
def encrypt_token(value: None, settings: Settings) -> None: ...


def encrypt_token(value: str | None, settings: Settings) -> str | None:
    if value is None:
        return None
    return (
        Fernet(settings.token_encryption_key.encode()).encrypt(value.encode()).decode()
    )


@overload
def decrypt_token(value: str, settings: Settings) -> str: ...


@overload
def decrypt_token(value: None, settings: Settings) -> None: ...


def decrypt_token(value: str | None, settings: Settings) -> str | None:
    if value is None:
        return None
    return (
        Fernet(settings.token_encryption_key.encode()).decrypt(value.encode()).decode()
    )


def decode_access_claims(token: str, settings: Settings) -> dict:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=["HS256"],
            options={"require": ["sub", "exp", "ver"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AccessTokenExpiredError from exc
    except jwt.InvalidTokenError as exc:
        raise SessionAuthenticationError from exc
