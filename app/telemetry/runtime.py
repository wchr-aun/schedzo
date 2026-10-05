"""Explicit provider ownership; no global provider registration or import-time workers."""

import logging
from contextlib import ExitStack
from threading import Thread
from urllib.parse import unquote

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LogRecordLimits
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import (
    DropAggregation,
    ExplicitBucketHistogramAggregation,
    View,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.config import Settings
from app.telemetry.logs import EventLogHandler
from app.telemetry.spans import SanitizingSpanProcessor

METRIC_ATTRIBUTES = {"http.request.method", "http.route", "http.response.status_code"}
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)


class Telemetry:
    def __init__(self, tracer_provider, meter_provider, logger_provider):
        self.tracer_provider = tracer_provider
        self.meter_provider = meter_provider
        self.logger_provider = logger_provider
        self.tracer = tracer_provider.get_tracer("schedzo.http")
        self.active = True
        self.handler = EventLogHandler(logger_provider, self)
        logging.getLogger("schedzo").addHandler(self.handler)

    def shutdown(self):
        """Detach immediately, then give background exports eight seconds to finish."""
        if not self.active:
            return
        self.active = False
        logging.getLogger("schedzo").removeHandler(self.handler)
        self.handler.close()

        def close():
            for provider in (
                self.logger_provider,
                self.meter_provider,
                self.tracer_provider,
            ):
                try:
                    provider.shutdown()
                except Exception:  # noqa: BLE001 -- release the remaining providers
                    logging.getLogger("telemetry.lifecycle").warning(
                        "telemetry_shutdown_failed"
                    )

        worker = Thread(target=close, name="telemetry-shutdown", daemon=True)
        worker.start()
        worker.join(timeout=8)


def create_telemetry(settings: Settings) -> Telemetry:
    endpoint = settings.otel_endpoint.rstrip("/")
    headers = {
        key.strip(): unquote(value.strip())
        for item in settings.otel_headers.split(",")
        for key, value in [item.split("=", 1)]
    }
    # Resource() avoids automatic environment/process detectors and arbitrary attributes.
    resource = Resource(
        {
            "service.name": settings.otel_service_name,
            "deployment.environment.name": settings.environment,
        }
    )
    batch = {
        "max_queue_size": 2048,
        "max_export_batch_size": 128,
        "schedule_delay_millis": 5000,
        "export_timeout_millis": 5000,
    }
    with ExitStack() as cleanup:
        traces = TracerProvider(
            resource=resource,
            shutdown_on_exit=False,
            sampler=ParentBased(TraceIdRatioBased(settings.otel_trace_sample_ratio)),
            span_limits=SpanLimits(
                max_attributes=16, max_attribute_length=128, max_events=0, max_links=0
            ),
        )
        cleanup.callback(traces.shutdown)
        traces.add_span_processor(
            SanitizingSpanProcessor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=f"{endpoint}/v1/traces",
                        headers=headers,
                        timeout=5,
                    ),
                    **batch,
                )
            )
        )
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=f"{endpoint}/v1/metrics",
                headers=headers,
                timeout=5,
            ),
            export_interval_millis=60000,
            export_timeout_millis=5000,
        )
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
        cleanup.callback(metrics.shutdown)
        logs = LoggerProvider(
            resource=resource,
            shutdown_on_exit=False,
            log_record_limits=LogRecordLimits(
                max_attributes=16, max_attribute_length=128
            ),
        )
        cleanup.callback(logs.shutdown)
        logs.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=f"{endpoint}/v1/logs",
                    headers=headers,
                    timeout=5,
                ),
                **batch,
            )
        )
        runtime = Telemetry(traces, metrics, logs)
        cleanup.pop_all()
        return runtime
