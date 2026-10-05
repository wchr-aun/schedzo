"""Offline tests of the exported telemetry, including middleware rejection paths."""

import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import (
    DropAggregation,
    ExplicitBucketHistogramAggregation,
    View,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.main import create_app
from app.telemetry.runtime import BUCKETS, METRIC_ATTRIBUTES, Telemetry
from app.telemetry.spans import SanitizingSpanProcessor


@pytest.fixture
def telemetry_app(settings):
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    reader = InMemoryMetricReader()
    resource = Resource({"service.name": "schedzo"})
    traces = TracerProvider(resource=resource, shutdown_on_exit=False)
    traces.add_span_processor(SanitizingSpanProcessor(SimpleSpanProcessor(spans)))
    metrics = MeterProvider(
        resource=resource,
        metric_readers=[reader],
        shutdown_on_exit=False,
        views=[
            View(instrument_name="*", aggregation=DropAggregation()),
            View(
                instrument_name="http.server.request.duration",
                attribute_keys=METRIC_ATTRIBUTES,
                aggregation=ExplicitBucketHistogramAggregation(BUCKETS),
            ),
        ],
    )
    logger = LoggerProvider(resource=resource, shutdown_on_exit=False)
    logger.add_log_record_processor(SimpleLogRecordProcessor(logs))
    runtime = Telemetry(traces, metrics, logger)
    enabled = replace(
        settings,
        otel_enabled=True,
        otel_endpoint="https://otlp.example.test/otlp",
        otel_headers="Authorization=Basic%20private",
    )
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    application = create_app(
        enabled, engine=engine, telemetry_factory=lambda _: runtime
    )

    @application.get("/telemetry-test/{item_id}")
    def probe(item_id: str):
        logging.getLogger("schedzo.test").info(
            "scheduled_transfer_completed transfer_id=%s", item_id
        )
        return {"ok": True}

    @application.get("/telemetry-failure")
    def failure():
        raise RuntimeError("sensitive-exception-token")

    try:
        with TestClient(
            application,
            headers={"X-BFF-API-Key": settings.bff_api_key},
            raise_server_exceptions=False,
        ) as client:
            yield client, runtime, spans, logs, reader
    finally:
        runtime.shutdown()
        engine.dispose()


def metric_points(reader):
    data = reader.get_metrics_data()
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for point in metric.data.data_points
    ]


def test_requests_group_by_template_and_logs_correlate(telemetry_app):
    client, _runtime, spans, logs, reader = telemetry_app
    for item in ("private-first", "private-second"):
        response = client.get(f"/telemetry-test/{item}?token=query-secret")
        assert response.status_code == 200
        span = spans.get_finished_spans()[-1]
        record = logs.get_finished_logs()[-1].log_record
        assert record.trace_id == span.context.trace_id
        assert record.span_id == span.context.span_id
        assert record.attributes["request_id"] == response.headers["X-Request-ID"]
        assert span.attributes["request_id"] == response.headers["X-Request-ID"]
        assert span.name == "GET /telemetry-test/{item_id}"
    points = metric_points(reader)
    assert len(points) == 1 and points[0].count == 2
    assert set(points[0].attributes) == METRIC_ATTRIBUTES
    assert "private-" not in repr(spans.get_finished_spans())
    assert "private-" not in str([x.log_record.body for x in logs.get_finished_logs()])


@pytest.mark.parametrize(
    "kind,expected",
    [("bff", 401), ("body", 413), ("unknown", 404), ("exception", 500), ("rate", 429)],
)
def test_rejections_and_exceptions_are_observed_without_secrets(
    telemetry_app, kind, expected
):
    client, _runtime, spans, logs, reader = telemetry_app
    if kind == "bff":
        response = client.get("/telemetry-test/id", headers={"X-BFF-API-Key": "bad"})
    elif kind == "body":
        response = client.post("/telemetry-test/id", content="sensitive-body" * 2000)
    elif kind == "unknown":
        response = client.get("/unknown-secret-path")
    elif kind == "exception":
        response = client.get("/telemetry-failure")
    else:
        client.app.state.resources.request_rate_limiter.allow = lambda _: False
        response = client.get("/telemetry-test/id")
    assert response.status_code == expected
    span = spans.get_finished_spans()[-1]
    assert span.attributes["http.response.status_code"] == expected
    assert not span.events
    assert span.status.description is None
    assert metric_points(reader)[0].attributes["http.response.status_code"] == expected
    exported = (
        str(span.attributes)
        + span.name
        + str(
            [
                (r.log_record.body, r.log_record.attributes)
                for r in logs.get_finished_logs()
            ]
        )
    )
    assert "sensitive-" not in exported
    assert "unknown-secret" not in exported


