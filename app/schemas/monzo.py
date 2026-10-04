from pydantic import BaseModel, ConfigDict, Field

from app.domain.monzo import (
    Account,
    AccountsWithBalancesResponse,
    AccountWithBalance,
    BalanceResponse,
    MonzoAccountsResponse,
    MonzoTokenResponse,
    Pot,
    PotsResponse,
)

__all__ = [
    "Account",
    "AccountWithBalance",
    "AccountsWithBalancesResponse",
    "BalanceResponse",
    "MonzoAccountsResponse",
    "MonzoTokenResponse",
    "Pot",
    "PotsResponse",
    "AppRefreshRequest",
]


class AppRefreshRequest(BaseModel):
    """Refresh credential submitted by an application client."""

    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", hide_input_in_errors=True
    )

    refresh_token: str = Field(
        min_length=32, max_length=256, alias="refreshToken", repr=False
    )
