from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from time import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.dependencies import monzo_session
from app.domain.authentication import MonzoSession
from app.domain.transfers import (
    ScheduledTransferDetails,
    ScheduledTransfersPage,
    ScheduleTransferCommand,
)
from app.main import create_app
from app.routers import tasks
from app.services.monzo import MonzoClient
from app.services.oauth_state import consume_oauth_state, create_oauth_state


@contextmanager
def _client_for_settings(settings):
    if not settings.token_encryption_key:
        settings = replace(
            settings,
            token_encryption_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        )
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with TestClient(create_app(settings, engine=engine)) as client:
        yield client
    engine.dispose()


def test_health_route(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"message": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_health_route_adds_hsts_over_https(client):
    response = client.get("https://testserver/health")

    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_schedule_transfer_calls_scheduler_service(monkeypatch, client):
    scheduled_for = datetime(2030, 1, 1, 9, 30, tzinfo=timezone.utc)
    transfer = ScheduledTransferDetails(
        setup_id="setup-123",
        transfer_id="transfer-123",
        scheduled_for=scheduled_for,
        created_at=scheduled_for,
        interval="weekly",
        transfer_type="withdraw",
        amount=500,
        setup_status="active",
        status="pending",
        executed_at=None,
    )
    called = {}

    def fake_schedule(scheduler, session_factory, settings, user_id, request, **kwargs):
        called.update(
            scheduler=scheduler,
            session_factory=session_factory,
            settings=settings,
            user_id=user_id,
            request=request,
        )
        return transfer

    monkeypatch.setattr(tasks, "schedule_transfer", fake_schedule)
    client.app.dependency_overrides[monzo_session] = lambda: MonzoSession(
        user_id="user_123", access_token="unused"
    )
    response = client.post(
        "/schedule-transfer",
        json={
            "datetime": "2030-01-01T09:30:00Z",
            "interval": "weekly",
            "type": "withdraw",
            "amount": 500,
            "pot_id": "pot_123",
            "account_id": "acc_123",
        },
    )
    client.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "transfer_id": "transfer-123",
        "setup_id": "setup-123",
        "scheduled_for": scheduled_for.isoformat().replace("+00:00", "Z"),
        "created_at": scheduled_for.isoformat().replace("+00:00", "Z"),
        "interval": "weekly",
        "type": "withdraw",
        "amount": 500,
        "setup_status": "active",
        "executed_at": None,
        "status": "pending",
    }
    assert called["scheduler"] is client.app.state.resources.transfer_jobs
    assert called["session_factory"] is client.app.state.resources.session_factory
    assert called["settings"] is client.app.state.resources.settings
    assert called["user_id"] == "user_123"
    assert isinstance(called["request"], ScheduleTransferCommand)
    assert called["request"].transfer_type == "withdraw"


def test_get_scheduled_transfers_calls_scheduler_service(monkeypatch, client):
    scheduled_for = datetime(2030, 1, 1, 9, 30, tzinfo=timezone.utc)
    transfer = type(
        "ScheduledTransferDetails",
        (),
        {
            "setup_id": "setup-123",
            "transfer_id": "transfer-123",
            "scheduled_for": scheduled_for,
            "created_at": scheduled_for,
            "interval": "weekly",
            "transfer_type": "withdraw",
            "amount": 500,
            "setup_status": "deactivated",
            "status": "failed",
            "executed_at": scheduled_for,
        },
    )()
    called = {}

    def fake_list(
        session_factory,
        user_id,
        *,
        statuses,
        account_id,
        pot_id,
        limit,
        offset,
    ):
        called.update(
            session_factory=session_factory,
            user_id=user_id,
            statuses=statuses,
            account_id=account_id,
            pot_id=pot_id,
            limit=limit,
            offset=offset,
        )
        return ScheduledTransfersPage(
            items=[transfer], total=3, limit=limit, offset=offset
        )

    monkeypatch.setattr(tasks, "list_scheduled_transfers", fake_list)
    client.app.dependency_overrides[monzo_session] = lambda: MonzoSession(
        user_id="user_123", access_token="unused"
    )

    response = client.get(
        "/scheduled-transfers",
        params={
            "account_id": "account-123",
            "pot_id": "pot-123",
            "limit": 20,
            "offset": 2,
        },
    )
    client.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "setup_id": "setup-123",
                "transfer_id": "transfer-123",
                "scheduled_for": scheduled_for.isoformat().replace("+00:00", "Z"),
                "created_at": scheduled_for.isoformat().replace("+00:00", "Z"),
                "interval": "weekly",
                "type": "withdraw",
                "amount": 500,
                "setup_status": "deactivated",
                "status": "failed",
                "executed_at": scheduled_for.isoformat().replace("+00:00", "Z"),
            }
        ],
        "total": 3,
        "limit": 20,
        "offset": 2,
    }
    assert called["session_factory"] is client.app.state.resources.session_factory
    assert called["user_id"] == "user_123"
    assert called["account_id"] == "account-123"
    assert called["pot_id"] == "pot-123"
    assert [status.value for status in called["statuses"]] == [
        "pending",
        "running",
        "completed",
        "failed",
    ]
    assert called["limit"] == 20
    assert called["offset"] == 2


