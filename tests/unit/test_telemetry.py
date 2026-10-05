import logging
from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from urllib3.response import HTTPResponse

from app.config import validate_settings
from app.main import create_app
from app.observability import configure_logging
from app.telemetry.logs import EventLogHandler
from app.telemetry.runtime import create_telemetry


def enabled(settings, **kwargs):
    values = {
        "otel_enabled": True,
        "otel_endpoint": "https://otlp.example.test/otlp",
        "otel_headers": "Authorization=Basic%20private",
    }
    values.update(kwargs)
    return replace(settings, **values)


@pytest.mark.parametrize(
    "changes",
    [
        {"otel_endpoint": "http://example.test"},
        {"otel_endpoint": "https://user:secret@example.test"},
        {"otel_endpoint": "https://example.test?token=secret"},
        {"otel_headers": "secret"},
        {"otel_headers": "Authorization="},
        {"otel_headers": "Authorization=secret\nInjected=x"},
        {"otel_trace_sample_ratio": -1},
        {"otel_trace_sample_ratio": float("nan")},
        {"otel_service_name": ""},
    ],
)
def test_invalid_configuration_is_sanitized(settings, changes):
    with pytest.raises(RuntimeError) as exc:
        validate_settings(enabled(settings, **changes))
    assert "secret" not in str(exc.value)


def test_credentials_hidden_from_repr(settings):
    assert "private" not in repr(enabled(settings))


def test_disabled_telemetry_never_constructs_exporters(settings, monkeypatch):
    factory = Mock(side_effect=AssertionError("must not be called"))
    engine = create_engine("sqlite://")
    application = create_app(settings, engine=engine, telemetry_factory=factory)
    factory.assert_not_called()
    monkeypatch.setattr("app.lifespan.register_background_jobs", lambda _: None)
    try:
        with TestClient(application) as client:
            assert client.get("/health").status_code == 200
    finally:
        engine.dispose()
    assert not any(
        isinstance(h, EventLogHandler) for h in logging.getLogger("schedzo").handlers
    )
    factory.assert_not_called()


def test_real_otlp_exporters_use_correct_urls_headers_and_survive_rejection(
    settings, monkeypatch, caplog
):
    configure_logging()
    caplog.set_level(logging.INFO, logger="schedzo")
    calls = []

    def request(pool, method, url, **kwargs):
        calls.append((url, kwargs))
        return HTTPResponse(status=401, body=b"unauthorized")

    monkeypatch.setattr("urllib3.PoolManager.request", request)
    runtime = create_telemetry(enabled(settings))
    try:
        with runtime.tracer.start_as_current_span("GET /test"):
            logging.getLogger("schedzo").warning(
                "oauth_callback_failed reason=invalid_state"
            )
        monkeypatch.setattr("app.lifespan.register_background_jobs", lambda _: None)
        engine = create_engine("sqlite://")
        application = create_app(
            enabled(settings), engine=engine, telemetry_factory=lambda _: runtime
        )

        @application.get("/telemetry-ping")
        def ping():
            return {"ok": True}

        try:
            with TestClient(
                application, headers={"X-BFF-API-Key": settings.bff_api_key}
            ) as client:
                assert client.get("/telemetry-ping").json() == {"ok": True}
                runtime.tracer_provider.force_flush(timeout_millis=1000)
                runtime.logger_provider.force_flush(timeout_millis=1000)
                runtime.meter_provider.force_flush(timeout_millis=1000)
                assert {url for url, _ in calls} == {
                    f"https://otlp.example.test/otlp/v1/{signal}"
                    for signal in ("traces", "metrics", "logs")
                }
                assert all(
                    {k.lower(): v for k, v in call["headers"].items()}["authorization"]
                    == "Basic private"
                    for _, call in calls
                )
                assert all(isinstance(call["body"], bytes) for _, call in calls)
                runtime.tracer_provider.force_flush(timeout_millis=1000)
                assert client.get("/telemetry-ping").status_code == 200
        finally:
            engine.dispose()
    finally:
        runtime.shutdown()
    assert not runtime.active
    assert runtime.handler not in logging.getLogger("schedzo").handlers
    runtime.shutdown()


def test_failed_startup_shuts_down_telemetry(settings, monkeypatch):
    runtime = Mock()
    monkeypatch.setattr(
        "app.lifespan.register_background_jobs",
        Mock(side_effect=RuntimeError("startup failed")),
    )
    with (
        pytest.raises(RuntimeError, match="startup failed"),
        TestClient(create_app(enabled(settings), telemetry_factory=lambda _: runtime)),
    ):
        pass
    runtime.shutdown.assert_called_once()


def test_span_processor_removes_automatic_sensitive_fields():
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace import SpanContext, Status, StatusCode, TraceState

    from app.telemetry.spans import SanitizingSpanProcessor

    context = SpanContext(
        123, 456, False, trace_state=TraceState([("private", "state-secret")])
    )
    delegate = Mock()
    span = ReadableSpan(
        name="GET /monzo-callback?code=oauth-secret",
        context=context,
        parent=context,
        resource=Resource({"service.name": "schedzo"}),
        attributes={
            "http.request.method": "GET",
            "http.route": "/monzo-callback",
            "http.response.status_code": 500,
            "url.full": "https://example.test?code=oauth-secret",
            "http.request.header.authorization": "Bearer token-secret",
        },
        status=Status(StatusCode.ERROR, "exception-secret"),
        events=[Event("exception", attributes={"exception.message": "event-secret"})],
    )
    processor = SanitizingSpanProcessor(delegate)
    processor.on_end(span)
    exported = delegate.on_end.call_args.args[0]
    assert exported.name == "GET /monzo-callback"
    assert set(exported.attributes) == {
        "http.request.method",
        "http.route",
        "http.response.status_code",
    }
    assert exported.status.status_code == StatusCode.ERROR
    assert exported.status.description is None
    assert not exported.events and not exported.links
    assert not exported.context.trace_state and not exported.parent.trace_state
    assert "secret" not in str(exported.attributes)
