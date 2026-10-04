"""Application authentication and credential errors independent of transport."""


class SessionAuthenticationError(Exception):
    """The application session token is missing or invalid."""


class MonzoConnectionError(Exception):
    """The user does not have usable Monzo credentials."""


class TokenStorageError(Exception):
    """Stored Monzo credentials could not be read or updated."""


class MonzoTokenResponseError(Exception):
    """Monzo returned an unusable token response."""


class MonzoDisconnectPendingError(Exception):
    """Provider revocation must complete before a new login is saved."""


class AppSessionQuotaError(Exception):
    """Persistent session issuance or refresh budget exceeded."""
