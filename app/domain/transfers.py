"""Transfer commands and results independent of API schemas and ORM models."""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.domain.time import UK_TIMEZONE

RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


class TransferInterval(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class TransferType(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"


class TransferStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvalidScheduleError(ValueError):
    """The requested schedule cannot be registered."""


class ScheduleNotFoundError(LookupError):
    """The requested setup does not exist for the authenticated user."""


class ScheduleQuotaExceededError(ValueError):
    """The user has reached the active schedule limit."""


@dataclass(frozen=True)
class ScheduleTransferCommand:
    scheduled_for: datetime
    interval: TransferInterval
    transfer_type: TransferType
    amount: int
    pot_id: str
    account_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scheduled_for", validate_uk_datetime(self.scheduled_for)
        )
        object.__setattr__(self, "interval", TransferInterval(self.interval))
        object.__setattr__(self, "transfer_type", TransferType(self.transfer_type))
        if type(self.amount) is not int or not 0 < self.amount <= 2**63 - 1:
            raise InvalidScheduleError(
                "amount must be a positive signed 64-bit integer"
            )
        for resource_id in (self.pot_id, self.account_id):
            if (
                not isinstance(resource_id, str)
                or RESOURCE_ID_PATTERN.fullmatch(resource_id) is None
            ):
                raise InvalidScheduleError("Invalid Monzo resource identifier")


def validate_uk_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidScheduleError("datetime must include the UK UTC offset")
    uk_value = value.astimezone(UK_TIMEZONE)
    if value.replace(tzinfo=None) != uk_value.replace(tzinfo=None):
        raise InvalidScheduleError("datetime must represent local UK time")
    if value.second != 0 or value.microsecond != 0:
        raise InvalidScheduleError("datetime must be aligned to a whole minute")
    return uk_value


@dataclass(frozen=True)
class TransferExecution:
    setup_id: str
    user_id: str
    transfer_type: str
    amount: int
    pot_id: str
    account_id: str


@dataclass(frozen=True)
class ScheduledTransferDetails:
    setup_id: str
    transfer_id: str
    scheduled_for: datetime
    created_at: datetime
    interval: TransferInterval
    transfer_type: TransferType
    amount: int
    setup_status: str
    status: str
    executed_at: datetime | None


@dataclass(frozen=True)
class ScheduledTransfersPage:
    items: list[ScheduledTransferDetails]
    total: int
    limit: int
    offset: int
