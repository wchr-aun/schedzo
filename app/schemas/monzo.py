from pydantic import BaseModel, ConfigDict, Field

from app.domain.monzo import (
    Account,
    AccountWithBalance,
    AccountsWithBalancesResponse,
    BalanceResponse,
    MonzoAccountsResponse,
    MonzoTokenResponse,
    Pot,
    PotsResponse,
)


class AppRefreshRequest(BaseModel):
    """Refresh credential submitted by an application client."""

    model_config = ConfigDict(
        populate_by_name=True, extra="forbid", hide_input_in_errors=True
    )

    refresh_token: str = Field(
        min_length=32, max_length=256, alias="refreshToken", repr=False
    )
