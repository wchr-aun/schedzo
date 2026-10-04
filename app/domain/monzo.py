"""Validated Monzo payloads and resource results shared by application callers."""

from pydantic import BaseModel, ConfigDict, Field


class MonzoTokenResponse(BaseModel):
    """Token payload returned by Monzo's OAuth authorization-code exchange."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    access_token: str = Field(min_length=1, repr=False)
    client_id: str | None = None
    expires_in: int = Field(strict=True, gt=0)
    refresh_token: str | None = Field(default=None, repr=False)
    token_type: str = Field(default="Bearer", min_length=1)
    user_id: str = Field(min_length=1)


class Account(BaseModel):
    """Account details exposed by this service."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    description: str


class BalanceResponse(BaseModel):
    """Balance details exposed by this service."""

    model_config = ConfigDict(extra="ignore")

    balance: int = Field(strict=True)
    total_balance: int = Field(strict=True)
    currency: str = Field(min_length=3, max_length=3)


class AccountWithBalance(Account):
    """Account details with an explicitly optional balance payload."""

    balance_details: BalanceResponse | None = None


class MonzoAccountsResponse(BaseModel):
    """Account list returned by Monzo before balance enrichment."""

    model_config = ConfigDict(extra="ignore")

    accounts: list[Account]


class AccountsWithBalancesResponse(BaseModel):
    """Response returned by the enriched accounts endpoint."""

    model_config = ConfigDict(extra="ignore")

    accounts: list[AccountWithBalance]


class Pot(BaseModel):
    """Savings pot details exposed by this service."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    name: str
    balance: int = Field(strict=True)
    currency: str = Field(min_length=3, max_length=3)
    deleted: bool = Field(strict=True)
    cover_image_url: str | None = None
    type: str


class PotsResponse(BaseModel):
    """Response returned by the pots endpoint."""

    model_config = ConfigDict(extra="ignore")

    pots: list[Pot]
