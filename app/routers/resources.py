"""Authenticated pass-through routes for Monzo account resources."""

from collections.abc import Awaitable
from typing import Literal, TypeVar

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.observability import get_logger, monzo_error_details
from app.schemas.monzo import (
    AccountsWithBalancesResponse,
    AccountWithBalance,
    BalanceResponse,
    MonzoAccountsResponse,
    PotsResponse,
)
from app.dependencies import monzo_access_token
from app.services.monzo import (
    BalanceResult,
    get_accounts,
    get_balance,
    get_balances,
    get_pots,
)

router = APIRouter(tags=["monzo"])
logger = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)
ResponseValueT = TypeVar("ResponseValueT")
MonzoOperation = Literal["accounts", "balance", "pots"]


@router.get(
    "/accounts-with-balances",
    response_model=AccountsWithBalancesResponse,
)
async def accounts_with_balances(
    account_type: str | None = None,
    access_token: str = Depends(monzo_access_token),
) -> AccountsWithBalancesResponse | Response:
    accounts_response = await _validate_response(
        get_accounts(access_token, account_type),
        MonzoAccountsResponse,
        operation="accounts",
    )
    if isinstance(accounts_response, Response):
        return accounts_response

    balance_responses = await get_balances(
        access_token,
        [account.id for account in accounts_response.accounts],
    )
    accounts_with_balances: list[AccountWithBalance] = []
    for account, balance_response in zip(
        accounts_response.accounts, balance_responses, strict=True
    ):
        balance = _optional_balance(balance_response)
        accounts_with_balances.append(
            AccountWithBalance(
                **account.model_dump(),
                balance_details=balance,
            )
        )

    return AccountsWithBalancesResponse(accounts=accounts_with_balances)


def _optional_balance(response: BalanceResult) -> BalanceResponse | None:
    if isinstance(response, httpx.RequestError):
        logger.error(
            "monzo_request_failed operation=balance reason=monzo_unreachable",
        )
        return None

    try:
        balance = _validate_completed_response(
            response,
            BalanceResponse,
            operation="balance",
        )
    except HTTPException:
        return None

    return balance if isinstance(balance, BalanceResponse) else None


@router.get("/balance", response_model=BalanceResponse)
async def balance(
    account_id: str = Query(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$"),
    access_token: str = Depends(monzo_access_token),
) -> BalanceResponse | Response:
    return await _validate_response(
        get_balance(access_token, account_id),
        BalanceResponse,
        operation="balance",
    )


@router.get("/pots", response_model=PotsResponse)
async def pots(
    current_account_id: str = Query(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$"
    ),
    access_token: str = Depends(monzo_access_token),
) -> PotsResponse | Response:
    return await _validate_response(
        get_pots(access_token, current_account_id),
        PotsResponse,
        operation="pots",
    )


async def _validate_response(
    response_awaitable: Awaitable[httpx.Response],
    schema: type[SchemaT],
    *,
    operation: MonzoOperation,
) -> SchemaT | Response:
    response = await _await_monzo_response(response_awaitable, operation=operation)
    return _validate_completed_response(response, schema, operation=operation)


async def _await_monzo_response(
    response_awaitable: Awaitable[ResponseValueT],
    *,
    operation: MonzoOperation,
) -> ResponseValueT:
    try:
        return await response_awaitable
    except httpx.RequestError as exc:
        logger.error(
            "monzo_request_failed operation=%s reason=monzo_unreachable",
            operation,
        )
        raise HTTPException(status_code=503, detail="Monzo API is unreachable") from exc


def _validate_completed_response(
    response: httpx.Response,
    schema: type[SchemaT],
    *,
    operation: MonzoOperation,
) -> SchemaT | Response:
    if response.is_error:
        error_code, error_message = monzo_error_details(response)
        logger.warning(
            "monzo_request_failed operation=%s upstream_status=%d "
            "monzo_code=%r monzo_message=%r",
            operation,
            response.status_code,
            error_code,
            error_message,
        )
        if _requires_monzo_approval(response):
            return JSONResponse(
                content={
                    "detail": {
                        "code": "monzo_approval_required",
                        "message": "You have not yet allowed access to your data. Please allow access to your data in the Monzo app.",
                    }
                },
                status_code=status.HTTP_403_FORBIDDEN,
            )
        return JSONResponse(
            content={"detail": "Monzo request failed"},
            status_code=response.status_code,
        )

    try:
        return schema.model_validate(response.json())
    except ValidationError as exc:
        schema_errors = ",".join(
            f"{'.'.join(str(part) for part in error['loc'])}:{error['type']}"
            for error in exc.errors(include_input=False)
        )
        logger.error(
            "monzo_request_failed operation=%s reason=invalid_response "
            "schema_errors=%s",
            operation,
            schema_errors,
        )
        raise HTTPException(
            status_code=502,
            detail="Monzo returned an invalid response",
        ) from exc
    except ValueError as exc:
        logger.error(
            "monzo_request_failed operation=%s reason=invalid_json exception_type=%s",
            operation,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Monzo returned an invalid response",
        ) from exc


def _requires_monzo_approval(response: httpx.Response) -> bool:
    """Recognize the one Monzo error that has a specific user action."""
    if response.status_code != status.HTTP_403_FORBIDDEN:
        return False
    try:
        payload = response.json()
    except ValueError, TypeError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("code") == "forbidden.insufficient_permissions"
    )
