"""Explicit authentication contexts with credentials hidden from representations."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthenticationContext:
    user_id: str
    session_token: str = field(repr=False)
    app_session_id: str | None = None


@dataclass(frozen=True)
class MonzoSession:
    user_id: str
    access_token: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    app_session_id: str | None = None
