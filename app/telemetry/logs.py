"""A fail-closed bridge for the application's existing event logs."""

import logging
import re
from contextvars import ContextVar

from opentelemetry.instrumentation.logging.handler import LoggingHandler

request_context: ContextVar[dict | None] = ContextVar("telemetry_request", default=None)


def bind_authenticated_user(user_id: str) -> None:
    """Enrich the current request only after its application session is verified."""
    context = request_context.get()
    if context is not None:
        # Sync dependencies run in a copied thread context. Mutate the request's
        # shared dictionary so async handlers and outer middleware see the result.
        context["user_id"] = user_id


EVENTS = frozenset(
    {
        "oauth_redirect_failed",
        "oauth_callback_failed",
        "oauth_token_exchange_failed",
        "app_token_refresh_failed",
        "authentication_failed",
        "credential_resolution_failed",
        "credential_refresh_failed",
        "monzo_disconnection_pending",
        "scheduled_transfers_restored",
        "scheduled_transfer_feed_rejected",
        "scheduled_transfer_feed_failed",
        "monzo_token_refresh_exception",
        "monzo_request_failed",
        "scheduled_transfer_failed",
        "scheduled_transfer_rejected",
        "scheduled_transfer_completed",
        "scheduled_transfer_storage_failed",
        "request_failed",
        "request_completed_with_error",
        "request_completed",
        "request_rejected",
        "monzo_request_started",
        "monzo_request_completed",
        "monzo_request_transport_failed",
        "scheduled_transfer_created",
        "scheduled_transfer_cancelled",
        "app_session_logged_out",
        "endpoint_entered",
    }
)

REASONS = frozenset(
    {
        "bearer_token_missing",
        "invalid_or_expired_jwt",
        "access_token_expired",
        "storage_or_configuration",
        "monzo_connection_unavailable",
        "invalid_response",
        "monzo_unreachable",
        "oauth_not_configured",
        "invalid_state",
        "session_signing_not_configured",
        "token_storage_unavailable",
        "session_storage_unavailable",
        "invalid_or_expired_refresh_token",
        "invalid_json",
    }
)

LOGGER_NAMES = frozenset(
    {
        "schedzo",
        "schedzo.oauth",
        "schedzo.app.http",
        "schedzo.app.dependencies",
        "schedzo.app.services.disconnection",
        "schedzo.app.services.scheduler",
        "schedzo.app.services.notifications",
        "schedzo.app.services.monzo_credentials",
        "schedzo.app.services.monzo",
        "schedzo.app.services.transfer_execution",
        "schedzo.app.services.schedules",
        "schedzo.app.services.sessions",
        "schedzo.app.routers.resources",
    }
)


class EventLogHandler(LoggingHandler):
    """Export event names and approved fields, never arbitrary message/extra content."""

    def __init__(self, provider, owner):
        super().__init__(level=logging.INFO, logger_provider=provider)
        self.owner = owner

    def emit(self, record: logging.LogRecord) -> None:
        context = request_context.get()
        if not self.owner.active or (context and context["owner"] is not self.owner):
            return
        try:
            template = record.msg if isinstance(record.msg, str) else ""
            # Entry logs happen before authentication, so they cannot carry the
            # authenticated user attribute required for exported request logs.
            # Keep them in application logs while avoiding incomplete telemetry.
            if template.startswith("endpoint_entered "):
                return
            event = template.split(" ", 1)[0]
            if event not in EVENTS:
                event = "application_log_redacted"
            logger_name = (
                record.name if record.name in LOGGER_NAMES else "schedzo.application"
            )
            attributes = {"event.name": event, "logger.name": logger_name}
            if event in EVENTS:
                # Only fixed values from the source template, not interpolated input.
                for key in ("reason",):
                    match = re.search(rf"(?:^| ){key}=([a-z_]+)(?: |$)", template)
                    if match and match[1] in REASONS:
                        attributes[key] = match[1]
                # Inspect positional fields from the source template rather than
                # parsing rendered text: an untrusted path can contain fake fields.
                fields = re.findall(r"([a-z_]+)=%[sdr]", template)
                if isinstance(record.args, tuple):
                    for key, value in zip(fields, record.args, strict=False):
                        if (
                            key
                            in {
                                "status_code",
                                "upstream_status",
                                "duration_ms",
                                "count",
                                "cancelled_count",
                            }
                            and type(value) is int
                            and 0 <= value < 10**10
                        ):
                            attributes[key] = value
            if context:
                attributes.update(
                    {
                        "request_id": context["request_id"],
                        "http.route": context["route"],
                    }
                )
                if "user_id" in context:
                    attributes["user_id"] = context["user_id"]
            safe = logging.LogRecord(
                logger_name, record.levelno, "", 0, event, (), None
            )
            safe.created = record.created
            safe.__dict__.update(attributes)
            super().emit(safe)
        except Exception as exc:  # noqa: BLE001 -- isolate telemetry from business work
            logging.getLogger("telemetry.logs").warning(
                "telemetry_log_dropped exception_type=%s", type(exc).__name__
            )
            # Never make a business operation fail because telemetry formatting failed.
            return
