"""Official FastAPI instrumentation with request correlation and safe metric views."""

import os
from uuid import uuid6

from fastapi.routing import iter_route_contexts
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import get_current_span
from starlette.routing import Match

from app.telemetry.logs import request_context

EXCLUDED_URLS = r"/(health|docs|redoc|openapi\.json)(?:\?.*)?$"


def instrument_http(application, telemetry):
    # Use the stable seconds-based HTTP metrics rather than legacy milliseconds.
    os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "http"
    FastAPIInstrumentor.instrument_app(
        application,
        tracer_provider=telemetry.tracer_provider,
        meter_provider=telemetry.meter_provider,
        excluded_urls=EXCLUDED_URLS,
        exclude_spans=["receive", "send"],
        http_capture_headers_server_request=[],
        http_capture_headers_server_response=[],
        http_capture_headers_sanitize_fields=[".*"],
    )
    # Lifespan dispatch already built the original stack. Rebuild for HTTP traffic
    # after the providers exist, without starting workers during app construction.
    application.middleware_stack = application.build_middleware_stack()


def uninstrument_http(application):
    if getattr(application, "_is_instrumented_by_opentelemetry", False):
        FastAPIInstrumentor.uninstrument_app(application)


class RequestCorrelationMiddleware:
    """Supply request/log correlation; the library owns spans, timing, and metrics."""

    def __init__(self, app, *, application):
        self.app = app
        self.application = application

    async def __call__(self, scope, receive, send):
        resources = getattr(self.application.state, "resources", None)
        telemetry = getattr(resources, "telemetry", None)
        if scope["type"] != "http" or not telemetry or not telemetry.active:
            return await self.app(scope, receive, send)
        route = "unmatched"
        for candidate in iter_route_contexts(self.application.routes):
            match, _ = candidate.matches(scope)
            if match != Match.NONE:
                route = getattr(candidate, "path", "unmatched")
                if match == Match.FULL:
                    break
        request_id = uuid6().hex
        scope.setdefault("state", {})["request_id"] = request_id
        span = get_current_span()
        span.set_attribute("request_id", request_id)
        token = request_context.set(
            {"owner": telemetry, "request_id": request_id, "route": route}
        )
        try:
            await self.app(scope, receive, send)
        finally:
            request_context.reset(token)
