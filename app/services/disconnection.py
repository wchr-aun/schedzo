"""Revoke Monzo grants, retaining retry state when the provider is unavailable."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db.models import MonzoCredential
from app.db.session import SessionFactory
from app.domain.authentication import AuthenticationContext
from app.domain.connection import ConnectionStatus
from app.domain.scheduling import TransferJobs
from app.domain.time import as_utc
from app.observability import get_logger
from app.services.monzo import MonzoClient, monzo_client_scope
from app.services.monzo_credentials import monzo_refresh_lock
from app.services.schedules import emergency_stop_user_transfers
from app.services.token_crypto import decrypt_token, encrypt_token
from app.services.user_locks import user_execution_lock

logger = get_logger(__name__)


def retry_monzo_disconnection(
    user_id: str, session_factory: SessionFactory, settings: Settings
) -> bool:
    with user_execution_lock(user_id), monzo_refresh_lock(user_id):
        try:
            asyncio.run(_revoke(user_id, session_factory, settings))
        except Exception as exc:
            logger.warning(
                "monzo_disconnection_pending exception_type=%s", type(exc).__name__
            )
            return False
        return True


async def _revoke(
    user_id: str, session_factory: SessionFactory, settings: Settings
) -> None:
    async with monzo_client_scope() as client:
        await _revoke_with_client(user_id, session_factory, settings, client)


async def _revoke_with_client(
    user_id: str,
    session_factory: SessionFactory,
    settings: Settings,
    client: MonzoClient,
) -> None:
    with session_factory() as session:
        credential = session.get(MonzoCredential, user_id)
        if (
            credential is None
            or credential.connection_status != ConnectionStatus.REVOCATION_PENDING
        ):
            return
        access = decrypt_token(credential.access_token, settings)
        refresh = decrypt_token(credential.refresh_token, settings)
        expired = as_utc(credential.expires_at) <= datetime.now(timezone.utc)

    async def renew():
        if not settings.monzo_client_id or not settings.monzo_client_secret:
            raise RuntimeError("Monzo OAuth is not configured")
        try:
            result = await client.refresh_access_token(refresh, settings)
        except httpx.HTTPStatusError as exc:
            try:
                invalid_grant = (
                    exc.response.status_code == 400
                    and exc.response.json().get("error") == "invalid_grant"
                )
            except ValueError, AttributeError:
                invalid_grant = False
            if invalid_grant:
                return None
            raise
        if result.user_id != user_id:
            raise ValueError("Invalid refresh identity")
        with session_factory() as session:
            credential = session.get(MonzoCredential, user_id)
            credential.access_token = encrypt_token(result.access_token, settings)
            credential.refresh_token = encrypt_token(
                result.refresh_token or refresh, settings
            )
            credential.expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=result.expires_in
            )
            session.commit()
        return result.access_token

    if expired and refresh:
        access = await renew() or access
    try:
        await client.revoke_access(access)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        if refresh:
            renewed = await renew()
            if renewed is not None:
                await client.revoke_access(renewed)
        # Without a refresh credential, a rejected access token has no further
        # stored capability. With refresh, require confirmed provider revocation.
    with session_factory() as session:
        credential = session.get(MonzoCredential, user_id)
        credential.access_token = ""
        credential.refresh_token = None
        credential.expires_at = datetime.now(timezone.utc)
        credential.connection_status = ConnectionStatus.DISCONNECTED
        session.commit()


def retry_pending_disconnections(
    session_factory: SessionFactory, settings: Settings
) -> None:
    with session_factory() as session:
        users = session.scalars(
            select(MonzoCredential.user_id).where(
                MonzoCredential.connection_status
                == ConnectionStatus.REVOCATION_PENDING
            )
        ).all()
    for user_id in users:
        retry_monzo_disconnection(user_id, session_factory, settings)


def disconnect_user(
    scheduler: TransferJobs,
    session_factory: SessionFactory,
    settings: Settings,
    authentication: AuthenticationContext,
) -> bool:
    """Persist scheduling/session revocation before attempting provider disconnection."""
    emergency_stop_user_transfers(
        scheduler,
        session_factory,
        authentication.user_id,
        session_token=authentication.session_token,
        settings=settings,
        disconnect=True,
    )
    return retry_monzo_disconnection(authentication.user_id, session_factory, settings)
