"""Application session results with credentials hidden from representations."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AppTokenPair:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    refresh_expires_in: int
