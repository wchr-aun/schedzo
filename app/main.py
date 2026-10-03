"""Application composition: settings, lifespan, HTTP policy, and routers."""

from fastapi import FastAPI
from sqlalchemy.engine import Engine

from app.config import Settings
from app.http import register_exception_handlers, register_http_middleware
from app.lifespan import build_lifespan
from app.observability import configure_logging
from app.routers import health, monzo, resources, tasks


def create_app(
    settings: Settings | None = None, *, engine: Engine | None = None
) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_environment()
    application = FastAPI(
        title="Schedzo", lifespan=build_lifespan(settings, engine=engine)
    )
    register_exception_handlers(application)
    register_http_middleware(
        application, production=settings.environment == "production"
    )
    application.include_router(health.router)
    application.include_router(tasks.router)
    application.include_router(monzo.router)
    application.include_router(resources.router)
    return application


app = create_app()
