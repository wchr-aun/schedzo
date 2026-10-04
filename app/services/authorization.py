"""Validate application sessions independently of Monzo provider credentials."""

from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.models import AppSession, MonzoCredential
from app.db.session import SessionFactory
from app.domain.authentication import AuthenticationContext
from app.domain.errors import SessionAuthenticationError, TokenStorageError
from app.domain.time import as_utc
from app.services.token_crypto import decode_access_claims


def authenticate_session(
    token: str, settings: Settings, session_factory: SessionFactory
) -> AuthenticationContext:
    if not settings.jwt_secret_key:
        raise TokenStorageError("Session signing is not configured")

    claims = decode_access_claims(token, settings)

    user_id = claims["sub"]
    if not isinstance(user_id, str) or not user_id:
        raise SessionAuthenticationError
    session_id = claims.get("sid")
    try:
        with session_factory() as session:
            credential = session.get(MonzoCredential, user_id)
            if credential is not None and claims["ver"] != credential.session_version:
                raise SessionAuthenticationError
            if session_id is not None:
                app_session = session.get(AppSession, session_id)
                if (
                    app_session is None
                    or app_session.user_id != user_id
                    or app_session.revoked_at is not None
                    or as_utc(app_session.expires_at) <= datetime.now(timezone.utc)
                    or app_session.session_version != claims["ver"]
                ):
                    raise SessionAuthenticationError
    except SQLAlchemyError:
        raise TokenStorageError("Token storage is unavailable") from None
    return AuthenticationContext(
        user_id, token, session_id if isinstance(session_id, str) else None
    )


def decode_user_id(
    token: str, settings: Settings, session_factory: SessionFactory
) -> str:
    """Revalidate persisted authorization, including when called inside a mutation lock."""
    return authenticate_session(token, settings, session_factory).user_id
