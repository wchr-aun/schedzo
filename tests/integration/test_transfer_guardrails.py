from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx

from app.db.models import AppSession, ScheduledTransfer
from app.services.transfer_execution import execute_scheduled_transfer
from app.services.token_store import rotate_app_refresh_token
from tests.integration.test_security_races import login, BODY


def test_large_transfers_are_accepted_but_storage_overflow_is_rejected(
    client, settings
):
    pair = login(client, settings)
    headers = {"Authorization": f"Bearer {pair.access_token}"}
    assert (
        client.post(
            "/schedule-transfer", headers=headers, json={**BODY, "amount": 2**63 - 1}
        ).status_code
        == 200
    )
    for amount in (2**63, 0, -1):
        assert (
            client.post(
                "/schedule-transfer", headers=headers, json={**BODY, "amount": amount}
            ).status_code
            == 422
        )


def test_old_login_can_create_and_resume_with_valid_session(client, settings):
    pair = login(client, settings)
    with client.app.state.session_factory() as session:
        row = session.query(AppSession).one()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=100)
        session.commit()
    rotated = rotate_app_refresh_token(
        pair.refresh_token, client.app.state.session_factory, settings
    )
    headers = {"Authorization": f"Bearer {rotated.access_token}"}
    assert (
        client.post("/schedule-transfer", headers=headers, json=BODY).status_code == 200
    )
    assert client.post("/resume-transfers", headers=headers).status_code == 204


def test_execution_has_no_application_monetary_budget(client, settings, monkeypatch):
    pair = login(client, settings)
    headers = {"Authorization": f"Bearer {pair.access_token}"}
    body = {**BODY, "amount": 600_000}
    first = client.post("/schedule-transfer", headers=headers, json=body).json()
    second = client.post("/schedule-transfer", headers=headers, json=body).json()
    with client.app.state.session_factory() as session:
        previous = session.get(ScheduledTransfer, first["transfer_id"])
        previous.status = "completed"
        previous.executed_at = datetime.now(timezone.utc)
        session.commit()
    withdrawal = AsyncMock(
        return_value=httpx.Response(
            200,
            json={},
            request=httpx.Request(
                "PUT", "https://api.monzo.com/pots/pot_audit/withdraw"
            ),
        )
    )
    monkeypatch.setattr("app.services.transfer_execution.withdraw_from_pot", withdrawal)
    monkeypatch.setattr(
        "app.services.notifications.create_feed_item",
        AsyncMock(
            return_value=httpx.Response(
                200,
                json={},
                request=httpx.Request("POST", "https://api.monzo.com/feed"),
            )
        ),
    )
    execute_scheduled_transfer(
        second["transfer_id"],
        client.app.state.scheduler,
        client.app.state.session_factory,
        settings,
    )
    withdrawal.assert_awaited_once()
    assert withdrawal.call_args.args[3] == 600_000
    with client.app.state.session_factory() as session:
        assert (
            session.get(ScheduledTransfer, second["transfer_id"]).status == "completed"
        )
