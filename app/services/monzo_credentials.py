"""Resolve and refresh encrypted provider credentials with per-user serialization."""

import asyncio
from datetime import datetime, timedelta, timezone
from threading import Lock
from weakref import WeakValueDictionary

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.models import MonzoCredential
from app.db.session import SessionFactory
from app.domain.errors import (
    MonzoConnectionError,
    MonzoTokenResponseError,
    TokenStorageError,
)
from app.domain.time import as_utc as _as_utc
from app.observability import get_logger, monzo_error_details
from app.services.monzo import MonzoClient, monzo_client_scope
from app.services.token_crypto import decrypt_token, encrypt_token

logger = get_logger(__name__)
_refresh_locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()
_refresh_locks_guard = Lock()


async def resolve_monzo_access_token(
    user_id: str,
    session_factory: SessionFactory,
    settings: Settings,
    *,
    client: MonzoClient | None = None,
) -> str:
    try:
        with session_factory() as session:
            credential = session.get(MonzoCredential, user_id)
            if credential is None or credential.disconnected:
                raise MonzoConnectionError
            access_token = decrypt_token(credential.access_token, settings)
            expires_at = credential.expires_at
    except SQLAlchemyError:
        raise TokenStorageError("Token storage is unavailable") from None

    if _as_utc(expires_at) > datetime.now(timezone.utc):
        return access_token

    lock = monzo_refresh_lock(user_id)
    await asyncio.to_thread(lock.acquire)
    try:
        async with monzo_client_scope(client) as scoped_client:
            return await _refresh_access_token_locked(
                user_id, session_factory, settings, scoped_client
            )
    finally:
        lock.release()


async def _refresh_access_token_locked(
    user_id: str,
    session_factory: SessionFactory,
    settings: Settings,
    client: MonzoClient,
) -> str:
    try:
        with session_factory() as session:
            credential = session.get(MonzoCredential, user_id)
            if credential is None or credential.disconnected:
                raise MonzoConnectionError
            access_token = decrypt_token(credential.access_token, settings)
            refresh_token = decrypt_token(credential.refresh_token, settings)
            expires_at = credential.expires_at
    except SQLAlchemyError:
        raise TokenStorageError("Token storage is unavailable") from None

    if _as_utc(expires_at) > datetime.now(timezone.utc):
        return access_token
    if not refresh_token:
        raise MonzoConnectionError
    if not settings.monzo_client_id or not settings.monzo_client_secret:
        raise TokenStorageError("Monzo OAuth is not configured")

    try:
        refreshed = await client.refresh_access_token(refresh_token, settings)
    except httpx.HTTPStatusError as exc:
        error_code, error_message = monzo_error_details(exc.response)
        logger.warning(
            "monzo_token_refresh_exception upstream_status=%d "
            "monzo_code=%r monzo_message=%r",
            exc.response.status_code,
            error_code,
            error_message,
        )
        if exc.response.status_code in {400, 401, 403}:
            raise MonzoConnectionError from exc
        raise
    except (ValidationError, ValueError) as exc:
        raise MonzoTokenResponseError from exc

    if refreshed.user_id != user_id:
        raise MonzoTokenResponseError

    now = datetime.now(timezone.utc)
    try:
        with session_factory() as session:
            credential = session.get(MonzoCredential, user_id)
            if credential is None:
                raise MonzoConnectionError
            credential.access_token = encrypt_token(refreshed.access_token, settings)
            credential.refresh_token = encrypt_token(
                refreshed.refresh_token or refresh_token, settings
            )
            credential.token_type = refreshed.token_type
            credential.expires_at = now + timedelta(seconds=refreshed.expires_in)
            credential.updated_at = now
            disconnected = credential.disconnected
            session.commit()
    except SQLAlchemyError:
        raise TokenStorageError("Token storage is unavailable") from None

    if disconnected:
        raise MonzoConnectionError
    return refreshed.access_token


def monzo_refresh_lock(user_id: str) -> Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(user_id, Lock())
