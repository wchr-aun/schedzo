from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid6
from urllib.parse import urlparse
import base64
from app.transport_security import TransportSecurityMiddleware

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from cryptography.fernet import Fernet
from fastapi.responses import JSONResponse

from app.config import Settings
from app.db.session import create_database_engine, create_session_factory
from app.observability import configure_logging, get_logger
from app.rate_limit import RequestRateLimiter
from app.request_bounds import RequestBoundsMiddleware
from app.routers import health, monzo, resources, tasks
from app.services.monzo import monzo_client_scope
from app.services.disconnection import retry_pending_disconnections
from app.services.maintenance import prune_history
from app.services.scheduler import restore_scheduled_transfers

logger = get_logger(__name__)


def create_app(settings: Settings | None = None, *, engine=None) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if not settings.token_encryption_key:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
        try:
            Fernet(settings.token_encryption_key.encode())
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY must be a valid Fernet key"
            ) from exc
        if len(settings.jwt_secret_key.encode()) < 32:
            raise RuntimeError("JWT_SECRET_KEY must contain at least 32 bytes")
        if not 1 <= settings.jwt_expiration_seconds <= 3600:
            raise RuntimeError("JWT_EXPIRATION_SECONDS must be between 1 and 3600")
        if settings.environment not in {"development", "production"}:
            raise RuntimeError("APP_ENV must be development or production")
        if settings.environment == "production":
            if urlparse(settings.monzo_redirect_uri).scheme != "https":
                raise RuntimeError("MONZO_REDIRECT_URI must use HTTPS in production")
            if settings.jwt_secret_key.startswith(("replace-", "your-", "test-")):
                raise RuntimeError(
                    "Production secrets must not be placeholders or test credentials"
                )
            if (
                len(
                    {
                        settings.jwt_secret_key,
                        settings.token_encryption_key,
                    }
                )
                != 2
            ):
                raise RuntimeError("Signing and encryption keys must be distinct")
            if base64.urlsafe_b64decode(settings.token_encryption_key) in {
                b"0" * 32,
                b"\0" * 32,
            }:
                raise RuntimeError("Production encryption key must not be a test key")
        async with monzo_client_scope() as monzo_client:
            database_engine = engine or create_database_engine(settings.database_url)
            session_factory = create_session_factory(database_engine)
            scheduler = BackgroundScheduler(timezone="UTC")
            prune_history(session_factory)
            scheduler.add_job(
                prune_history,
                "interval",
                hours=1,
                args=[session_factory],
                id="prune-history",
                max_instances=1,
            )
            restore_scheduled_transfers(scheduler, session_factory, settings)
            scheduler.add_job(
                retry_pending_disconnections,
                "interval",
                minutes=1,
                args=[session_factory, settings],
                id="retry-disconnections",
                max_instances=1,
            )
            scheduler.start()
            application.state.monzo_client = monzo_client
            application.state.scheduler = scheduler
            application.state.settings = settings
            application.state.database_engine = database_engine
            application.state.session_factory = session_factory
            application.state.oauth_start_rate_limiter = RequestRateLimiter(
                max_requests=5, window_seconds=600
            )
            application.state.request_rate_limiter = RequestRateLimiter()
            try:
                yield
            finally:
                scheduler.shutdown(wait=False)
                if engine is None:
                    database_engine.dispose()

    application = FastAPI(
        title="Schedzo",
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def sanitized_validation_error(request, exc):
        # Never return submitted input, exception context, or attacker-controlled
        # field names. Preserve only the validation category and input source.
        detail = [
            {
                "loc": [error["loc"][0]] if error.get("loc") else [],
                "type": error["type"],
                "msg": "Invalid request data",
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            {"detail": detail},
            status_code=422,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @application.exception_handler(Exception)
    async def sanitized_server_error(request, exc):
        response = JSONResponse({"detail": "Internal server error"}, status_code=500)
        _add_security_headers(response, request)
        return response

    @application.middleware("http")
    async def log_request_failures(request: Request, call_next):
        request_id = uuid6().hex
        request.state.request_id = request_id
        started_at = monotonic()
        client_host = request.client.host if request.client is not None else "unknown"
        if not request.app.state.request_rate_limiter.allow(client_host):
            response = JSONResponse(
                status_code=429, content={"detail": "Too many requests"}
            )
            response.headers["X-Request-ID"] = request_id
            response.headers["Retry-After"] = "60"
            _add_security_headers(response, request)
            return response
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "request_failed request_id=%s method=%s path=%s "
                "exception_type=%s duration_ms=%d",
                request_id,
                request.method,
                request.url.path,
                type(exc).__name__,
                round((monotonic() - started_at) * 1000),
            )
            raise

        response.headers["X-Request-ID"] = request_id
        _add_security_headers(response, request)
        if response.status_code >= 400:
            log = logger.error if response.status_code >= 500 else logger.warning
            log(
                "request_completed_with_error request_id=%s method=%s path=%s "
                "status_code=%d duration_ms=%d",
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                round((monotonic() - started_at) * 1000),
            )
        return response

    application.add_middleware(RequestBoundsMiddleware)
    application.add_middleware(
        TransportSecurityMiddleware, production=settings.environment == "production"
    )

    application.include_router(health.router)
    application.include_router(tasks.router)
    application.include_router(monzo.router)
    application.include_router(resources.router)
    return application


def _add_security_headers(response, request: Request) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )


app = create_app()
