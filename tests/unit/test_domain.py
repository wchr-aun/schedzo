from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.domain.authentication import AuthenticationContext, MonzoSession
from app.domain.transfers import (
    InvalidScheduleError,
    ScheduleTransferCommand,
    TransferInterval,
    TransferType,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": True},
        {"amount": 0},
        {"amount": 2**63},
        {"pot_id": "../accounts"},
        {"account_id": ""},
        {"scheduled_for": datetime(2030, 1, 1, 9)},
        {"scheduled_for": datetime(2030, 7, 1, 9, tzinfo=timezone.utc)},
    ],
)
def test_non_http_schedule_callers_cannot_bypass_validation(changes):
    command = ScheduleTransferCommand(
        datetime(2030, 1, 1, 9, tzinfo=timezone.utc),
        TransferInterval.DAILY,
        TransferType.DEPOSIT,
        100,
        "pot_a",
        "acc_a",
    )
    with pytest.raises(InvalidScheduleError):
        replace(command, **changes)


def test_authentication_contexts_hide_credentials():
    authentication = AuthenticationContext("user", "private-session-token", "session")
    connection = MonzoSession(
        "user", "private-provider-token", authentication.session_token
    )
    assert "private-session-token" not in repr(authentication)
    assert "private-session-token" not in repr(connection)
    assert "private-provider-token" not in repr(connection)
