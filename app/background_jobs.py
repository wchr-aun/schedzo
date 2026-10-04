"""Register maintenance and transfer-recovery jobs before scheduler startup."""

from app.runtime import ApplicationResources
from app.services.disconnection import retry_pending_disconnections
from app.services.maintenance import prune_history
from app.services.scheduler import restore_scheduled_transfers


def register_background_jobs(resources: ApplicationResources) -> None:
    scheduler = resources.scheduler
    factory = resources.session_factory
    settings = resources.settings
    prune_history(factory)
    scheduler.add_job(
        prune_history,
        "interval",
        hours=1,
        args=[factory],
        id="prune-history",
        max_instances=1,
    )
    restore_scheduled_transfers(resources.transfer_jobs, factory)
    scheduler.add_job(
        retry_pending_disconnections,
        "interval",
        minutes=1,
        args=[factory, settings],
        id="retry-disconnections",
        max_instances=1,
    )
