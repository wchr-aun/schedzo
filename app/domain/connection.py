"""Monzo connection state values persisted for each user."""

from enum import StrEnum


class ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    REVOCATION_PENDING = "revocation_pending"
    DISCONNECTED = "disconnected"
