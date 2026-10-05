from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import func, select

from app.db.models import ScheduledTransferSetup
from app.schemas.monzo import MonzoTokenResponse
from app.services.schedules import schedule_transfer
from app.services.sessions import issue_app_session

BODY = {
    "datetime": "2030-01-01T10:00:00Z",
    "interval": "daily",
    "type": "withdraw",
    "amount": 100,
    "pot_id": "pot_audit",
    "account_id": "acc_audit",
}


def login(client, settings):
    return issue_app_session(
        MonzoTokenResponse(
            user_id="audit-user", access_token="synthetic-token", expires_in=3600
        ),
        client.app.state.resources.session_factory,
        settings,
    )


@pytest.mark.parametrize("operation", ["/disconnect", "/logout"])
def test_authenticated_creation_cannot_survive_revocation(
    client, settings, monkeypatch, operation
):
    pair = login(client, settings)
    headers = {"Authorization": f"Bearer {pair.access_token}"}
    entered, release = Event(), Event()

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return schedule_transfer(*args, **kwargs)

    monkeypatch.setattr("app.routers.tasks.schedule_transfer", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post, "/schedule-transfer", headers=headers, json=BODY
        )
        try:
            assert entered.wait(5)
            assert client.post(operation, headers=headers).status_code == 204
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 401
    with client.app.state.resources.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(ScheduledTransferSetup))
            == 0
        )


def test_reconnection_allows_new_schedules_but_does_not_reactivate_old_setups(
    client, settings
):
    pair = login(client, settings)
    headers = {"Authorization": f"Bearer {pair.access_token}"}
    existing = client.post("/schedule-transfer", headers=headers, json=BODY).json()
    assert (
        client.post(
            "/disconnect", headers=headers
        ).status_code
        == 204
    )
    fresh = login(client, settings)
    fresh_headers = {"Authorization": f"Bearer {fresh.access_token}"}
    assert (
        client.post(
            "/schedule-transfer", headers=fresh_headers, json=BODY
        ).status_code
        == 200
    )
    with client.app.state.resources.session_factory() as session:
        setup = session.get(ScheduledTransferSetup, existing["setup_id"])
        assert setup.status == "deactivated"
