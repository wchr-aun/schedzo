"""Construct and release application resources, including failed startup."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from sqlalchemy.engine import Engine

from app.background_jobs import register_background_jobs
from app.config import Settings, validate_settings
from app.db.session import create_database_engine, create_session_factory
from app.rate_limit import RequestRateLimiter
from app.runtime import ApplicationResources
from app.services.monzo import monzo_client_scope
from app.services.scheduler import APSchedulerTransferJobs
from app.services.transfer_execution import execute_scheduled_transfer

type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def build_lifespan(settings: Settings, *, engine: Engine | None = None) -> Lifespan:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        validate_settings(settings)
        database_engine = (
            engine
            if engine is not None
            else create_database_engine(settings.database_url)
        )
        scheduler: BackgroundScheduler | None = None
        try:
            scheduler = BackgroundScheduler(timezone="UTC")
            session_factory = create_session_factory(database_engine)
            async with monzo_client_scope() as monzo_client:
                resources = ApplicationResources(
                    settings=settings,
                    database_engine=database_engine,
                    session_factory=session_factory,
                    scheduler=scheduler,
                    transfer_jobs=APSchedulerTransferJobs(
                        scheduler, execute_scheduled_transfer, session_factory, settings
                    ),
                    monzo_client=monzo_client,
                    oauth_start_rate_limiter=RequestRateLimiter(
                        max_requests=5, window_seconds=600
                    ),
                    request_rate_limiter=RequestRateLimiter(),
                )
                application.state.resources = resources
                register_background_jobs(resources)
                scheduler.start()
                yield
        finally:
            if scheduler is not None and scheduler.running:
                scheduler.shutdown(wait=False)
            if engine is None:
                database_engine.dispose()

    return lifespan
