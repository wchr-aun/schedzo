"""HTTP error sanitization, request diagnostics, and security middleware."""

from collections.abc import Awaitable, Callable
from hmac import compare_digest
from time import monotonic
from uuid import uuid6

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.observability import get_logger
from app.request_bounds import RequestBoundsMiddleware
from app.transport_security import TransportSecurityMiddleware

logger = get_logger(__name__)


def register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
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
    async def sanitized_server_error(request: Request, exc: Exception) -> JSONResponse:
        response = JSONResponse({"detail": "Internal server error"}, status_code=500)
        add_security_headers(response, request)
        return response


def register_http_middleware(application: FastAPI, *, production: bool) -> None:
    @application.middleware("http")
    async def log_request_failures(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid6().hex
        request.state.request_id = request_id
        started_at = monotonic()
        if request.url.path not in {
            "/health",
            "/monzo-callback",
            "/docs",
            "/redoc",
            "/openapi.json",
        }:
            supplied_key = request.headers.get("X-BFF-API-Key", "")
            expected_key = request.app.state.resources.settings.bff_api_key
            if not supplied_key or not compare_digest(supplied_key, expected_key):
                response = JSONResponse(
                    status_code=401, content={"detail": "Invalid or missing BFF key"}
                )
                response.headers["X-Request-ID"] = request_id
                add_security_headers(response, request)
                return response
        client_host = request.client.host if request.client is not None else "unknown"
        if not request.app.state.resources.request_rate_limiter.allow(client_host):
            response = JSONResponse(
                status_code=429, content={"detail": "Too many requests"}
            )
            response.headers["X-Request-ID"] = request_id
            response.headers["Retry-After"] = "60"
            add_security_headers(response, request)
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
        add_security_headers(response, request)
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
    application.add_middleware(TransportSecurityMiddleware, production=production)


def add_security_headers(response: Response, request: Request) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