def test_get_scheduled_transfers_passes_status_filter(monkeypatch, client):
    called = {}

    def fake_list(
        session_factory,
        user_id,
        *,
        statuses,
        account_id,
        pot_id,
        limit,
        offset,
    ):
        called["statuses"] = statuses
        called["account_id"] = account_id
        called["pot_id"] = pot_id
        return ScheduledTransfersPage(items=[], total=0, limit=limit, offset=offset)

    monkeypatch.setattr(tasks, "list_scheduled_transfers", fake_list)
    client.app.dependency_overrides[monzo_session] = lambda: MonzoSession(
        user_id="user_123", access_token="unused"
    )

    response = client.get("/scheduled-transfers?status=pending,failed,pending")
    client.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [status.value for status in called["statuses"]] == ["pending", "failed"]
    assert called["account_id"] is None
    assert called["pot_id"] is None


def test_get_scheduled_transfers_rejects_invalid_status(client):
    client.app.dependency_overrides[monzo_session] = lambda: MonzoSession(
        user_id="user_123", access_token="unused"
    )

    response = client.get("/scheduled-transfers?status=unknown")
    client.app.dependency_overrides.clear()

    assert response.status_code == 422


def test_create_task_endpoint_is_removed(client):
    response = client.post("/create-task")

    assert response.status_code == 404


def test_monzo_redirect_requires_client_id(settings):
    settings = replace(settings, monzo_client_id="")

    with _client_for_settings(settings) as client:
        response = client.get("/monzo-redirect")

    assert response.status_code == 503
    assert response.json()["detail"] == "Monzo OAuth is not configured"


def test_monzo_callback_rejects_unknown_state(client):
    response = client.get("/monzo-callback", params={"code": "code", "state": "bad"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired OAuth state"


def test_monzo_callback_rejects_expired_state(client):
    state = create_oauth_state(client.app.state.resources.settings, now=time() - 601)
    client.cookies.set("monzo_oauth_state", state, path="/monzo-callback")
    response = client.get("/monzo-callback", params={"code": "code", "state": state})

    assert response.status_code == 400


def test_monzo_callback_requires_both_credentials(settings):
    settings = replace(settings, monzo_client_secret="")
    with _client_for_settings(settings) as client:
        state = create_oauth_state(client.app.state.resources.settings)
        client.cookies.set("monzo_oauth_state", state, path="/monzo-callback")
        response = client.get(
            "/monzo-callback", params={"code": "code", "state": state}
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Monzo OAuth is not configured"


def test_monzo_callback_requires_jwt_configuration(settings):
    incomplete_settings = replace(settings, jwt_secret_key="")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        with _client_for_settings(incomplete_settings):
            pass


@pytest.mark.parametrize("token_response", [{}, {"user_id": "user-1"}])
def test_monzo_callback_rejects_incomplete_token_payload(
    monkeypatch, client, token_response
):
    async def fake_exchange(self, code, settings):
        from app.domain.monzo import MonzoTokenResponse

        return MonzoTokenResponse.model_validate(token_response)

    monkeypatch.setattr(MonzoClient, "exchange_authorization_code", fake_exchange)
    state = create_oauth_state(client.app.state.resources.settings)
    client.cookies.set("monzo_oauth_state", state, path="/monzo-callback")
    response = client.get("/monzo-callback", params={"code": "code", "state": state})

    assert response.status_code == 502
    assert response.json()["detail"] == "Monzo returned an invalid token response"


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_detail"),
    [
        (
            httpx.HTTPStatusError(
                "bad response",
                request=httpx.Request("POST", "https://api.monzo.com/oauth2/token"),
                response=httpx.Response(422),
            ),
            422,
            "Monzo token exchange failed",
        ),
        (
            httpx.ConnectError("offline"),
            503,
            "Monzo API is unreachable",
        ),
    ],
)
def test_monzo_callback_maps_upstream_errors(
    monkeypatch, client, failure, expected_status, expected_detail
):
    async def fail_exchange(self, code, settings):
        raise failure

    monkeypatch.setattr(MonzoClient, "exchange_authorization_code", fail_exchange)
    state = create_oauth_state(client.app.state.resources.settings)
    client.cookies.set("monzo_oauth_state", state, path="/monzo-callback")

    response = client.get("/monzo-callback", params={"code": "code", "state": state})

    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
    assert not consume_oauth_state(
        state,
        state,
        client.app.state.resources.settings,
        client.app.state.resources.session_factory,
    )
