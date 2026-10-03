"""Persistence, recovery, cancellation, and execution of scheduled transfers."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote
from uuid import uuid6

import httpx
from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import MonzoCredential, ScheduledTransfer, ScheduledTransferSetup
from app.observability import get_logger, monzo_error_details
from app.domain.time import UK_TIMEZONE, as_utc as _as_utc
from app.domain.recurrence import Recurrence, next_occurrence
from app.domain.transfers import (
    InvalidScheduleError,
    ScheduleNotFoundError,
    ScheduleQuotaExceededError,
    SchedulingPausedError,
    ScheduleTransferCommand,
    TransferExecution,
    ScheduledTransferDetails,
    ScheduledTransfersPage,
    TransferInterval,
    TransferStatus,
    TransferType,
)
from app.services.authorization import (
    resolve_monzo_access_token,
    decode_user_id,
    SessionAuthenticationError,
)
from app.services.user_locks import user_execution_lock
from app.services.monzo import create_feed_item, deposit_into_pot, withdraw_from_pot

logger = get_logger(__name__)

FEED_IMAGE_URL = (
    "https://raw.githubusercontent.com/wchr-aun/monzo-scheduler-ui/"
    "refs/heads/main/public/logo.png"
)
SCHEDULER_UI_URL = "https://monzo-scheduler-ui.vercel.app"


MAX_ACTIVE_SCHEDULES_PER_USER = 50
MAX_SCHEDULE_CREATIONS_PER_USER_PER_DAY = 100

_user_execution_lock = user_execution_lock


def list_scheduled_transfers(
    session_factory: sessionmaker[Session],
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
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
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
    job: Job | None = None

    try:
        with session_factory() as session:
            session.add(setup)
            session.flush()
            session.add(transfer)
            session.flush()
            job = _add_transfer_job(
                scheduler,
                transfer,
                session_factory,
                settings,
                replace_existing=False,
            )
            session.commit()
    except Exception:
        if job is not None:
            _remove_job_if_present(scheduler, job.id)
        raise

    return _transfer_details(setup, transfer)


def schedule_transfer(
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    settings: Settings,
    user_id: str,
    command: ScheduleTransferCommand,
    *,
    now: datetime | None = None,
    session_token: str | None = None,
) -> ScheduledTransferDetails:
    lock = _user_execution_lock(user_id)
    lock.acquire()
    try:
        if (
            session_token is not None
            and decode_user_id(session_token, settings, session_factory) != user_id
        ):
            raise SessionAuthenticationError
        with session_factory() as session:
            credential = session.get(MonzoCredential, user_id)
            if credential is not None and credential.scheduling_paused:
                raise SchedulingPausedError
        return _schedule_transfer_unlocked(
            scheduler, session_factory, settings, user_id, command, now=now
        )
    finally:
        lock.release()


def cancel_scheduled_transfer(
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    user_id: str,
    setup_id: str,
) -> ScheduledTransferSetup:
    lock = _user_execution_lock(user_id)
    lock.acquire()
    try:
        return _cancel_scheduled_transfer_locked(
            scheduler, session_factory, user_id, setup_id
        )
    finally:
        lock.release()


def _cancel_scheduled_transfer_locked(
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    user_id: str,
    setup_id: str,
) -> ScheduledTransferSetup:
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
        session.commit()

    for transfer in pending:
        _remove_job_if_present(scheduler, transfer.transfer_id)
    return setup


def emergency_stop_user_transfers(
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    user_id: str,
    *,
    session_token: str | None = None,
    settings: Settings | None = None,
    disconnect: bool = False,
) -> int:
    """Deactivate a user's schedules and cancel all occurrences that have not started."""
    lock = _user_execution_lock(user_id)
    lock.acquire()
    try:
        if (
            session_token is not None
            and decode_user_id(session_token, settings, session_factory) != user_id
        ):
            raise SessionAuthenticationError
        return _emergency_stop_user_transfers_locked(
            scheduler, session_factory, user_id, disconnect=disconnect
        )
    finally:
        lock.release()


