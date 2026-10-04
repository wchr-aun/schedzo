"""Reusable account-resource workflows without HTTP response concerns."""

from app.domain.monzo import (
    AccountsWithBalancesResponse,
    AccountWithBalance,
    BalanceResponse,
    PotsResponse,
)
from app.services.monzo import MonzoClient


class ResourceService:
    def __init__(self, client: MonzoClient):
        self._client = client

    async def accounts_with_balances(
        self,
        access_token: str,
        account_type: str | None = None,
    ) -> AccountsWithBalancesResponse:
        accounts = await self._client.get_accounts(access_token, account_type)
        balances = await self._client.get_balances(
            access_token, [account.id for account in accounts.accounts]
        )
        return AccountsWithBalancesResponse(
            accounts=[
                AccountWithBalance(**account.model_dump(), balance_details=balance)
                for account, balance in zip(accounts.accounts, balances, strict=True)
            ]
        )

    async def balance(
        self,
        access_token: str,
        account_id: str,
    ) -> BalanceResponse:
        return await self._client.get_balance(access_token, account_id)

    async def pots(
        self,
        access_token: str,
        current_account_id: str,
    ) -> PotsResponse:
        return await self._client.get_pots(access_token, current_account_id)