def test_oauth_secrets_and_arbitrary_log_fields_are_not_exported(telemetry_app):
    client, _runtime, spans, logs, _reader = telemetry_app
    response = client.get(
        "/monzo-callback?code=oauth-secret&state=state-secret",
        headers={
            "Authorization": "Bearer jwt-secret",
            "Cookie": "private=cookie-secret",
        },
    )
    assert response.status_code == 400
    assert spans.get_finished_spans()[-1].attributes["http.route"] == "/monzo-callback"
    assert (
        logs.get_finished_logs()[-1].log_record.attributes["http.route"]
        == "/monzo-callback"
    )
    logging.getLogger("schedzo.test").error(
        "arbitrary secret: %s",
        "log-secret",
        extra={"access_token": "extra-secret"},
        exc_info=RuntimeError("exception-secret"),
    )
    serialized = str(
        [
            (s.name, dict(s.attributes), s.events, s.status.description)
            for s in spans.get_finished_spans()
        ]
    )
    serialized += str(
        [(r.log_record.body, r.log_record.attributes) for r in logs.get_finished_logs()]
    )
    for secret in (
        "oauth-secret",
        "state-secret",
        "jwt-secret",
        "cookie-secret",
        "log-secret",
        "extra-secret",
        "exception-secret",
    ):
        assert secret not in serialized
    assert logs.get_finished_logs()[-1].log_record.body == "application_log_redacted"


def test_health_docs_excluded_and_unknown_paths_bounded(telemetry_app):
    client, _runtime, spans, _logs, reader = telemetry_app
    for path in ("/health", "/docs", "/openapi.json"):
        client.get(path)
    assert not spans.get_finished_spans()
    for path in ("/unmatched-one", "/unmatched-two"):
        client.get(path)
    points = metric_points(reader)
    assert len(points) == 1 and points[0].count == 2
    assert points[0].attributes.get("http.route", "unmatched") == "unmatched"


def test_traceparent_propagates_without_baggage(telemetry_app):
    client, _runtime, spans, logs, reader = telemetry_app
    trace_id = "1234567890abcdef1234567890abcdef"
    client.get(
        "/telemetry-test/123",
        headers={
            "traceparent": f"00-{trace_id}-1234567890abcdef-01",
            "tracestate": "private=state-secret",
            "baggage": "access_token=baggage-secret",
        },
    )
    span = spans.get_finished_spans()[-1]
    assert f"{span.context.trace_id:032x}" == trace_id
    assert span.parent.span_id == int("1234567890abcdef", 16)
    assert not span.context.trace_state
    assert "secret" not in str(span.attributes)
    assert logs.get_finished_logs()[-1].log_record.trace_id == int(trace_id, 16)
    # Unsampled parent suppresses spans but leaves endpoint metrics and event logs.
    count = len(spans.get_finished_spans())
    client.get(
        "/telemetry-test/456",
        headers={
            "traceparent": f"00-{trace_id}-1234567890abcdef-00",
        },
    )
    assert len(spans.get_finished_spans()) == count
    assert metric_points(reader)[0].count == 2


def test_logger_handler_is_removed_between_app_lifespans(telemetry_app):
    client, runtime, _spans, _logs, _reader = telemetry_app
    logger = logging.getLogger("schedzo")
    assert logger.handlers.count(runtime.handler) == 1
    runtime.shutdown()
    assert runtime.handler not in logger.handlers
    # Requests continue to work with a stopped telemetry runtime.
    assert client.get("/telemetry-test/123").status_code == 200


def test_log_fields_cannot_be_injected_through_rendered_arguments(telemetry_app):
    _client, _runtime, _spans, logs, _reader = telemetry_app
    logging.getLogger("schedzo.logger-secret").error(
        "request_failed path=%s duration_ms=%d",
        "/private status_code=123456",
        25,
        extra={"exception.message": "extra-secret"},
    )
    record = logs.get_finished_logs()[-1]
    assert record.log_record.attributes["duration_ms"] == 25
    assert "status_code" not in record.log_record.attributes
    assert "logger-secret" not in record.instrumentation_scope.name
    assert "extra-secret" not in str(record.log_record.attributes)