def _emergency_stop_user_transfers_locked(
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    user_id: str,
    *,
    disconnect: bool = False,
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
            credential.scheduling_paused = True
            if disconnect:
                credential.disconnected = True
                credential.revocation_pending = True
        session.commit()

    for transfer_id in transfer_ids:
        _remove_job_if_present(scheduler, transfer_id)
    return len(transfer_ids)


def restore_scheduled_transfers(
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> int:
    """Restore pending occurrences, cancelling any from inactive setups."""
    with session_factory() as session:
        rows = session.execute(
            select(ScheduledTransfer, ScheduledTransferSetup)
            .join(
                ScheduledTransferSetup,
                ScheduledTransfer.setup_id == ScheduledTransferSetup.setup_id,
            )
            .where(ScheduledTransfer.status.in_(("pending", "running")))
        ).all()
        active_transfers: list[ScheduledTransfer] = []
        for transfer, setup in rows:
            if setup.status == "active":
                transfer.status = "pending"
                active_transfers.append(transfer)
            else:
                transfer.status = "cancelled"
        session.commit()

    restored = 0
    now = datetime.now(timezone.utc)
    for transfer in active_transfers:
        run_at = max(_as_utc(transfer.scheduled_for), now)
        _add_transfer_job(
            scheduler,
            transfer,
            session_factory,
            settings,
            replace_existing=True,
            run_at=run_at,
        )
        restored += 1

    logger.info("scheduled_transfers_restored count=%d", restored)
    return restored


def execute_scheduled_transfer(
    transfer_id: str,
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    """Execute one occurrence and create its setup's next occurrence."""
    user_id = _transfer_user_id(transfer_id, session_factory)
    if user_id is None:
        return
    lock = _user_execution_lock(user_id)
    lock.acquire()
    try:
        asyncio.run(
            _execute_scheduled_transfer(
                transfer_id,
                scheduler,
                session_factory,
                settings,
            )
        )
    except Exception as exc:
        logger.error(
            "scheduled_transfer_failed transfer_id=%s exception_type=%s",
            transfer_id,
            type(exc).__name__,
        )
        raise
    finally:
        lock.release()


def _transfer_user_id(
    transfer_id: str, session_factory: sessionmaker[Session]
) -> str | None:
    with session_factory() as session:
        row = session.execute(
            select(ScheduledTransferSetup.user_id)
            .join(
                ScheduledTransfer,
                ScheduledTransfer.setup_id == ScheduledTransferSetup.setup_id,
            )
            .where(ScheduledTransfer.transfer_id == transfer_id)
        ).first()
        return row[0] if row is not None else None


async def _execute_scheduled_transfer(
    transfer_id: str,
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    values = _load_pending_execution(transfer_id, session_factory)
    if values is None:
        return

    access_token: str | None = None
    try:
        access_token = await resolve_monzo_access_token(
            values.user_id, session_factory, settings
        )
        if values.transfer_type == TransferType.DEPOSIT.value:
            response = await deposit_into_pot(
                access_token,
                values.pot_id,
                values.account_id,
                values.amount,
                transfer_id,
            )
        else:
            response = await withdraw_from_pot(
                access_token,
                values.pot_id,
                values.account_id,
                values.amount,
                transfer_id,
            )

        if response.is_error:
            error_code, error_message = monzo_error_details(response)
            logger.warning(
                "scheduled_transfer_rejected transfer_id=%s upstream_status=%d "
                "monzo_code=%r monzo_message=%r",
                transfer_id,
                response.status_code,
                error_code,
                error_message,
            )
        response.raise_for_status()
    except Exception:
        _finalize_occurrence(
            transfer_id,
            "failed",
            scheduler,
            session_factory,
            settings,
        )
        if access_token is not None:
            await _notify_transfer_result(
                access_token,
                values,
                transfer_id,
                succeeded=False,
            )
        raise

    _finalize_occurrence(
        transfer_id,
        "completed",
        scheduler,
        session_factory,
        settings,
    )
    await _notify_transfer_result(
        access_token,
        values,
        transfer_id,
        succeeded=True,
        pot_name=_pot_name(response),
    )
    logger.info("scheduled_transfer_completed transfer_id=%s", transfer_id)


async def _notify_transfer_result(
    access_token: str,
    values: TransferExecution,
    transfer_id: str,
    *,
    succeeded: bool,
    pot_name: str | None = None,
) -> None:
    is_deposit = values.transfer_type == TransferType.DEPOSIT.value
    action = "deposit" if is_deposit else "withdrawal"
    past_tense_action = "deposited" if is_deposit else "withdrawn"
    amount = _format_gbp(values.amount)
    pot_name = pot_name or "Pot"
    if succeeded:
        title = f"🎉 {amount} {past_tense_action}"
    else:
        title = f"❌ {amount} {action} failed"
    body = f"Balance → {pot_name}" if is_deposit else f"{pot_name} → Balance"

    account_id = quote(values.account_id, safe="")
    pot_id = quote(values.pot_id, safe="")
    try:
        response = await create_feed_item(
            access_token,
            values.account_id,
            title=title,
            image_url=FEED_IMAGE_URL,
            body=body,
            url=(
                None
                if succeeded
                else f"{SCHEDULER_UI_URL}/account/{account_id}/pot/{pot_id}"
            ),
        )
        if response.is_error:
            error_code, error_message = monzo_error_details(response)
            logger.warning(
                "scheduled_transfer_feed_rejected transfer_id=%s "
                "upstream_status=%d monzo_code=%r monzo_message=%r",
                transfer_id,
                response.status_code,
                error_code,
                error_message,
            )
        response.raise_for_status()
    except Exception:
        logger.warning(
            "scheduled_transfer_feed_failed transfer_id=%s",
            transfer_id,
        )


def _format_gbp(amount: int) -> str:
    pounds, pence = divmod(amount, 100)
    return f"£{pounds:,}.{pence:02d}"


def _pot_name(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    return name if isinstance(name, str) and name else None


def _load_pending_execution(
    transfer_id: str,
    session_factory: sessionmaker[Session],
) -> TransferExecution | None:
    try:
        with session_factory() as session:
            claimed = session.execute(
                update(ScheduledTransfer)
                .where(
                    ScheduledTransfer.transfer_id == transfer_id,
                    ScheduledTransfer.status == "pending",
                )
                .values(status="running", executed_at=datetime.now(timezone.utc))
            )
            if claimed.rowcount != 1:
                session.rollback()
                return None
            transfer = session.get(ScheduledTransfer, transfer_id)
            if transfer is None:
                raise LookupError(f"Scheduled transfer {transfer_id} does not exist")
            setup = session.get(ScheduledTransferSetup, transfer.setup_id)
            if setup is None:
                raise LookupError(f"Setup {transfer.setup_id} does not exist")
            if transfer.status != "running":
                return None
            if setup.status != "active":
                transfer.status = "cancelled"
                session.commit()
                return None
            session.commit()
            return TransferExecution(
                setup_id=setup.setup_id,
                user_id=setup.user_id,
                transfer_type=setup.transfer_type,
                amount=setup.amount,
                pot_id=setup.pot_id,
                account_id=setup.account_id,
            )
    except SQLAlchemyError as exc:
        logger.error(
            "scheduled_transfer_storage_failed transfer_id=%s exception_type=%s",
            transfer_id,
            type(exc).__name__,
        )
        raise


def _finalize_occurrence(
    transfer_id: str,
    status: Literal["completed", "failed"],
    scheduler: BackgroundScheduler,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    next_transfer: ScheduledTransfer | None = None
    next_job: Job | None = None
    try:
        with session_factory() as session:
            transfer = session.get(ScheduledTransfer, transfer_id)
            if transfer is None:
                raise LookupError(f"Scheduled transfer {transfer_id} does not exist")
            setup = session.get(ScheduledTransferSetup, transfer.setup_id)
            if setup is None:
                raise LookupError(f"Setup {transfer.setup_id} does not exist")

            executed_at = datetime.now(timezone.utc)
            transfer.status = status
            transfer.executed_at = executed_at
            if setup.status == "active":
                next_scheduled_for = _next_occurrence(setup, transfer.scheduled_for)
                while next_scheduled_for <= executed_at:
                    next_scheduled_for = _next_occurrence(setup, next_scheduled_for)
                next_transfer = ScheduledTransfer(
                    setup_id=setup.setup_id,
                    scheduled_for=next_scheduled_for,
                    status="pending",
                )
                session.add(next_transfer)
                session.flush()
                next_job = _add_transfer_job(
                    scheduler,
                    next_transfer,
                    session_factory,
                    settings,
                    replace_existing=False,
                )
            session.commit()
    except Exception:
        if next_job is not None:
            _remove_job_if_present(scheduler, next_job.id)
        raise


def _next_occurrence(setup: ScheduledTransferSetup, previous: datetime) -> datetime:
    return next_occurrence(
        Recurrence(
            setup.scheduled_date,
            setup.hour,
            setup.minute,
            TransferInterval(setup.interval),
        ),
        previous,
    )


def _add_transfer_job(
    scheduler: BackgroundScheduler,
    transfer: ScheduledTransfer,
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    replace_existing: bool,
    run_at: datetime | None = None,
) -> Job:
    return scheduler.add_job(
        execute_scheduled_transfer,
        "date",
        run_date=run_at or _as_utc(transfer.scheduled_for),
        args=[transfer.transfer_id, scheduler, session_factory, settings],
        id=transfer.transfer_id,
        replace_existing=replace_existing,
        misfire_grace_time=None,
    )


def _remove_job_if_present(scheduler: BackgroundScheduler, transfer_id: str) -> None:
    try:
        scheduler.remove_job(transfer_id)
    except JobLookupError:
        pass


def resume_user_scheduling(user_id, session_token, session_factory, settings):
    with user_execution_lock(user_id):
        if decode_user_id(session_token, settings, session_factory) != user_id:
            raise SessionAuthenticationError
        with session_factory() as session:
            credential = session.get(MonzoCredential, user_id)
            if credential is None:
                raise SessionAuthenticationError
            credential.scheduling_paused = False
            session.commit()
