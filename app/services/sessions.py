"""Application session issuance, logout, and refresh rotation workflows."""

from datetime import datetime, timedelta, timezone
import secrets
from threading import Lock
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import AppSession, MonzoCredential, UsedAppRefreshToken
from app.db.session import SessionFactory
from app.db.session_queries import (
    count_issued_sessions,
    count_refreshes,
    find_refresh_identity,
)
from app.domain.authentication import AuthenticationContext
from app.domain.errors import (
    AppSessionQuotaError,
    MonzoDisconnectPendingError,
    SessionAuthenticationError,
)
from app.domain.monzo import MonzoTokenResponse
from app.domain.sessions import AppTokenPair
from app.domain.time import as_utc
from app.services.authorization import authenticate_session
from app.services.user_locks import user_execution_lock
from app.services.refresh_replay import refresh_replay_cache
from app.services.token_crypto import (
    encrypt_token,
    encode_access_token,
    hash_refresh_token,
)

APP_REFRESH_TOKEN_TTL = timedelta(days=60)
MAX_APP_SESSIONS_PER_USER_PER_DAY = 20
MAX_REFRESHES_PER_USER_PER_HOUR = 60
_REFRESH_LOCKS = tuple(Lock() for _ in range(32))


def issue_app_session(
    token_response: MonzoTokenResponse,
    session_factory: SessionFactory,
    settings: Settings,
) -> AppTokenPair:
    """Persist provider credentials and issue an application session atomically."""
    with user_execution_lock(token_response.user_id), session_factory() as session:
        with session.begin():
            return _save_monzo_tokens(token_response, session, settings)


def _save_monzo_tokens(
    token_response: MonzoTokenResponse, session: Session, settings: Settings
) -> AppTokenPair:
    if not settings.jwt_secret_key:
        raise ValueError("JWT_SECRET_KEY is not configured")

    now = datetime.now(timezone.utc)
    issued_today = count_issued_sessions(
        session, token_response.user_id, now - timedelta(days=1)
    )
    if issued_today >= MAX_APP_SESSIONS_PER_USER_PER_DAY:
        raise AppSessionQuotaError
    credential = session.get(MonzoCredential, token_response.user_id)
    if credential is not None and credential.revocation_pending:
        raise MonzoDisconnectPendingError
    if credential is None:
        credential = MonzoCredential(user_id=token_response.user_id, session_version=1)
        session.add(credential)

    credential.disconnected = False
    credential.access_token = encrypt_token(token_response.access_token, settings)
    credential.refresh_token = encrypt_token(token_response.refresh_token, settings)
    credential.token_type = token_response.token_type
    # Keep existing sessions valid on login. The version is advanced only
    # when an operation explicitly invalidates every session for this user.
    credential.expires_at = now + timedelta(seconds=token_response.expires_in)
    credential.updated_at = now
    # Models use scalar foreign keys rather than ORM relationships; persist
    # the parent before adding the child when SQLite constraints are enabled.
    session.flush()
    return _create_app_session(credential, session, settings, now)


def _create_app_session(
    credential: MonzoCredential,
    session: Session,
    settings: Settings,
    now: datetime,
) -> AppTokenPair:
    session_id = str(uuid4())
    refresh_token = secrets.token_urlsafe(48)
    app_session = AppSession(
        session_id=session_id,
        user_id=credential.user_id,
        session_version=credential.session_version,
        refresh_token_hash=hash_refresh_token(refresh_token),
        expires_at=now + APP_REFRESH_TOKEN_TTL,
        created_at=now,
        updated_at=now,
    )
    session.add(app_session)
    return AppTokenPair(
        access_token=encode_access_token(
            credential.user_id,
            credential.session_version,
            session_id,
            settings,
            now,
        ),
        refresh_token=refresh_token,
        expires_in=settings.jwt_expiration_seconds,
        refresh_expires_in=int(APP_REFRESH_TOKEN_TTL.total_seconds()),
    )


