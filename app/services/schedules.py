"""Application workflows for scheduling and cancelling transfers."""

from datetime import datetime, timedelta, timezone
from uuid import uuid6

from sqlalchemy import func, select

from app.config import Settings
from app.db.models import MonzoCredential, ScheduledTransfer, ScheduledTransferSetup
from app.db.session import SessionFactory
from app.domain.connection import ConnectionStatus
from app.domain.errors import SessionAuthenticationError
from app.domain.scheduling import TransferJobs
from app.domain.time import UK_TIMEZONE
from app.domain.time import as_utc as _as_utc
from app.domain.transfers import (
    InvalidScheduleError,
    ScheduledTransferDetails,
    ScheduledTransfersPage,
    ScheduleNotFoundError,
    ScheduleQuotaExceededError,
    ScheduleTransferCommand,
    TransferInterval,
    TransferStatus,
    TransferType,
)
from app.services.authorization import decode_user_id
from app.services.user_locks import user_execution_lock

MAX_ACTIVE_SCHEDULES_PER_USER = 50
MAX_SCHEDULE_CREATIONS_PER_USER_PER_DAY = 100


def list_scheduled_transfers(
    session_factory: SessionFactory,
    user_id: str,
    *,
    statuses: tuple[TransferStatus, ...],
    limit: int,
    offset: int,
    account_id: str | None = None,
    pot_id: str | None = None,
) -> ScheduledTransfersPage:
    """Return a filtered page of the authenticated user's transfers."""
    with session_factory() as session:
        filters = [
            ScheduledTransferSetup.user_id == user_id,
            ScheduledTransfer.status.in_(statuses),
        ]
        if account_id is not None:
            filters.append(ScheduledTransferSetup.account_id == account_id)
        if pot_id is not None:
            filters.append(ScheduledTransferSetup.pot_id == pot_id)
        total = session.scalar(
            select(func.count())
            .select_from(ScheduledTransfer)
            .join(
                ScheduledTransferSetup,
                ScheduledTransfer.setup_id == ScheduledTransferSetup.setup_id,
            )
            .where(*filters)
        )
        rows = session.execute(
            select(ScheduledTransfer, ScheduledTransferSetup)
            .join(
                ScheduledTransferSetup,
                ScheduledTransfer.setup_id == ScheduledTransferSetup.setup_id,
            )
            .where(*filters)
            .order_by(
                ScheduledTransfer.created_at.desc(),
                ScheduledTransfer.transfer_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()

        return ScheduledTransfersPage(
            items=[_transfer_details(setup, transfer) for transfer, setup in rows],
            total=total or 0,
            limit=limit,
            offset=offset,
        )


def _transfer_details(
    setup: ScheduledTransferSetup, transfer: ScheduledTransfer
) -> ScheduledTransferDetails:
    return ScheduledTransferDetails(
        setup_id=setup.setup_id,
        transfer_id=transfer.transfer_id,
        scheduled_for=_as_utc(transfer.scheduled_for).astimezone(UK_TIMEZONE),
        created_at=_as_utc(transfer.created_at).astimezone(UK_TIMEZONE),
        interval=TransferInterval(setup.interval),
        transfer_type=TransferType(setup.transfer_type),
        amount=setup.amount,
        setup_status=setup.status,
        status=transfer.status,
        executed_at=(
            _as_utc(transfer.executed_at).astimezone(UK_TIMEZONE)
            if transfer.executed_at is not None
            else None
        ),
    )


def _schedule_transfer_unlocked(
    scheduler: TransferJobs,
    session_factory: SessionFactory,
    settings: Settings,
    user_id: str,
    command: ScheduleTransferCommand,
    *,
    now: datetime | None = None,
) -> ScheduledTransferDetails:
    scheduled_at = command.scheduled_for.astimezone(UK_TIMEZONE)
    current_time = (now or datetime.now(timezone.utc)).astimezone(UK_TIMEZONE)
    if scheduled_at <= current_time:
        raise InvalidScheduleError("datetime must be in the future")

    with session_factory() as session:
        created_today = session.scalar(
            select(func.count())
            .select_from(ScheduledTransferSetup)
            .where(
                ScheduledTransferSetup.user_id == user_id,
                ScheduledTransferSetup.created_at
                >= current_time.astimezone(timezone.utc) - timedelta(days=1),
            )
        )
        if created_today >= MAX_SCHEDULE_CREATIONS_PER_USER_PER_DAY:
            raise ScheduleQuotaExceededError
        active_count = session.scalar(
            select(func.count())
            .select_from(ScheduledTransferSetup)
            .where(
                ScheduledTransferSetup.user_id == user_id,
                ScheduledTransferSetup.status == "active",
            )
        )
    if active_count >= MAX_ACTIVE_SCHEDULES_PER_USER:
        raise ScheduleQuotaExceededError

    setup_id = str(uuid6())
    setup = ScheduledTransferSetup(
        setup_id=setup_id,
        user_id=user_id,
        scheduled_date=scheduled_at.date(),
        hour=scheduled_at.hour,
        minute=scheduled_at.minute,
        interval=command.interval.value,
        transfer_type=command.transfer_type.value,
        amount=command.amount,
        pot_id=command.pot_id,
        account_id=command.account_id,
        status="active",
    )
    transfer = ScheduledTransfer(
        setup_id=setup_id,
        scheduled_for=scheduled_at.astimezone(timezone.utc),
        status="pending",
    )
    job_registered = False

    try:
        with session_factory() as session:
            session.add(setup)
            session.flush()
            session.add(transfer)
            session.flush()
            scheduler.schedule(
                transfer.transfer_id,
                transfer.scheduled_for,
                replace_existing=False,
            )
            job_registered = True
            result = _transfer_details(setup, transfer)
            session.commit()
    except Exception:
        if job_registered:
            scheduler.remove(transfer.transfer_id)
        raise

    return result


def schedule_transfer(
    scheduler: TransferJobs,
    session_factory: SessionFactory,
    settings: Settings,
    user_id: str,
    command: ScheduleTransferCommand,
    *,
    now: datetime | None = None,
    session_token: str | None = None,
) -> ScheduledTransferDetails:
    lock = user_execution_lock(user_id)
    lock.acquire()
    try:
        if (
            session_token is not None
            and decode_user_id(session_token, settings, session_factory) != user_id
        ):
            raise SessionAuthenticationError
        return _schedule_transfer_unlocked(
            scheduler, session_factory, settings, user_id, command, now=now
        )
    finally:
        lock.release()


def cancel_scheduled_transfer(
    scheduler: TransferJobs,
    session_factory: SessionFactory,
    user_id: str,
    setup_id: str,
) -> None:
    lock = user_execution_lock(user_id)
    lock.acquire()
    try:
        return _cancel_scheduled_transfer_locked(
            scheduler, session_factory, user_id, setup_id
        )
    finally:
        lock.release()


def _cancel_scheduled_transfer_locked(
    scheduler: TransferJobs,
    session_factory: SessionFactory,
    user_id: str,
    setup_id: str,
) -> None:
    with session_factory() as session:
        setup = session.get(ScheduledTransferSetup, setup_id)
        if setup is None or setup.user_id != user_id:
            raise ScheduleNotFoundError

        pending = session.scalars(
            select(ScheduledTransfer).where(
                ScheduledTransfer.setup_id == setup_id,
                ScheduledTransfer.status == "pending",
            )
        ).all()
        setup.status = "deactivated"
        for transfer in pending:
            transfer.status = "cancelled"
        transfer_ids = [transfer.transfer_id for transfer in pending]
        session.commit()

    for transfer_id in transfer_ids:
        scheduler.remove(transfer_id)
    return None


def disconnect_user_schedules(
    scheduler: TransferJobs,
    session_factory: SessionFactory,
    user_id: str,
    *,
    session_token: str,
    settings: Settings,
) -> int:
    """Revoke sessions and deactivate schedules while preparing to disconnect."""
    lock = user_execution_lock(user_id)
    lock.acquire()
    try:
        if decode_user_id(session_token, settings, session_factory) != user_id:
            raise SessionAuthenticationError
        return _disconnect_user_schedules_locked(scheduler, session_factory, user_id)
    finally:
        lock.release()


def _disconnect_user_schedules_locked(
    scheduler: TransferJobs,
    session_factory: SessionFactory,
    user_id: str,
) -> int:
    with session_factory() as session:
        setups = session.scalars(
            select(ScheduledTransferSetup).where(
                ScheduledTransferSetup.user_id == user_id,
                ScheduledTransferSetup.status == "active",
            )
        ).all()
        setup_ids = [setup.setup_id for setup in setups]
        transfer_ids: list[str] = []
        if setup_ids:
            for setup in setups:
                setup.status = "deactivated"
            pending = session.scalars(
                select(ScheduledTransfer).where(
                    ScheduledTransfer.setup_id.in_(setup_ids),
                    ScheduledTransfer.status == "pending",
                )
            ).all()
            for transfer in pending:
                transfer.status = "cancelled"
                transfer_ids.append(transfer.transfer_id)
        credential = session.get(MonzoCredential, user_id)
        if credential is not None:
            credential.session_version = (credential.session_version or 0) + 1
            credential.connection_status = ConnectionStatus.REVOCATION_PENDING
        session.commit()

    for transfer_id in transfer_ids:
        scheduler.remove(transfer_id)
    return len(transfer_ids)
