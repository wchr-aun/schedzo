from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.session import SessionFactory
from app.dependencies import (
    authenticated_session,
    get_session_factory,
    get_settings,
    get_transfer_jobs,
    monzo_session,
)
from app.domain.authentication import AuthenticationContext, MonzoSession
from app.domain.errors import SessionAuthenticationError
from app.domain.scheduling import TransferJobs
from app.domain.transfers import (
    InvalidScheduleError,
    ScheduledTransferDetails,
    ScheduleNotFoundError,
    ScheduleQuotaExceededError,
    SchedulingPausedError,
    TransferStatus,
)
from app.schemas.tasks import (
    ScheduledTransferResponse,
    ScheduledTransfersPageResponse,
    ScheduleTransferRequest,
)
from app.services.disconnection import disconnect_user
from app.services.schedules import (
    cancel_scheduled_transfer,
    list_scheduled_transfers,
    resume_user_scheduling,
    schedule_transfer,
)
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
    authentication: AuthenticationContext = Depends(authenticated_session),
    settings: Settings = Depends(get_settings),
    session_factory: SessionFactory = Depends(get_session_factory),
) -> Response:
    try:
        logout_session(
            authentication,
            session_factory,
            settings,
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
    authentication: AuthenticationContext = Depends(authenticated_session),
    settings: Settings = Depends(get_settings),
    session_factory: SessionFactory = Depends(get_session_factory),
    scheduler: TransferJobs = Depends(get_transfer_jobs),
) -> Response:
    try:
        disconnected = disconnect_user(
            scheduler,
            session_factory,
            settings,
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
    status: Annotated[
        str | None,
        Query(description="Comma-separated transfer statuses"),
    ] = None,
    account_id: Annotated[str | None, Query(min_length=1)] = None,
    pot_id: Annotated[str | None, Query(min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    authentication: MonzoSession = Depends(monzo_session),
    session_factory: SessionFactory = Depends(get_session_factory),
) -> ScheduledTransfersPageResponse:
    statuses = _parse_transfer_statuses(status)
    try:
        page = list_scheduled_transfers(
            session_factory,
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
    authentication: MonzoSession = Depends(monzo_session),
    settings: Settings = Depends(get_settings),
    session_factory: SessionFactory = Depends(get_session_factory),
    scheduler: TransferJobs = Depends(get_transfer_jobs),
) -> ScheduledTransferResponse:
    try:
        transfer = schedule_transfer(
            scheduler,
            session_factory,
            settings,
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
    authentication: MonzoSession = Depends(monzo_session),
    session_factory: SessionFactory = Depends(get_session_factory),
    scheduler: TransferJobs = Depends(get_transfer_jobs),
) -> Response:
    try:
        cancel_scheduled_transfer(
            scheduler,
            session_factory,
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
    authentication: AuthenticationContext = Depends(authenticated_session),
    settings: Settings = Depends(get_settings),
    session_factory: SessionFactory = Depends(get_session_factory),
) -> Response:
    user_id = authentication.user_id
    try:
        resume_user_scheduling(
            user_id,
            authentication.session_token,
            session_factory,
            settings,
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
