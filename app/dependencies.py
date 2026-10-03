"""Shared HTTP dependencies; services receive explicit values instead of requests."""

from typing import Never

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.db.session import SessionFactory
from app.domain.authentication import AuthenticationContext, MonzoSession
from app.domain.errors import (
    MonzoConnectionError,
    MonzoTokenResponseError,
    SessionAuthenticationError,
    TokenStorageError,
)
from app.observability import get_logger
from app.rate_limit import RequestRateLimiter
from app.services.authorization import authenticate_session
from app.services.monzo import MonzoClient
from app.services.monzo_credentials import resolve_monzo_access_token
from app.services.oauth import OAuthService
from app.services.resources import ResourceService

logger = get_logger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_factory(request: Request) -> SessionFactory:
    return request.app.state.session_factory


def get_scheduler(request: Request) -> BackgroundScheduler:
    return request.app.state.scheduler


def _get_monzo_client(request: Request) -> MonzoClient:
    return request.app.state.monzo_client


def get_resource_service(
    client: MonzoClient = Depends(_get_monzo_client),
) -> ResourceService:
    return ResourceService(client)


def get_oauth_service(
    client: MonzoClient = Depends(_get_monzo_client),
    session_factory: SessionFactory = Depends(get_session_factory),
    settings: Settings = Depends(get_settings),
) -> OAuthService:
    return OAuthService(client, session_factory, settings)


def get_oauth_start_rate_limiter(request: Request) -> RequestRateLimiter:
    return request.app.state.oauth_start_rate_limiter


def authenticated_session(
    request: Request,
    authorization: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    session_factory: SessionFactory = Depends(get_session_factory),
) -> AuthenticationContext:
    if authorization is None:
        logger.warning(
            "authentication_failed path=%s reason=bearer_token_missing",
            request.url.path,
        )
        _raise_unauthorized("Bearer token required")

    try:
        return authenticate_session(
            authorization.credentials, settings, session_factory
        )
    except SessionAuthenticationError:
        logger.warning(
            "authentication_failed path=%s reason=invalid_or_expired_jwt",
            request.url.path,
        )
        _raise_unauthorized("Invalid or expired bearer token")
    except TokenStorageError as exc:
        logger.error(
            "credential_resolution_failed path=%s reason=storage_or_configuration",
            request.url.path,
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def monzo_session(
    request: Request,
    authentication: AuthenticationContext = Depends(authenticated_session),
    settings: Settings = Depends(get_settings),
    session_factory: SessionFactory = Depends(get_session_factory),
    client: MonzoClient = Depends(_get_monzo_client),
) -> MonzoSession:
    try:
        access_token = await resolve_monzo_access_token(
            authentication.user_id,
            session_factory,
            settings,
            client=client,
        )
        return MonzoSession(
            user_id=authentication.user_id,
            access_token=access_token,
            session_token=authentication.session_token,
            app_session_id=authentication.app_session_id,
        )
    except MonzoConnectionError:
        logger.warning(
            "authentication_failed path=%s reason=monzo_connection_unavailable",
            request.url.path,
        )
        _raise_unauthorized("Monzo connection is missing or expired")
    except TokenStorageError as exc:
        logger.error(
            "credential_resolution_failed path=%s reason=storage_or_configuration",
            request.url.path,
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MonzoTokenResponseError as exc:
        logger.error(
            "credential_refresh_failed path=%s reason=invalid_response",
            request.url.path,
        )
        raise HTTPException(
            status_code=502,
            detail="Monzo returned an invalid token response",
        ) from exc
    except httpx.RequestError as exc:
        logger.error(
            "credential_refresh_failed path=%s reason=monzo_unreachable",
            request.url.path,
        )
        raise HTTPException(status_code=503, detail="Monzo API is unreachable") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail="Monzo token refresh failed"
        ) from exc


async def monzo_access_token(
    authentication: MonzoSession = Depends(monzo_session),
) -> str:
    return authentication.access_token


def _raise_unauthorized(detail: str) -> Never:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
