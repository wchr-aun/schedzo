"""Logging helpers for request and application diagnostics."""

import logging
from typing import Any

LOGGER_NAME = "schedzo"


class QueryStringRedactionFilter(logging.Filter):
    """Remove query parameters from Uvicorn access-log request targets."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            arguments = list(record.args)
            if isinstance(arguments[2], str):
                arguments[2] = arguments[2].split("?", maxsplit=1)[0]
                record.args = tuple(arguments)
        return True


def configure_logging() -> None:
    """Configure a useful fallback when the process runner has no logging setup."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger(LOGGER_NAME).setLevel(logging.INFO)
    # Migration logging configuration can disable existing application loggers.
    for name, candidate in logging.Logger.manager.loggerDict.copy().items():
        if isinstance(candidate, logging.Logger) and (
            name == LOGGER_NAME or name.startswith(f"{LOGGER_NAME}.")
        ):
            candidate.disabled = False
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(log_filter, QueryStringRedactionFilter)
        for log_filter in access_logger.filters
    ):
        access_logger.addFilter(QueryStringRedactionFilter())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def monzo_error_details(response: Any) -> tuple[str, str]:
    """Return a fixed diagnostic without trusting fields from upstream errors."""
    try:
        payload = response.json()
    except ValueError, TypeError:
        return "unknown", "unknown"

    if not isinstance(payload, dict):
        return "unknown", "unknown"

    return "upstream_error", "upstream_error"
