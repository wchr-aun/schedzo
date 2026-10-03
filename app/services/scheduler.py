"""APScheduler job registration and startup reconciliation."""

from apscheduler.jobstores.base import JobLookupError

from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import Settings
from app.db.models import ScheduledTransfer, ScheduledTransferSetup
from app.db.session import SessionFactory
from app.domain.time import as_utc as _as_utc
from app.observability import get_logger

logger = get_logger(__name__)


def restore_scheduled_transfers(
    scheduler: BackgroundScheduler,
    session_factory: SessionFactory,
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
        add_transfer_job(
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


def add_transfer_job(
    scheduler: BackgroundScheduler,
    transfer: ScheduledTransfer,
    session_factory: SessionFactory,
    settings: Settings,
    *,
    replace_existing: bool,
    run_at: datetime | None = None,
) -> None:
    # Resolve the callback after modules are loaded; execution uses this adapter.
    from app.services.transfer_execution import execute_scheduled_transfer

    scheduler.add_job(
        execute_scheduled_transfer,
        "date",
        run_date=run_at or _as_utc(transfer.scheduled_for),
        args=[transfer.transfer_id, scheduler, session_factory, settings],
        id=transfer.transfer_id,
        replace_existing=replace_existing,
        misfire_grace_time=None,
    )


def remove_job_if_present(scheduler: BackgroundScheduler, transfer_id: str) -> None:
    try:
        scheduler.remove_job(transfer_id)
    except JobLookupError:
        pass
