from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.exc import SQLAlchemyError

from app.dependencies import authenticated_session, monzo_session
from app.domain.authentication import AuthenticationContext, MonzoSession
from app.schemas.tasks import (
    ScheduleTransferRequest,
    ScheduledTransferResponse,
    ScheduledTransfersPageResponse,
)
from app.domain.transfers import ScheduledTransferDetails, TransferStatus
from app.services.schedules import (
    InvalidScheduleError,
    SchedulingPausedError,
    resume_user_scheduling,
    ScheduleNotFoundError,
    ScheduleQuotaExceededError,
    cancel_scheduled_transfer,
    list_scheduled_transfers,
    schedule_transfer,
)

from app.domain.errors import SessionAuthenticationError

from app.services.disconnection import disconnect_user
from fastapi.responses import JSONResponse
from app.services.sessions import logout_session

router = APIRouter(tags=["tasks"])

DEFAULT_TRANSFER_STATUSES = (
    TransferStatus.PENDING,
    TransferStatus.RUNNING,
    TransferStatus.COMPLETED,
    TransferStatus.FAILED,
)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    authentication: AuthenticationContext = Depends(authenticated_session),
) -> Response:
    user_id = authentication.user_id
    try:
        logout_session(
            authentication,
            request.app.state.session_factory,
            request.app.state.settings,
        )
    except SessionAuthenticationError:
        raise HTTPException(status_code=401, detail="Session revoked") from None
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="Session storage is unavailable"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/disconnect", status_code=status.HTTP_204_NO_CONTENT)
@router.post("/emergency-stop", status_code=status.HTTP_204_NO_CONTENT)
def emergency_stop(
    request: Request,
    authentication: AuthenticationContext = Depends(authenticated_session),
) -> Response:
    try:
        disconnected = disconnect_user(
            request.app.state.scheduler,
            request.app.state.session_factory,
            request.app.state.settings,
            authentication,
        )
    except SessionAuthenticationError:
        raise HTTPException(status_code=401, detail="Session revoked") from None
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="Scheduled transfer storage is unavailable"
        ) from exc
    if not disconnected:
        return JSONResponse(
            {"detail": "Schedules stopped; Monzo disconnection pending"},
            status_code=202,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _parse_transfer_statuses(value: str | None) -> tuple[TransferStatus, ...]:
    if value is None:
        return DEFAULT_TRANSFER_STATUSES

    try:
        statuses = tuple(TransferStatus(status.strip()) for status in value.split(","))
    except ValueError as exc:
        allowed = ", ".join(status.value for status in TransferStatus)
        raise HTTPException(
            status_code=422,
            detail=f"Invalid transfer status. Allowed values: {allowed}",
        ) from exc

    if not statuses:
        raise HTTPException(status_code=422, detail="At least one status is required")
    return tuple(dict.fromkeys(statuses))


@router.get(
    "/scheduled-transfers",
    response_model=ScheduledTransfersPageResponse,
)
def get_scheduled_transfers(
    request: Request,
    status: Annotated[
        str | None,
        Query(description="Comma-separated transfer statuses"),
    ] = None,
    account_id: Annotated[str | None, Query(min_length=1)] = None,
    pot_id: Annotated[str | None, Query(min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    authentication: MonzoSession = Depends(monzo_session),
) -> ScheduledTransfersPageResponse:
    statuses = _parse_transfer_statuses(status)
    try:
        page = list_scheduled_transfers(
            request.app.state.session_factory,
            authentication.user_id,
            statuses=statuses,
            account_id=account_id,
            pot_id=pot_id,
            limit=limit,
            offset=offset,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Scheduled transfer storage is unavailable",
        ) from exc

    return ScheduledTransfersPageResponse(
        items=[_transfer_response(transfer) for transfer in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("/schedule-transfer", response_model=ScheduledTransferResponse)
def create_scheduled_transfer(
    transfer_request: ScheduleTransferRequest,
    request: Request,
    authentication: MonzoSession = Depends(monzo_session),
) -> ScheduledTransferResponse:
    try:
        transfer = schedule_transfer(
            request.app.state.scheduler,
            request.app.state.session_factory,
            request.app.state.settings,
            authentication.user_id,
            transfer_request.to_command(),
            session_token=authentication.session_token,
        )
    except SessionAuthenticationError:
        raise HTTPException(status_code=401, detail="Session revoked") from None
    except SchedulingPausedError:
        raise HTTPException(
            status_code=409,
            detail="Scheduling is paused; explicitly resume before creating transfers",
        ) from None
    except InvalidScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ScheduleQuotaExceededError as exc:
        raise HTTPException(status_code=429, detail="Schedule quota reached") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Scheduled transfer storage is unavailable",
        ) from exc

    return _transfer_response(transfer)


@router.delete(
    "/schedule-transfer/{setup_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def cancel_transfer_schedule(
    setup_id: str,
    request: Request,
    authentication: MonzoSession = Depends(monzo_session),
) -> Response:
    try:
        cancel_scheduled_transfer(
            request.app.state.scheduler,
            request.app.state.session_factory,
            authentication.user_id,
            setup_id,
        )
    except ScheduleNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Scheduled transfer not found"
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Scheduled transfer storage is unavailable",
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/resume-transfers", status_code=204)
def resume_transfers(
    request: Request,
    authentication: AuthenticationContext = Depends(authenticated_session),
):
    user_id = authentication.user_id
    try:
        resume_user_scheduling(
            user_id,
            authentication.session_token,
            request.app.state.session_factory,
            request.app.state.settings,
        )
    except SessionAuthenticationError:
        raise HTTPException(status_code=401, detail="Session revoked") from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503, detail="Session storage is unavailable"
        ) from None
    return Response(status_code=204)


def _transfer_response(transfer: ScheduledTransferDetails) -> ScheduledTransferResponse:
    return ScheduledTransferResponse(
        setup_id=transfer.setup_id,
        transfer_id=transfer.transfer_id,
        scheduled_for=transfer.scheduled_for,
        created_at=transfer.created_at,
        interval=transfer.interval,
        type=transfer.transfer_type,
        amount=transfer.amount,
        setup_status=transfer.setup_status,
        status=transfer.status,
        executed_at=transfer.executed_at,
    )
