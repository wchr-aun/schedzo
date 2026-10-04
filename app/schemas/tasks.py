from datetime import datetime as DateTime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.transfers import (
    ScheduleTransferCommand,
    TransferInterval,
    TransferType,
    validate_uk_datetime,
)


class ScheduleTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    datetime: DateTime
    interval: TransferInterval
    type: TransferType
    amount: int = Field(strict=True, gt=0, le=2**63 - 1)
    pot_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    account_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("datetime")
    @classmethod
    def validate_uk_datetime(cls, value: DateTime) -> DateTime:
        return validate_uk_datetime(value)

    def to_command(self) -> ScheduleTransferCommand:
        return ScheduleTransferCommand(
            scheduled_for=self.datetime,
            interval=self.interval,
            transfer_type=self.type,
            amount=self.amount,
            pot_id=self.pot_id,
            account_id=self.account_id,
        )


class ScheduledTransferResponse(BaseModel):
    setup_id: str
    transfer_id: str
    scheduled_for: DateTime
    created_at: DateTime
    interval: TransferInterval
    type: TransferType
    amount: int
    setup_status: str
    status: str
    executed_at: DateTime | None


class ScheduledTransfersPageResponse(BaseModel):
    items: list[ScheduledTransferResponse]
    total: int
    limit: int
    offset: int
