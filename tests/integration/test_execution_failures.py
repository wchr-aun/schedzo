import respx

from app.db.models import ScheduledTransfer
from app.services.transfer_execution import execute_scheduled_transfer
from tests.integration.test_security_races import BODY, login


def test_notification_failure_keeps_transfer_result_and_duplicate_execution_is_skipped(
    client, settings
):
    pair = login(client, settings)
    response = client.post(
        "/schedule-transfer",
        json=BODY,
        headers={"Authorization": f"Bearer {pair.access_token}"},
    )
    assert response.status_code == 200
    transfer_id = response.json()["transfer_id"]
    resources = client.app.state.resources
    with respx.mock() as mock:
        withdrawal = mock.put("https://api.monzo.com/pots/pot_audit/withdraw").respond(
            200, json={}
        )
        feed = mock.post("https://api.monzo.com/feed").respond(500)
        for _ in range(2):
            execute_scheduled_transfer(
                transfer_id, resources.transfer_jobs, resources.session_factory, settings
            )
        assert withdrawal.call_count == 1
        assert feed.call_count == 1
    with resources.session_factory() as session:
        assert session.get(ScheduledTransfer, transfer_id).status == "completed"
        assert session.query(ScheduledTransfer).filter_by(status="pending").count() == 1
