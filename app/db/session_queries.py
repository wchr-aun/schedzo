"""Session queries; the calling workflow owns the transaction."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AppSession, UsedAppRefreshToken


def count_issued_sessions(session: Session, user_id: str, since: datetime) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(AppSession)
            .where(
                AppSession.user_id == user_id,
                AppSession.created_at >= since,
            )
        )
        or 0
    )


def count_refreshes(session: Session, user_id: str, since: datetime) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(UsedAppRefreshToken)
            .join(AppSession)
            .where(
                AppSession.user_id == user_id,
                UsedAppRefreshToken.used_at >= since,
            )
        )
        or 0
    )


def find_refresh_identity(session: Session, token_hash: str) -> tuple[str, str] | None:
    current = session.scalar(
        select(AppSession).where(AppSession.refresh_token_hash == token_hash)
    )
    if current is None:
        used = session.get(UsedAppRefreshToken, token_hash)
        current = session.get(AppSession, used.session_id) if used is not None else None
    return (current.user_id, current.session_id) if current is not None else None