def logout_session(
    authentication: AuthenticationContext,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    with user_execution_lock(authentication.user_id):
        current = authenticate_session(
            authentication.session_token, settings, session_factory
        )
        if current.user_id != authentication.user_id:
            raise SessionAuthenticationError
        with session_factory() as session, session.begin():
            if current.app_session_id is not None:
                app_session = session.get(AppSession, current.app_session_id)
                if app_session is not None and app_session.user_id == current.user_id:
                    app_session.revoked_at = datetime.now(timezone.utc)
            else:
                # Legacy access JWTs have no per-session identifier.
                credential = session.get(MonzoCredential, current.user_id)
                if credential is not None:
                    credential.session_version += 1


def rotate_app_refresh_token(
    refresh_token: str, session_factory: SessionFactory, settings: Settings
) -> AppTokenPair | None:
    """Rotate once; briefly replay the same result for immediate duplicates."""
    token_hash = hash_refresh_token(refresh_token)
    # Bounded locks serialize duplicate submissions without a global request lock.
    with _REFRESH_LOCKS[int(token_hash[:8], 16) % len(_REFRESH_LOCKS)]:
        with session_factory() as session:
            identity = find_refresh_identity(session, token_hash)
            if identity is None:
                return None
            user_id, session_id = identity
        with user_execution_lock(user_id):
            return _rotate_app_refresh_token_locked(
                token_hash, session_id, session_factory, settings
            )


def _rotate_app_refresh_token_locked(
    token_hash: str,
    session_id: str,
    session_factory: SessionFactory,
    settings: Settings,
) -> AppTokenPair | None:
    now = datetime.now(timezone.utc)
    cache = refresh_replay_cache(session_factory)
    with session_factory() as session:
        app_session = (
            session.query(AppSession)
            .filter_by(session_id=session_id)
            .with_for_update()
            .one_or_none()
        )
        if (
            app_session is None
            or app_session.revoked_at is not None
            or as_utc(app_session.expires_at) <= now
        ):
            cache.discard(session_id)
            return None
        credential = session.get(MonzoCredential, app_session.user_id)
        if (
            credential is None
            or credential.session_version != app_session.session_version
        ):
            cache.discard(session_id)
            return None
        if session.get(UsedAppRefreshToken, token_hash) is not None:
            cached = cache.lookup(
                session_id, token_hash, app_session.refresh_token_hash
            )
            if cached is not None:
                return cached
            app_session.revoked_at = now
            session.commit()
            cache.discard(session_id)
            return None
        if app_session.refresh_token_hash != token_hash:
            return None
        recent_count = count_refreshes(
            session, credential.user_id, now - timedelta(hours=1)
        )
        if recent_count >= MAX_REFRESHES_PER_USER_PER_HOUR:
            raise AppSessionQuotaError
        next_token = secrets.token_urlsafe(48)
        result = session.execute(
            update(AppSession)
            .execution_options(synchronize_session=False)
            .where(
                AppSession.session_id == app_session.session_id,
                AppSession.refresh_token_hash == token_hash,
                AppSession.revoked_at.is_(None),
                AppSession.expires_at > now,
            )
            .values(
                refresh_token_hash=hash_refresh_token(next_token),
                updated_at=now,
                expires_at=now + APP_REFRESH_TOKEN_TTL,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        session.add(
            UsedAppRefreshToken(
                token_hash=token_hash, session_id=session_id, used_at=now
            )
        )
        pair = AppTokenPair(
            access_token=encode_access_token(
                credential.user_id,
                credential.session_version,
                session_id,
                settings,
                now,
            ),
            refresh_token=next_token,
            expires_in=settings.jwt_expiration_seconds,
            refresh_expires_in=int(APP_REFRESH_TOKEN_TTL.total_seconds()),
        )
        session.commit()
        # JWT NumericDate truncates fractional seconds. Do not replay an access
        # token after its actual expiry, even when a very short TTL is configured.
        access_deadline = int(
            (now + timedelta(seconds=settings.jwt_expiration_seconds)).timestamp()
        )
        cache.record(
            session_id,
            token_hash,
            hash_refresh_token(next_token),
            pair,
            access_deadline - datetime.now(timezone.utc).timestamp(),
        )
        return pair
