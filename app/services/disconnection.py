"""Revoke Monzo grants, retaining retry state when the provider is unavailable."""

import asyncio
from datetime import datetime, timedelta, timezone
import httpx
from sqlalchemy import select

from app.db.models import MonzoCredential
from app.observability import get_logger
from app.services.authorization import monzo_refresh_lock
from app.services.monzo import MonzoClient, monzo_client_scope
from app.services.token_store import encrypt_token, decrypt_token
from app.services.user_locks import user_execution_lock

logger = get_logger(__name__)


def retry_monzo_disconnection(user_id, session_factory, settings):
    with user_execution_lock(user_id), monzo_refresh_lock(user_id):
        try:
            asyncio.run(_revoke(user_id, session_factory, settings))
        except Exception as exc:
            logger.warning(
                "monzo_disconnection_pending exception_type=%s", type(exc).__name__
            )
            return False
        return True


async def _revoke(user_id, session_factory, settings):
    async with monzo_client_scope() as client:
        await _revoke_with_client(user_id, session_factory, settings, client)


async def _revoke_with_client(user_id, session_factory, settings, client: MonzoClient):
    with session_factory() as session:
        credential = session.get(MonzoCredential, user_id)
        if credential is None or not credential.revocation_pending:
            return
        access = decrypt_token(credential.access_token, settings)
        refresh = decrypt_token(credential.refresh_token, settings)
        expired = credential.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(
            timezone.utc
        )

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
        credential.revocation_pending = False
        session.commit()


def retry_pending_disconnections(session_factory, settings):
    with session_factory() as session:
        users = session.scalars(
            select(MonzoCredential.user_id).where(
                MonzoCredential.revocation_pending.is_(True)
            )
        ).all()
    for user_id in users:
        retry_monzo_disconnection(user_id, session_factory, settings)
