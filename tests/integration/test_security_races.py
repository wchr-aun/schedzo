from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select, func

from app.db.models import ScheduledTransferSetup
from app.schemas.monzo import MonzoTokenResponse
from app.services.token_store import save_monzo_tokens
from app.services.schedules import schedule_transfer

BODY = {
    "datetime": "2030-01-01T10:00:00Z",
    "interval": "daily",
    "type": "withdraw",
    "amount": 100,
    "pot_id": "pot_audit",
    "account_id": "acc_audit",
}


def login(client, settings):
    with client.app.state.session_factory() as session:
        return save_monzo_tokens(
            MonzoTokenResponse(
                user_id="audit-user", access_token="synthetic-token", expires_in=3600
            ),
            session,
            settings,
        )


@pytest.mark.parametrize("operation", ["/emergency-stop", "/logout"])
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
    with client.app.state.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(ScheduledTransferSetup))
            == 0
        )


def test_emergency_pause_survives_reauthentication_until_explicit_resume(
    client, settings
):
    pair = login(client, settings)
    assert (
        client.post(
            "/emergency-stop", headers={"Authorization": f"Bearer {pair.access_token}"}
        ).status_code
        == 204
    )
    fresh = login(client, settings)
    headers = {"Authorization": f"Bearer {fresh.access_token}"}
    assert (
        client.post("/schedule-transfer", headers=headers, json=BODY).status_code == 409
    )
    assert client.post("/resume-transfers", headers=headers).status_code == 204
    assert (
        client.post("/schedule-transfer", headers=headers, json=BODY).status_code == 200
    )
