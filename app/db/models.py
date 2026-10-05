from datetime import date, datetime, timezone
from uuid import uuid4, uuid6

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.connection import ConnectionStatus


class Base(DeclarativeBase):
    pass


class MonzoCredential(Base):
    __tablename__ = "monzo_credentials"
    __table_args__ = (
        CheckConstraint(
            "connection_status IN ('connected', 'revocation_pending', 'disconnected')",
            name="ck_monzo_credentials_connection_status",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    access_token: Mapped[str] = mapped_column(
        "access_token_ciphertext", String, nullable=False
    )
    refresh_token: Mapped[str | None] = mapped_column(
        "refresh_token_ciphertext", String, nullable=True
    )
    token_type: Mapped[str] = mapped_column(String(32), nullable=False)
    session_version: Mapped[int] = mapped_column(default=0, nullable=False)
    connection_status: Mapped[str] = mapped_column(
        String(32),
        default=ConnectionStatus.CONNECTED,
        server_default=ConnectionStatus.CONNECTED,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class AppSession(Base):
    __tablename__ = "app_sessions"

    session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("monzo_credentials.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_version: Mapped[int] = mapped_column(nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UsedAppRefreshToken(Base):
    __tablename__ = "used_app_refresh_tokens"
    __table_args__ = (Index("ix_used_refresh_session_time", "session_id", "used_at"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("app_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConsumedOAuthState(Base):
    __tablename__ = "consumed_oauth_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ScheduledTransferSetup(Base):
    __tablename__ = "scheduled_transfer_setups"
    __table_args__ = (
        CheckConstraint(
            "interval IN ('daily', 'weekly', 'monthly')",
            name="ck_scheduled_transfer_setups_interval",
        ),
        CheckConstraint(
            "type IN ('withdraw', 'deposit')",
            name="ck_scheduled_transfer_setups_type",
        ),
        CheckConstraint(
            "status IN ('active', 'deactivated')",
            name="ck_scheduled_transfer_setups_status",
        ),
        CheckConstraint(
            "hour BETWEEN 0 AND 23", name="ck_scheduled_transfer_setups_hour"
        ),
        CheckConstraint(
            "minute BETWEEN 0 AND 59", name="ck_scheduled_transfer_setups_minute"
        ),
        CheckConstraint("amount > 0", name="ck_scheduled_transfer_setups_amount"),
    )

    setup_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid6())
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("monzo_credentials.user_id"), nullable=False, index=True
    )
    scheduled_date: Mapped[date] = mapped_column("date", Date, nullable=False)
    hour: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    minute: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    transfer_type: Mapped[str] = mapped_column("type", String(16), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pot_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ScheduledTransfer(Base):
    __tablename__ = "scheduled_transfers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_scheduled_transfers_status",
        ),
    )

    transfer_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid6())
    )
    setup_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_transfer_setups.setup_id"), nullable=False, index=True
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
