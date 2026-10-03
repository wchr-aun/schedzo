"""HTTP adapters for authenticated Monzo resource workflows."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from app.dependencies import get_resource_service, monzo_access_token
from app.domain.monzo_errors import (
    MonzoError,
    MonzoUnavailableError,
    MonzoInvalidResponseError,
    MonzoRequestError,
)
from app.schemas.monzo import (
    AccountsWithBalancesResponse,
    BalanceResponse,
    PotsResponse,
)
from app.services.resources import ResourceService

router = APIRouter(tags=["monzo"])


@router.get("/accounts-with-balances", response_model=AccountsWithBalancesResponse)
async def accounts_with_balances(
    account_type: str | None = None,
    access_token: str = Depends(monzo_access_token),
    service: ResourceService = Depends(get_resource_service),
) -> AccountsWithBalancesResponse | Response:
    try:
        return await service.accounts_with_balances(access_token, account_type)
    except MonzoError as exc:
        return _resource_error_response(exc)


@router.get("/balance", response_model=BalanceResponse)
async def balance(
    account_id: str = Query(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$"),
    access_token: str = Depends(monzo_access_token),
    service: ResourceService = Depends(get_resource_service),
) -> BalanceResponse | Response:
    try:
        return await service.balance(access_token, account_id)
    except MonzoError as exc:
        return _resource_error_response(exc)


@router.get("/pots", response_model=PotsResponse)
async def pots(
    current_account_id: str = Query(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$"
    ),
    access_token: str = Depends(monzo_access_token),
    service: ResourceService = Depends(get_resource_service),
) -> PotsResponse | Response:
    try:
        return await service.pots(access_token, current_account_id)
    except MonzoError as exc:
        return _resource_error_response(exc)


def _resource_error_response(error: MonzoError) -> Response:
    if isinstance(error, MonzoUnavailableError):
        raise HTTPException(
            status_code=503, detail="Monzo API is unreachable"
        ) from None
    if isinstance(error, MonzoInvalidResponseError):
        raise HTTPException(
            status_code=502, detail="Monzo returned an invalid response"
        ) from None
    if isinstance(error, MonzoRequestError):
        if error.approval_required:
            return JSONResponse(
                {
                    "detail": {
                        "code": "monzo_approval_required",
                        "message": "You have not yet allowed access to your data. Please allow access to your data in the Monzo app.",
                    }
                },
                status_code=403,
            )
        return JSONResponse(
            {"detail": "Monzo request failed"}, status_code=error.status_code
        )
    raise error
