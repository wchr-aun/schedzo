"""Remove obsolete authentication records while preserving transfer history."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select

from app.db.models import (
    AppSession,
    ConsumedOAuthState,
    UsedAppRefreshToken,
)
from app.db.session import SessionFactory


def prune_history(session_factory: SessionFactory) -> None:
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        expired_sessions = select(AppSession.session_id).where(
            or_(
                AppSession.revoked_at <= now - timedelta(days=1),
                AppSession.expires_at <= now,
            ),
        )
        session.execute(
            delete(UsedAppRefreshToken).where(
                UsedAppRefreshToken.session_id.in_(expired_sessions)
            )
        )
        session.execute(
            delete(AppSession).where(AppSession.session_id.in_(expired_sessions))
        )
        session.execute(
            delete(ConsumedOAuthState).where(ConsumedOAuthState.expires_at <= now)
        )
        session.commit()
