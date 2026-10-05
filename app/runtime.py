"""Typed resources owned by one application lifespan."""

from dataclasses import dataclass

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.session import SessionFactory
from app.domain.scheduling import TransferJobs
from app.rate_limit import RequestRateLimiter
from app.services.monzo import MonzoClient
from app.telemetry import Telemetry


@dataclass(frozen=True)
class ApplicationResources:
    settings: Settings
    database_engine: Engine
    session_factory: SessionFactory
    transfer_jobs: TransferJobs
    scheduler: BackgroundScheduler
    monzo_client: MonzoClient
    oauth_start_rate_limiter: RequestRateLimiter
    request_rate_limiter: RequestRateLimiter
    telemetry: Telemetry | None = None
