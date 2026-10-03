"""APScheduler job registration and startup reconciliation."""

from collections.abc import Callable
from datetime import datetime, timezone

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import Settings
from app.db.models import ScheduledTransfer, ScheduledTransferSetup
from app.db.session import SessionFactory
from app.domain.scheduling import TransferJobs
from app.domain.time import as_utc as _as_utc
from app.observability import get_logger

logger = get_logger(__name__)


def restore_scheduled_transfers(
    scheduler: TransferJobs,
    session_factory: SessionFactory,
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
        active_transfers: list[tuple[str, datetime]] = []
        for transfer, setup in rows:
            if setup.status == "active":
                transfer.status = "pending"
                active_transfers.append(
                    (transfer.transfer_id, _as_utc(transfer.scheduled_for))
                )
            else:
                transfer.status = "cancelled"
        session.commit()

    restored = 0
    now = datetime.now(timezone.utc)
    for transfer_id, scheduled_for in active_transfers:
        run_at = max(_as_utc(scheduled_for), now)
        scheduler.schedule(
            transfer_id,
            scheduled_for,
            replace_existing=True,
            run_at=run_at,
        )
        restored += 1

    logger.info("scheduled_transfers_restored count=%d", restored)
    return restored


type TransferExecutor = Callable[[str, TransferJobs, SessionFactory, Settings], None]


class APSchedulerTransferJobs:
    """Adapt application occurrence operations to APScheduler date jobs."""

    def __init__(
        self,
        scheduler: BackgroundScheduler,
        executor: TransferExecutor,
        session_factory: SessionFactory,
        settings: Settings,
    ) -> None:
        self.scheduler = scheduler
        self.executor = executor
        self.session_factory = session_factory
        self.settings = settings

    def schedule(
        self,
        transfer_id: str,
        scheduled_for: datetime,
        *,
        replace_existing: bool = False,
        run_at: datetime | None = None,
    ) -> None:
        self.scheduler.add_job(
            self.executor,
            "date",
            run_date=run_at or _as_utc(scheduled_for),
            args=[transfer_id, self, self.session_factory, self.settings],
            id=transfer_id,
            replace_existing=replace_existing,
            misfire_grace_time=None,
        )

    def remove(self, transfer_id: str) -> None:
        try:
            self.scheduler.remove_job(transfer_id)
        except JobLookupError:
            pass
