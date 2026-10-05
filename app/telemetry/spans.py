"""Sanitize finished library spans before they enter export queues."""

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import SpanContext, Status

METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
    "TRACE",
    "CONNECT",
}


def clean_context(context):
    if context is None:
        return None
    return SpanContext(
        context.trace_id, context.span_id, context.is_remote, context.trace_flags
    )


class SanitizingSpanProcessor(SpanProcessor):
    """Discard automatic URLs, exceptions, headers, links, and status descriptions."""

    def __init__(self, delegate):
        self.delegate = delegate

    def on_start(self, span, parent_context=None):
        self.delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span):
        original = span.attributes or {}
        method = original.get("http.request.method", "OTHER")
        method = method if method in METHODS else "OTHER"
        route = original.get("http.route") or "unmatched"
        attributes = {"http.request.method": method, "http.route": route}
        for key in ("http.response.status_code", "request_id"):
            if key in original:
                attributes[key] = original[key]
        safe = ReadableSpan(
            name=f"{method} {route}",
            context=clean_context(span.context),
            parent=clean_context(span.parent),
            resource=span.resource,
            attributes=attributes,
            kind=span.kind,
            status=Status(span.status.status_code),
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )
        self.delegate.on_end(safe)

    def shutdown(self):
        self.delegate.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.delegate.force_flush(timeout_millis)
