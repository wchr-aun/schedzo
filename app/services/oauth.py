"""Monzo login workflow independent of HTTP callback handling."""

from pydantic import ValidationError

from app.config import Settings
from app.db.session import SessionFactory
from app.domain.sessions import AppTokenPair
from app.services.monzo import MonzoClient
from app.services.sessions import issue_app_session


class OAuthTokenResponseError(Exception):
    """Monzo returned an unusable authorization-code exchange response."""


class OAuthService:
    def __init__(
        self,
        client: MonzoClient,
        session_factory: SessionFactory,
        settings: Settings,
    ):
        self._client = client
        self._session_factory = session_factory
        self._settings = settings

    async def complete_monzo_login(self, code: str) -> AppTokenPair:
        """Exchange an authorization code and persist an application session."""
        try:
            token_response = await self._client.exchange_authorization_code(
                code, self._settings
            )
        except ValidationError, ValueError:
            raise OAuthTokenResponseError from None

        return issue_app_session(token_response, self._session_factory, self._settings)
