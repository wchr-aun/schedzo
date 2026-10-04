"""Pure calendar rules for recurring UK-local schedules."""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.domain.time import UK_TIMEZONE, as_utc
from app.domain.transfers import TransferInterval


@dataclass(frozen=True)
class Recurrence:
    scheduled_date: date
    hour: int
    minute: int
    interval: TransferInterval


def next_occurrence(setup: Recurrence, previous_scheduled_for: datetime) -> datetime:
    previous = as_utc(previous_scheduled_for).astimezone(UK_TIMEZONE)
    if setup.interval == TransferInterval.DAILY.value:
        next_date = previous.date() + timedelta(days=1)
    elif setup.interval == TransferInterval.WEEKLY.value:
        next_date = previous.date() + timedelta(weeks=1)
    else:
        next_date = _next_month(previous.date(), setup.scheduled_date.day)

    local = datetime.combine(
        next_date,
        time(setup.hour, setup.minute),
        tzinfo=UK_TIMEZONE,
    )
    return local.astimezone(timezone.utc)


def _next_month(previous: date, requested_day: int) -> date:
    month_index = previous.year * 12 + previous.month
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(requested_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
