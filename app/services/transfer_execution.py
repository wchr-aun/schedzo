"""Claim, execute, and finalize individual transfer occurrences."""

import asyncio
from typing import Literal

import httpx
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from app.domain.recurrence import Recurrence, next_occurrence
from app.domain.transfers import TransferExecution, TransferInterval, TransferType
from app.observability import monzo_error_details
from app.services.authorization import resolve_monzo_access_token
from app.services.monzo import deposit_into_pot, withdraw_from_pot
from app.services.notifications import notify_transfer_result, pot_name
from app.services.scheduler import add_transfer_job, remove_job_if_present
from app.services.user_locks import user_execution_lock

from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import Settings
from app.db.models import ScheduledTransfer, ScheduledTransferSetup
from app.db.session import SessionFactory
from app.domain.time import as_utc as _as_utc
from app.observability import get_logger

logger = get_logger(__name__)


def execute_scheduled_transfer(
    transfer_id: str,
    scheduler: BackgroundScheduler,
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    """Execute one occurrence and create its setup's next occurrence."""
    user_id = _transfer_user_id(transfer_id, session_factory)
    if user_id is None:
        return
    lock = user_execution_lock(user_id)
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


def _transfer_user_id(transfer_id: str, session_factory: SessionFactory) -> str | None:
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
    session_factory: SessionFactory,
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
            await notify_transfer_result(
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
    await notify_transfer_result(
        access_token,
        values,
        transfer_id,
        succeeded=True,
        pot_name=pot_name(response),
    )
    logger.info("scheduled_transfer_completed transfer_id=%s", transfer_id)


def _load_pending_execution(
    transfer_id: str,
    session_factory: SessionFactory,
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
    session_factory: SessionFactory,
    settings: Settings,
) -> None:
    next_transfer: ScheduledTransfer | None = None
    next_job_registered = False
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
                add_transfer_job(
                    scheduler,
                    next_transfer,
                    session_factory,
                    settings,
                    replace_existing=False,
                )
                next_job_registered = True
            session.commit()
    except Exception:
        if next_job_registered and next_transfer is not None:
            remove_job_if_present(scheduler, next_transfer.transfer_id)
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
