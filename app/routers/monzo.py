import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.session import SessionFactory
from app.dependencies import (
    get_oauth_service,
    get_oauth_start_rate_limiter,
    get_session_factory,
    get_settings,
)
from app.observability import monzo_error_details
from app.rate_limit import RequestRateLimiter
from app.schemas.monzo import AppRefreshRequest
from app.services.oauth import OAuthService, OAuthTokenResponseError
from app.services.oauth_state import (
    consume_oauth_state,
    create_oauth_state,
)
from app.services.token_store import (
    AppSessionQuotaError,
    AppTokenPair,
    MonzoDisconnectPendingError,
    rotate_app_refresh_token,
)

router = APIRouter(tags=["monzo"])
logger = logging.getLogger("schedzo.oauth")


def _token_pair_response(
    token_pair: AppTokenPair, settings: Settings
) -> dict[str, str | int]:
    return {
        "token": token_pair.access_token,
        "expiresIn": token_pair.expires_in,
        "refreshToken": token_pair.refresh_token,
        "refreshExpiresIn": token_pair.refresh_expires_in,
    }


@router.get("/monzo-redirect")
def monzo_redirect(
    request: Request,
    settings: Settings = Depends(get_settings),
    rate_limiter: RequestRateLimiter = Depends(get_oauth_start_rate_limiter),
):
    if not settings.monzo_client_id:
        logger.error("oauth_redirect_failed reason=oauth_not_configured")
        raise HTTPException(status_code=503, detail="Monzo OAuth is not configured")

    client_host = request.client.host if request.client is not None else "unknown"
    if not rate_limiter.allow(client_host):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts",
            headers={"Retry-After": "600"},
        )
    state = create_oauth_state(settings)
    url = httpx.URL(
        "https://auth.monzo.com/",
        params={
            "client_id": settings.monzo_client_id,
            "redirect_uri": settings.monzo_redirect_uri,
            "response_type": "code",
            "state": state,
        },
    )
    response = RedirectResponse(str(url), status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        "monzo_oauth_state",
        state,
        httponly=True,
        secure=urlparse(settings.monzo_redirect_uri).scheme == "https",
        samesite="lax",
        path="/monzo-callback",
        max_age=600,
    )
    return response


@router.get("/monzo-callback")
async def monzo_callback(
    request: Request,
    code: str,
    state: str,
    service: OAuthService = Depends(get_oauth_service),
    settings: Settings = Depends(get_settings),
    session_factory: SessionFactory = Depends(get_session_factory),
):
    try:
        valid_state = consume_oauth_state(
            state,
            request.cookies.get("monzo_oauth_state", ""),
            settings,
            session_factory,
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503, detail="OAuth state storage is unavailable"
        ) from None
    if not valid_state:
        logger.warning("oauth_callback_failed reason=invalid_state")
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    if not settings.monzo_client_id or not settings.monzo_client_secret:
        logger.error("oauth_callback_failed reason=oauth_not_configured")
        raise HTTPException(status_code=503, detail="Monzo OAuth is not configured")
    if not settings.jwt_secret_key:
        logger.error("oauth_callback_failed reason=session_signing_not_configured")
        raise HTTPException(status_code=503, detail="Session signing is not configured")
    try:
        token_pair = await service.complete_monzo_login(code)
    except httpx.HTTPStatusError as exc:
        error_code, error_message = monzo_error_details(exc.response)
        logger.warning(
            "oauth_token_exchange_failed upstream_status=%d "
            "monzo_code=%r monzo_message=%r",
            exc.response.status_code,
            error_code,
            error_message,
        )
        raise HTTPException(
            status_code=exc.response.status_code, detail="Monzo token exchange failed"
        ) from exc
    except httpx.RequestError as exc:
        logger.error(
            "oauth_token_exchange_failed reason=monzo_unreachable",
        )
        raise HTTPException(status_code=503, detail="Monzo API is unreachable") from exc
    except OAuthTokenResponseError as exc:
        logger.error("oauth_token_exchange_failed reason=invalid_response")
        raise HTTPException(
            status_code=502, detail="Monzo returned an invalid token response"
        ) from exc

    except MonzoDisconnectPendingError:
        raise HTTPException(
            status_code=409,
            detail="Monzo disconnection is pending; retry login after revocation completes",
        ) from None
    except AppSessionQuotaError:
        raise HTTPException(
            status_code=429, detail="Session issuance quota reached"
        ) from None
    except SQLAlchemyError as exc:
        logger.error("oauth_callback_failed reason=token_storage_unavailable")
        raise HTTPException(
            status_code=503, detail="Token storage is unavailable"
        ) from exc

    response = JSONResponse(
        {
            **_token_pair_response(token_pair, settings),
        },
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )
    response.delete_cookie("monzo_oauth_state", path="/monzo-callback")
    return response


@router.post("/auth/refresh")
def refresh_app_session(body: AppRefreshRequest, request: Request):
    settings = request.app.state.settings
    try:
        token_pair = rotate_app_refresh_token(
            body.refresh_token, request.app.state.session_factory, settings
        )
    except AppSessionQuotaError:
        raise HTTPException(
            status_code=429,
            detail="Session refresh quota reached",
            headers={"Retry-After": "3600"},
        ) from None
    except SQLAlchemyError as exc:
        logger.error("app_token_refresh_failed reason=session_storage_unavailable")
        raise HTTPException(
            status_code=503, detail="Session storage is unavailable"
        ) from exc
    if token_pair is None:
        logger.warning(
            "app_token_refresh_failed reason=invalid_or_expired_refresh_token"
        )
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    return JSONResponse(
        _token_pair_response(token_pair, settings),
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
