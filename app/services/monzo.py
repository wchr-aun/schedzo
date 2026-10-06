"""Injectable Monzo client; its HTTP transport belongs to one event loop."""

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from time import monotonic
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.domain.monzo import (
    BalanceResponse,
    MonzoAccountsResponse,
    MonzoTokenResponse,
    PotsResponse,
)
from app.domain.monzo_errors import (
    MonzoError,
    MonzoInvalidResponseError,
    MonzoRequestError,
    MonzoUnavailableError,
)
from app.domain.transfers import RESOURCE_ID_PATTERN
from app.observability import get_logger, monzo_error_details

MONZO_API_URL = "https://api.monzo.com"
logger = get_logger(__name__)


def _validate_resource_id(value: str) -> None:
    if RESOURCE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid Monzo resource identifier")


class MonzoClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def exchange_authorization_code(
        self, code: str, settings: Settings
    ) -> MonzoTokenResponse:
        response = await self._request(
            "POST",
            "/oauth2/token",
            f"{MONZO_API_URL}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.monzo_client_id,
                "client_secret": settings.monzo_client_secret,
                "redirect_uri": settings.monzo_redirect_uri,
                "code": code,
            },
        )
        response.raise_for_status()
        return MonzoTokenResponse.model_validate(response.json())

    async def refresh_access_token(
        self, refresh_token: str, settings: Settings
    ) -> MonzoTokenResponse:
        response = await self._request(
            "POST",
            "/oauth2/token",
            f"{MONZO_API_URL}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": settings.monzo_client_id,
                "client_secret": settings.monzo_client_secret,
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        return MonzoTokenResponse.model_validate(response.json())

    async def _accounts_response(
        self, access_token: str, account_type: str | None = None
    ) -> httpx.Response:
        params = {"account_type": account_type} if account_type is not None else None
        return await self._get("/accounts", access_token, params=params)

    async def _balance_response(
        self, access_token: str, account_id: str
    ) -> httpx.Response:
        return await self._get(
            "/balance", access_token, params={"account_id": account_id}
        )

    async def _pots_response(
        self, access_token: str, current_account_id: str
    ) -> httpx.Response:
        return await self._get(
            "/pots",
            access_token,
            params={"current_account_id": current_account_id},
        )

    async def deposit_into_pot(
        self,
        access_token: str,
        pot_id: str,
        account_id: str,
        amount: int,
        dedupe_id: str,
    ) -> httpx.Response:
        _validate_resource_id(pot_id)
        _validate_resource_id(account_id)
        return await self._put(
            f"/pots/{quote(pot_id, safe='')}/deposit",
            access_token,
            data={
                "source_account_id": account_id,
                "amount": str(amount),
                "dedupe_id": dedupe_id,
            },
        )

    async def withdraw_from_pot(
        self,
        access_token: str,
        pot_id: str,
        account_id: str,
        amount: int,
        dedupe_id: str,
    ) -> httpx.Response:
        _validate_resource_id(pot_id)
        _validate_resource_id(account_id)
        return await self._put(
            f"/pots/{quote(pot_id, safe='')}/withdraw",
            access_token,
            data={
                "destination_account_id": account_id,
                "amount": str(amount),
                "dedupe_id": dedupe_id,
            },
        )

    async def create_feed_item(
        self,
        access_token: str,
        account_id: str,
        *,
        title: str,
        image_url: str,
        body: str,
        url: str | None = None,
    ) -> httpx.Response:
        """Create a basic item in the account's Monzo feed."""
        data = {
            "account_id": account_id,
            "type": "basic",
            "params[title]": title,
            "params[image_url]": image_url,
            "params[body]": body,
        }
        if url is not None:
            data["url"] = url
        return await self._post(
            "/feed",
            access_token,
            data=data,
        )

    async def _get(
        self, path: str, access_token: str, *, params: dict[str, str] | None = None
    ) -> httpx.Response:
        return await self._request(
            "GET",
            path,
            f"{MONZO_API_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def _post(
        self, path: str, access_token: str, *, data: dict[str, str]
    ) -> httpx.Response:
        return await self._request(
            "POST",
            path,
            f"{MONZO_API_URL}{path}",
            data=data,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def _put(
        self, path: str, access_token: str, *, data: dict[str, str]
    ) -> httpx.Response:
        return await self._request(
            "PUT",
            path,
            f"{MONZO_API_URL}{path}",
            data=data,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def _request(self, method: str, path: str, url: str, **kwargs) -> httpx.Response:
        # Mask resource identifiers embedded in transfer endpoints.
        endpoint = re.sub(r"(/pots/)[^/]+(/(?:deposit|withdraw))", r"\1{id}\2", path)
        started_at = monotonic()
        logger.info("monzo_request_started method=%s endpoint=%s", method, endpoint)
        try:
            response = await self.http.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            logger.error(
                "monzo_request_transport_failed method=%s endpoint=%s exception_type=%s duration_ms=%d",
                method,
                endpoint,
                type(exc).__name__,
                round((monotonic() - started_at) * 1000),
            )
            raise
        logger.info(
            "monzo_request_completed method=%s endpoint=%s status_code=%d duration_ms=%d",
            method,
            endpoint,
            response.status_code,
            round((monotonic() - started_at) * 1000),
        )
        return response

    async def revoke_access(self, access_token: str) -> None:
        response = await self._post("/oauth2/logout", access_token, data={})
        response.raise_for_status()

    async def get_accounts(
        self, access_token: str, account_type: str | None = None
    ) -> MonzoAccountsResponse:
        return await self._resource(
            self._accounts_response(access_token, account_type),
            MonzoAccountsResponse,
            "accounts",
        )

    async def get_balance(self, access_token: str, account_id: str) -> BalanceResponse:
        return await self._resource(
            self._balance_response(access_token, account_id), BalanceResponse, "balance"
        )

    async def get_pots(
        self, access_token: str, current_account_id: str
    ) -> PotsResponse:
        return await self._resource(
            self._pots_response(access_token, current_account_id), PotsResponse, "pots"
        )

    async def get_balances(
        self, access_token: str, account_ids: list[str]
    ) -> list[BalanceResponse | None]:
        return list(
            await asyncio.gather(
                *(
                    self._optional_balance(access_token, account_id)
                    for account_id in account_ids
                )
            )
        )

    async def _optional_balance(
        self, access_token: str, account_id: str
    ) -> BalanceResponse | None:
        try:
            return await self.get_balance(access_token, account_id)
        except MonzoError:
            return None

    async def _resource[T: BaseModel](
        self, request: Awaitable[httpx.Response], schema: type[T], operation: str
    ) -> T:
        try:
            response = await request
        except httpx.RequestError:
            logger.error(
                "monzo_request_failed operation=%s reason=monzo_unreachable", operation
            )
            raise MonzoUnavailableError("Monzo API is unreachable") from None
        if response.is_error:
            error_code, error_message = monzo_error_details(response)
            logger.warning(
                "monzo_request_failed operation=%s upstream_status=%d monzo_code=%r monzo_message=%r",
                operation,
                response.status_code,
                error_code,
                error_message,
            )
            approval_required = False
            if response.status_code == 403:
                try:
                    payload = response.json()
                    approval_required = (
                        isinstance(payload, dict)
                        and payload.get("code") == "forbidden.insufficient_permissions"
                    )
                except ValueError, TypeError:
                    pass
            raise MonzoRequestError(
                response.status_code, approval_required=approval_required
            )
        try:
            return schema.model_validate(response.json())
        except ValidationError as exc:
            schema_errors = ",".join(
                f"{'.'.join(str(part) for part in error['loc'])}:{error['type']}"
                for error in exc.errors(include_input=False)
            )
            logger.error(
                "monzo_request_failed operation=%s reason=invalid_response schema_errors=%s",
                operation,
                schema_errors,
            )
            raise MonzoInvalidResponseError(
                "Monzo returned an invalid response"
            ) from None
        except ValueError as exc:
            logger.error(
                "monzo_request_failed operation=%s reason=invalid_json exception_type=%s",
                operation,
                type(exc).__name__,
            )
            raise MonzoInvalidResponseError(
                "Monzo returned an invalid response"
            ) from None


@asynccontextmanager
async def monzo_client_scope(
    client: MonzoClient | None = None,
) -> AsyncIterator[MonzoClient]:
    """Reuse an injected transport, or own one for this request/job event loop."""
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=15.0) as http:
            yield MonzoClient(http)
