import httpx
import respx

from app.db.models import MonzoCredential
from app.domain.connection import ConnectionStatus
from app.services.disconnection import retry_monzo_disconnection
from tests.integration.test_security_races import login


def test_emergency_stop_revokes_monzo_and_erases_stored_credentials(
    client, settings, mock_monzo_disconnection
):
    pair = login(client, settings)
    route = mock_monzo_disconnection
    response = client.post(
        "/disconnect", headers={"Authorization": f"Bearer {pair.access_token}"}
    )
    assert response.status_code == 204
    assert route.calls.last.request.headers["Authorization"] == "Bearer synthetic-token"
    with client.app.state.resources.session_factory() as session:
        row = session.get(MonzoCredential, "audit-user")
        assert (
            row.connection_status == ConnectionStatus.DISCONNECTED
            and row.scheduling_paused
        )
        assert row.access_token == "" and row.refresh_token is None


def test_provider_outage_keeps_connection_blocked_and_retries(
    client, settings, mock_monzo_disconnection
):
    pair = login(client, settings)
    mock_monzo_disconnection.mock(side_effect=httpx.ConnectError("offline"))
    response = client.post(
        "/disconnect", headers={"Authorization": f"Bearer {pair.access_token}"}
    )
    assert response.status_code == 202
    with client.app.state.resources.session_factory() as session:
        row = session.get(MonzoCredential, "audit-user")
        assert (
            row.connection_status == ConnectionStatus.REVOCATION_PENDING
            and row.scheduling_paused
        )
    mock_monzo_disconnection.mock(return_value=httpx.Response(200))
    assert retry_monzo_disconnection(
        "audit-user", client.app.state.resources.session_factory, settings
    )
    with client.app.state.resources.session_factory() as session:
        assert (
            session.get(MonzoCredential, "audit-user").connection_status
            == ConnectionStatus.DISCONNECTED
        )


def test_expired_connection_is_renewed_then_revoked(
    client, settings, mock_monzo_disconnection
):
    from datetime import datetime, timedelta, timezone

    from app.services.token_crypto import encrypt_token

    pair = login(client, settings)
    with client.app.state.resources.session_factory() as session:
        credential = session.get(MonzoCredential, "audit-user")
        credential.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        credential.refresh_token = encrypt_token("synthetic-refresh", settings)
        session.commit()
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.monzo.com/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "renewed-access",
                    "refresh_token": "renewed-refresh",
                    "user_id": "audit-user",
                    "expires_in": 3600,
                },
            )
        )
        assert (
            client.post(
                "/disconnect", headers={"Authorization": f"Bearer {pair.access_token}"}
            ).status_code
            == 204
        )
    assert (
        mock_monzo_disconnection.calls.last.request.headers["Authorization"]
        == "Bearer renewed-access"
    )


def test_revoked_grant_can_finish_disconnection_without_permanent_retry(
    client, settings, mock_monzo_disconnection
):
    from datetime import datetime, timedelta, timezone

    from app.services.token_crypto import encrypt_token

    pair = login(client, settings)
    with client.app.state.resources.session_factory() as session:
        credential = session.get(MonzoCredential, "audit-user")
        credential.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        credential.refresh_token = encrypt_token("synthetic-invalid-refresh", settings)
        session.commit()
    mock_monzo_disconnection.mock(return_value=httpx.Response(401))
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.monzo.com/oauth2/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        assert (
            client.post(
                "/disconnect", headers={"Authorization": f"Bearer {pair.access_token}"}
            ).status_code
            == 204
        )
    with client.app.state.resources.session_factory() as session:
        assert (
            session.get(MonzoCredential, "audit-user").connection_status
            == ConnectionStatus.DISCONNECTED
        )


def test_inflight_monzo_refresh_preserves_new_tokens_for_revocation(
    client, settings, monkeypatch, mock_monzo_disconnection
):
    import asyncio
    from datetime import datetime, timedelta, timezone

    import pytest

    from app.schemas.monzo import MonzoTokenResponse
    from app.services.monzo_credentials import (
        MonzoConnectionError,
        resolve_monzo_access_token,
    )
    from app.services.token_crypto import encrypt_token

    login(client, settings)
    with client.app.state.resources.session_factory() as session:
        credential = session.get(MonzoCredential, "audit-user")
        credential.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        credential.refresh_token = encrypt_token("old-provider-refresh", settings)
        session.commit()

    async def provider_refresh(*args):
        with client.app.state.resources.session_factory() as session:
            credential = session.get(MonzoCredential, "audit-user")
            credential.connection_status = ConnectionStatus.REVOCATION_PENDING
            session.commit()
        return MonzoTokenResponse(
            user_id="audit-user",
            access_token="inflight-new-access",
            refresh_token="inflight-new-refresh",
            expires_in=3600,
        )

    monkeypatch.setattr(
        "app.services.monzo.MonzoClient.refresh_access_token", provider_refresh
    )
    with pytest.raises(MonzoConnectionError):
        asyncio.run(
            resolve_monzo_access_token(
                "audit-user", client.app.state.resources.session_factory, settings
            )
        )
    assert retry_monzo_disconnection(
        "audit-user", client.app.state.resources.session_factory, settings
    )
    assert (
        mock_monzo_disconnection.calls.last.request.headers["Authorization"]
        == "Bearer inflight-new-access"
    )
