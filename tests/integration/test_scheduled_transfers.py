from datetime import datetime, timedelta, timezone
import asyncio
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import jwt
import pytest
import respx

from app.db.models import (
    MonzoCredential,
    ScheduledTransfer,
    ScheduledTransferSetup,
)
from app.domain.time import UK_TIMEZONE
from app.services.transfer_execution import execute_scheduled_transfer
from app.services.token_store import encrypt_token
from app.services.monzo import deposit_into_pot


def _session_token(settings, user_id="user_test123"):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "ver": 0, "iat": now, "exp": now + timedelta(hours=1)},
        settings.jwt_secret_key,
        algorithm="HS256",
    )


def _save_credential(client):
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        session.add(
            MonzoCredential(
                user_id="user_test123",
                access_token=encrypt_token("test-access-token", client.app.state.settings),
                refresh_token=encrypt_token("test-refresh-token", client.app.state.settings),
                token_type="Bearer",
                expires_at=now + timedelta(hours=1),
                updated_at=now,
            )
        )
        session.commit()


def test_schedule_transfer_endpoint_persists_authenticated_users_task(
    client, settings
):
    _save_credential(client)
    scheduled_at = (datetime.now(UK_TIMEZONE) + timedelta(days=2)).replace(
        second=0, microsecond=0
    )

    response = client.post(
        "/schedule-transfer",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
        json={
            "datetime": scheduled_at.isoformat(),
            "interval": "monthly",
            "type": "deposit",
            "amount": 1250,
            "pot_id": "pot_123",
            "account_id": "acc_123",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert UUID(body["setup_id"]).version == 6
    assert UUID(body["transfer_id"]).version == 6
    assert datetime.fromisoformat(body["scheduled_for"]) == scheduled_at
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert body["interval"] == "monthly"
    assert body["type"] == "deposit"
    assert body["amount"] == 1250
    assert body["setup_status"] == "active"
    assert body["executed_at"] is None

    with client.app.state.session_factory() as session:
        setup = session.get(ScheduledTransferSetup, body["setup_id"])
        transfer = session.get(ScheduledTransfer, body["transfer_id"])
        assert setup is not None
        assert transfer is not None
        assert setup.user_id == "user_test123"
        assert setup.status == "active"
        assert setup.scheduled_date == scheduled_at.date()
        assert setup.hour == scheduled_at.hour
        assert setup.minute == scheduled_at.minute
        assert setup.interval == "monthly"
        assert setup.transfer_type == "deposit"
        assert setup.amount == 1250
        assert setup.pot_id == "pot_123"
        assert setup.account_id == "acc_123"
        assert transfer.setup_id == setup.setup_id
        assert transfer.status == "pending"
        assert transfer.executed_at is None


def test_emergency_stop_cancels_pending_transfers_and_revokes_sessions(client, settings):
    _save_credential(client)
    token = _session_token(settings)
    headers = {"Authorization": f"Bearer {token}"}
    scheduled_at = (datetime.now(UK_TIMEZONE) + timedelta(days=2)).replace(
        second=0, microsecond=0
    )
    created = client.post(
        "/schedule-transfer",
        headers=headers,
        json={
            "datetime": scheduled_at.isoformat(),
            "interval": "weekly",
            "type": "deposit",
            "amount": 1250,
            "pot_id": "pot_123",
            "account_id": "acc_123",
        },
    ).json()

    stopped = client.post("/emergency-stop", headers=headers)
    old_session = client.get("/scheduled-transfers", headers=headers)

    assert stopped.status_code == 204
    assert old_session.status_code == 401
    assert client.app.state.scheduler.get_job(created["transfer_id"]) is None
    with client.app.state.session_factory() as session:
        setup = session.get(ScheduledTransferSetup, created["setup_id"])
        transfer = session.get(ScheduledTransfer, created["transfer_id"])
        assert setup.status == "deactivated"
        assert transfer.status == "cancelled"


def test_schedule_endpoint_enforces_active_schedule_quota(client, settings):
    _save_credential(client)
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        session.add_all(
            [
                ScheduledTransferSetup(
                    setup_id=f"quota-setup-{index}",
                    user_id="user_test123",
                    scheduled_date=(now + timedelta(days=2)).date(),
                    hour=9,
                    minute=index % 60,
                    interval="daily",
                    transfer_type="deposit",
                    amount=100,
                    pot_id="pot_123",
                    account_id="acc_123",
                    status="active",
                )
                for index in range(50)
            ]
        )
        session.commit()
    scheduled_at = (datetime.now(UK_TIMEZONE) + timedelta(days=2)).replace(
        second=0, microsecond=0
    )

    response = client.post(
        "/schedule-transfer",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
        json={
            "datetime": scheduled_at.isoformat(),
            "interval": "weekly",
            "type": "deposit",
            "amount": 100,
            "pot_id": "pot_123",
            "account_id": "acc_123",
        },
    )

    assert response.status_code == 429


@pytest.mark.parametrize("pot_id", ["../accounts", "pot/a", "pot?x=1"])
def test_schedule_endpoint_rejects_pot_ids_that_can_change_api_path(
    client, settings, pot_id
):
    _save_credential(client)
    scheduled_at = (datetime.now(UK_TIMEZONE) + timedelta(days=2)).replace(
        second=0, microsecond=0
    )
    response = client.post(
        "/schedule-transfer",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
        json={
            "datetime": scheduled_at.isoformat(),
            "interval": "weekly",
            "type": "deposit",
            "amount": 100,
            "pot_id": pot_id,
            "account_id": "acc_123",
        },
    )

    assert response.status_code == 422


def test_monzo_deposit_rejects_legacy_traversal_ids_before_request():
    with pytest.raises(ValueError, match="Invalid Monzo resource identifier"):
        asyncio.run(
            deposit_into_pot(
                "unused-access-token", "../accounts", "acc_123", 100, "dedupe"
            )
        )


def test_get_scheduled_transfers_lists_default_statuses_for_authenticated_user(
    client, settings
):
    _save_credential(client)
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        session.add(
            MonzoCredential(
                user_id="another-user",
                access_token=encrypt_token("another-access-token", client.app.state.settings),
                refresh_token=None,
                token_type="Bearer",
                expires_at=now + timedelta(hours=1),
                updated_at=now,
            )
        )
        session.add_all(
            [
                ScheduledTransferSetup(
                    setup_id="own-active",
                    user_id="user_test123",
                    scheduled_date=now.date(),
                    hour=10,
                    minute=30,
                    interval="weekly",
                    transfer_type="deposit",
                    amount=750,
                    pot_id="pot-own",
                    account_id="account-own",
                    status="active",
                ),
                ScheduledTransferSetup(
                    setup_id="own-inactive",
                    user_id="user_test123",
                    scheduled_date=now.date(),
                    hour=11,
                    minute=30,
                    interval="daily",
                    transfer_type="withdraw",
                    amount=100,
                    pot_id="pot-inactive",
                    account_id="account-own",
                    status="deactivated",
                ),
                ScheduledTransferSetup(
                    setup_id="own-active-later",
                    user_id="user_test123",
                    scheduled_date=now.date(),
                    hour=11,
                    minute=0,
                    interval="daily",
                    transfer_type="withdraw",
                    amount=125,
                    pot_id="pot-own-later",
                    account_id="account-own",
                    status="active",
                ),
                ScheduledTransferSetup(
                    setup_id="other-active",
                    user_id="another-user",
                    scheduled_date=now.date(),
                    hour=12,
                    minute=30,
                    interval="monthly",
                    transfer_type="withdraw",
                    amount=200,
                    pot_id="pot-other",
                    account_id="account-other",
                    status="active",
                ),
                ScheduledTransfer(
                    transfer_id="own-transfer",
                    setup_id="own-active",
                    scheduled_for=now + timedelta(days=1),
                    created_at=now,
                    status="pending",
                ),
                ScheduledTransfer(
                    transfer_id="inactive-transfer",
                    setup_id="own-inactive",
                    scheduled_for=now + timedelta(days=2),
                    status="cancelled",
                ),
                ScheduledTransfer(
                    transfer_id="own-transfer-later",
                    setup_id="own-active-later",
                    scheduled_for=now + timedelta(days=2),
                    created_at=now - timedelta(hours=1),
                    executed_at=now,
                    status="failed",
                ),
                ScheduledTransfer(
                    transfer_id="other-transfer",
                    setup_id="other-active",
                    scheduled_for=now + timedelta(days=3),
                    status="pending",
                ),
            ]
        )
        session.commit()

    response = client.get(
        "/scheduled-transfers",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "setup_id": "own-active",
                "transfer_id": "own-transfer",
                "scheduled_for": (now + timedelta(days=1))
                .astimezone(UK_TIMEZONE)
                .isoformat(),
                "created_at": now.astimezone(UK_TIMEZONE).isoformat(),
                "interval": "weekly",
                "type": "deposit",
                "amount": 750,
                "setup_status": "active",
                "status": "pending",
                "executed_at": None,
            },
            {
                "setup_id": "own-active-later",
                "transfer_id": "own-transfer-later",
                "scheduled_for": (now + timedelta(days=2))
                .astimezone(UK_TIMEZONE)
                .isoformat(),
                "created_at": (now - timedelta(hours=1))
                .astimezone(UK_TIMEZONE)
                .isoformat(),
                "interval": "daily",
                "type": "withdraw",
                "amount": 125,
                "setup_status": "active",
                "status": "failed",
                "executed_at": now.astimezone(UK_TIMEZONE).isoformat(),
            },
        ],
        "total": 2,
        "limit": 50,
        "offset": 0,
    }

    paginated_response = client.get(
        "/scheduled-transfers?limit=1&offset=1",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert paginated_response.status_code == 200
    paginated_body = paginated_response.json()
    assert paginated_body["total"] == 2
    assert paginated_body["limit"] == 1
    assert paginated_body["offset"] == 1
    assert [item["transfer_id"] for item in paginated_body["items"]] == [
        "own-transfer-later"
    ]

    filtered_response = client.get(
        "/scheduled-transfers?status=cancelled",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert filtered_response.status_code == 200
    filtered_body = filtered_response.json()
    assert filtered_body["total"] == 1
    assert [item["transfer_id"] for item in filtered_body["items"]] == [
        "inactive-transfer"
    ]

    account_filtered_response = client.get(
        "/scheduled-transfers",
        params={"status": "cancelled", "account_id": "missing-account"},
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert account_filtered_response.status_code == 200
    assert account_filtered_response.json()["total"] == 0

    pot_filtered_response = client.get(
        "/scheduled-transfers",
        params={"pot_id": "pot-own"},
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert pot_filtered_response.status_code == 200
    pot_filtered_body = pot_filtered_response.json()
    assert pot_filtered_body["total"] == 1
    assert [item["transfer_id"] for item in pot_filtered_body["items"]] == [
        "own-transfer"
    ]

    resource_filtered_response = client.get(
        "/scheduled-transfers",
        params={"account_id": "account-own", "pot_id": "pot-own"},
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert resource_filtered_response.status_code == 200
    resource_filtered_body = resource_filtered_response.json()
    assert resource_filtered_body["total"] == 1
    assert [
        item["transfer_id"] for item in resource_filtered_body["items"]
    ] == ["own-transfer"]

    combined_filtered_response = client.get(
        "/scheduled-transfers",
        params={
            "status": "cancelled",
            "account_id": "account-own",
            "pot_id": "pot-inactive",
        },
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert combined_filtered_response.status_code == 200
    combined_filtered_body = combined_filtered_response.json()
    assert combined_filtered_body["total"] == 1
    assert [
        item["transfer_id"] for item in combined_filtered_body["items"]
    ] == ["inactive-transfer"]


def test_get_scheduled_transfers_requires_authentication(client):
    response = client.get("/scheduled-transfers")

    assert response.status_code == 401


@pytest.mark.parametrize(
    (
        "transfer_type",
        "path",
        "account_field",
        "past_tense_action",
    ),
    [
        (
            "deposit",
            "/pots/pot_123/deposit",
            "source_account_id",
            "deposited",
        ),
        (
            "withdraw",
            "/pots/pot_123/withdraw",
            "destination_account_id",
            "withdrawn",
        ),
    ],
)
def test_scheduled_transfer_uses_monzo_pot_api_form_fields(
    client,
    settings,
    transfer_type,
    path,
    account_field,
    past_tense_action,
):
    _save_credential(client)
    setup_id = f"setup-{transfer_type}"
    transfer_id = f"transfer-{transfer_type}"
    with client.app.state.session_factory() as session:
        session.add_all(
            [
                ScheduledTransferSetup(
                    setup_id=setup_id,
                    user_id="user_test123",
                    scheduled_date=datetime.now(UK_TIMEZONE).date(),
                    hour=9,
                    minute=30,
                    interval="daily",
                    transfer_type=transfer_type,
                    amount=1250,
                    pot_id="pot_123",
                    account_id="acc_123",
                    status="active",
                ),
                ScheduledTransfer(
                    transfer_id=transfer_id,
                    setup_id=setup_id,
                    scheduled_for=datetime.now(timezone.utc),
                    status="pending",
                ),
            ]
        )
        session.commit()

    with respx.mock(assert_all_called=True) as monzo_mock:
        transfer = monzo_mock.put(f"https://api.monzo.com{path}").mock(
            return_value=httpx.Response(200, json={"name": "Rainy Day"})
        )
        feed = monzo_mock.post("https://api.monzo.com/feed").mock(
            return_value=httpx.Response(200, json={})
        )
        execute_scheduled_transfer(
            transfer_id,
            client.app.state.scheduler,
            client.app.state.session_factory,
            settings,
        )

    assert transfer.calls.last.request.headers["Authorization"] == (
        "Bearer test-access-token"
    )
    form = parse_qs(transfer.calls.last.request.content.decode())
    assert form[account_field] == ["acc_123"]
    assert form["amount"] == ["1250"]
    assert form["dedupe_id"] == [transfer_id]
    assert feed.calls.last.request.headers["Authorization"] == (
        "Bearer test-access-token"
    )
    feed_form = parse_qs(feed.calls.last.request.content.decode())
    assert feed_form == {
        "account_id": ["acc_123"],
        "type": ["basic"],
        "params[title]": [f"🎉 £12.50 {past_tense_action}"],
        "params[image_url]": [
            "https://raw.githubusercontent.com/wchr-aun/monzo-scheduler-ui/"
            "refs/heads/main/public/logo.png"
        ],
        "params[body]": [
            "Balance → Rainy Day"
            if transfer_type == "deposit"
            else "Rainy Day → Balance"
        ],
    }

    with client.app.state.session_factory() as session:
        completed = session.get(ScheduledTransfer, transfer_id)
        transfers = session.query(ScheduledTransfer).filter_by(setup_id=setup_id).all()
        assert completed is not None
        assert completed.status == "completed"
        assert completed.executed_at is not None
        assert len(transfers) == 2
        assert [item.status for item in transfers].count("pending") == 1


def test_cancelling_setup_deactivates_it_and_cancels_pending_transfer(
    client, settings
):
    _save_credential(client)
    scheduled_at = (datetime.now(UK_TIMEZONE) + timedelta(days=2)).replace(
        second=0, microsecond=0
    )
    created = client.post(
        "/schedule-transfer",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
        json={
            "datetime": scheduled_at.isoformat(),
            "interval": "weekly",
            "type": "withdraw",
            "amount": 500,
            "pot_id": "pot_123",
            "account_id": "acc_123",
        },
    ).json()

    response = client.delete(
        f"/schedule-transfer/{created['setup_id']}",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert client.app.state.scheduler.get_job(created["transfer_id"]) is None
    with client.app.state.session_factory() as session:
        setup = session.get(ScheduledTransferSetup, created["setup_id"])
        transfer = session.get(ScheduledTransfer, created["transfer_id"])
        assert setup is not None
        assert setup.status == "deactivated"
        assert transfer is not None
        assert transfer.status == "cancelled"
        assert transfer.executed_at is None


def test_failed_occurrence_is_recorded_and_next_occurrence_is_pending(
    client, settings
):
    _save_credential(client)
    setup_id = "setup-failed"
    transfer_id = "transfer-failed"
    with client.app.state.session_factory() as session:
        session.add_all(
            [
                ScheduledTransferSetup(
                    setup_id=setup_id,
                    user_id="user_test123",
                    scheduled_date=datetime.now(UK_TIMEZONE).date(),
                    hour=9,
                    minute=30,
                    interval="daily",
                    transfer_type="deposit",
                    amount=1250,
                    pot_id="pot_123",
                    account_id="acc_123",
                    status="active",
                ),
                ScheduledTransfer(
                    transfer_id=transfer_id,
                    setup_id=setup_id,
                    scheduled_for=datetime.now(timezone.utc),
                    status="pending",
                ),
            ]
        )
        session.commit()

    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.put("https://api.monzo.com/pots/pot_123/deposit").mock(
            return_value=httpx.Response(
                500,
                json={"code": "internal_service_error", "message": "Try again"},
            )
        )
        feed = monzo_mock.post("https://api.monzo.com/feed").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(httpx.HTTPStatusError):
            execute_scheduled_transfer(
                transfer_id,
                client.app.state.scheduler,
                client.app.state.session_factory,
                settings,
            )

    feed_form = parse_qs(feed.calls.last.request.content.decode())
    assert feed_form["params[title]"] == ["❌ £12.50 deposit failed"]
    assert feed_form["params[body]"] == ["Balance → Pot"]
    assert feed_form["url"] == [
        "https://monzo-scheduler-ui.vercel.app/account/acc_123/pot/pot_123"
    ]

    with client.app.state.session_factory() as session:
        transfers = session.query(ScheduledTransfer).filter_by(setup_id=setup_id).all()
        failed = session.get(ScheduledTransfer, transfer_id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.executed_at is not None
        assert len(transfers) == 2
        assert [item.status for item in transfers].count("pending") == 1


def test_three_executions_leave_three_completed_and_one_pending(client, settings):
    _save_credential(client)
    setup_id = "setup-repeated"
    with client.app.state.session_factory() as session:
        session.add_all(
            [
                ScheduledTransferSetup(
                    setup_id=setup_id,
                    user_id="user_test123",
                    scheduled_date=datetime.now(UK_TIMEZONE).date(),
                    hour=9,
                    minute=30,
                    interval="daily",
                    transfer_type="deposit",
                    amount=100,
                    pot_id="pot_123",
                    account_id="acc_123",
                    status="active",
                ),
                ScheduledTransfer(
                    transfer_id="transfer-first",
                    setup_id=setup_id,
                    scheduled_for=datetime.now(timezone.utc),
                    status="pending",
                ),
            ]
        )
        session.commit()

    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.put("https://api.monzo.com/pots/pot_123/deposit").mock(
            return_value=httpx.Response(200, json={})
        )
        monzo_mock.post("https://api.monzo.com/feed").mock(
            return_value=httpx.Response(200, json={})
        )
        for _ in range(3):
            with client.app.state.session_factory() as session:
                pending = session.query(ScheduledTransfer).filter_by(
                    setup_id=setup_id,
                    status="pending",
                ).one()
                transfer_id = pending.transfer_id
            execute_scheduled_transfer(
                transfer_id,
                client.app.state.scheduler,
                client.app.state.session_factory,
                settings,
            )

    with client.app.state.session_factory() as session:
        transfers = session.query(ScheduledTransfer).filter_by(setup_id=setup_id).all()
        assert len(transfers) == 4
        assert [item.status for item in transfers].count("completed") == 3
        assert [item.status for item in transfers].count("pending") == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("datetime", "2030-07-01T09:30:00Z"),
        ("datetime", "2030-01-01T09:30:01Z"),
        ("interval", "yearly"),
        ("interval", "on_date"),
        ("type", "move"),
        ("amount", 0),
        ("amount", 1.5),
    ],
)
def test_schedule_transfer_rejects_invalid_body(client, settings, field, value):
    _save_credential(client)
    body = {
        "datetime": "2030-01-01T09:30:00Z",
        "interval": "weekly",
        "type": "deposit",
        "amount": 500,
        "pot_id": "pot_123",
        "account_id": "acc_123",
    }
    body[field] = value

    response = client.post(
        "/schedule-transfer",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
        json=body,
    )

    assert response.status_code == 422


def test_schedule_transfer_rejects_past_datetime(client, settings):
    _save_credential(client)
    past = (datetime.now(UK_TIMEZONE) - timedelta(days=1)).replace(
        second=0, microsecond=0
    )

    response = client.post(
        "/schedule-transfer",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
        json={
            "datetime": past.isoformat(),
            "interval": "monthly",
            "type": "deposit",
            "amount": 500,
            "pot_id": "pot_123",
            "account_id": "acc_123",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "datetime must be in the future"}


def test_schedule_transfer_requires_authentication(client):
    response = client.post(
        "/schedule-transfer",
        json={
            "datetime": "2030-01-01T09:30:00Z",
            "interval": "monthly",
            "type": "deposit",
            "amount": 500,
            "pot_id": "pot_123",
            "account_id": "acc_123",
        },
    )

    assert response.status_code == 401
