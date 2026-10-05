"""Construct and release application resources, including failed startup."""

from asyncio import to_thread
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
from app.telemetry import Telemetry, create_telemetry
from app.telemetry.http import instrument_http, uninstrument_http

type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def build_lifespan(
    settings: Settings,
    *,
    engine: Engine | None = None,
    telemetry_factory: Callable[[Settings], Telemetry] = create_telemetry,
) -> Lifespan:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        validate_settings(settings)
        database_engine = (
            engine
            if engine is not None
            else create_database_engine(settings.database_url)
        )
        telemetry: Telemetry | None = None
        scheduler: BackgroundScheduler | None = None
        try:
            if settings.otel_enabled:
                telemetry = telemetry_factory(settings)
            scheduler = BackgroundScheduler(timezone="UTC")
            session_factory = create_session_factory(database_engine)
            async with monzo_client_scope() as monzo_client:
                resources = ApplicationResources(
                    settings=settings,
                    telemetry=telemetry,
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
                if telemetry is not None:
                    instrument_http(application, telemetry)
                register_background_jobs(resources)
                scheduler.start()
                yield
        finally:
            try:
                if scheduler is not None and scheduler.running:
                    scheduler.shutdown(wait=False)
            finally:
                try:
                    if engine is None:
                        database_engine.dispose()
                finally:
                    if telemetry is not None:
                        try:
                            uninstrument_http(application)
                        finally:
                            await to_thread(telemetry.shutdown)

    return lifespan
