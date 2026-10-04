"""Timezone normalization for persisted UTC timestamps and UK schedules."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UK_TIMEZONE = ZoneInfo("Europe/London")


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
