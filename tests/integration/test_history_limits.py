from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.models import (
    AppSession,
    ScheduledTransfer,
    ScheduledTransferSetup,
    UsedAppRefreshToken,
)
from app.services.maintenance import prune_history
from app.services.sessions import rotate_app_refresh_token
from tests.integration.test_security_races import BODY, login


def test_cancelled_schedules_still_count_toward_creation_quota(client, settings):
    pair = login(client, settings)
    with client.app.state.resources.session_factory() as session:
        session.add_all(
            [
                ScheduledTransferSetup(
                    user_id="audit-user",
                    scheduled_date=datetime.now().date(),
                    hour=9,
                    minute=0,
                    interval="daily",
                    transfer_type="deposit",
                    amount=100,
                    pot_id="pot_audit",
                    account_id="acc_audit",
                    status="deactivated",
                )
                for _ in range(100)
            ]
        )
        session.commit()
    response = client.post(
        "/schedule-transfer",
        headers={"Authorization": f"Bearer {pair.access_token}"},
        json=BODY,
    )
    assert response.status_code == 429


def test_refresh_quota_does_not_rotate_or_revoke_session(client, settings, monkeypatch):
    pair = login(client, settings)
    monkeypatch.setattr("app.services.sessions.MAX_REFRESHES_PER_USER_PER_HOUR", 1)
    rotated = rotate_app_refresh_token(
        pair.refresh_token, client.app.state.resources.session_factory, settings
    )
    response = client.post(
        "/auth/refresh", json={"refreshToken": rotated.refresh_token}
    )
    assert response.status_code == 429
    monkeypatch.setattr("app.services.sessions.MAX_REFRESHES_PER_USER_PER_HOUR", 60)
    assert (
        rotate_app_refresh_token(
            rotated.refresh_token, client.app.state.resources.session_factory, settings
        )
        is not None
    )


def test_pruning_removes_revoked_sessions_but_preserves_all_transfer_history(
    client, settings
):
    pair = login(client, settings)
    rotate_app_refresh_token(
        pair.refresh_token, client.app.state.resources.session_factory, settings
    )
    old = datetime.now(timezone.utc) - timedelta(days=3650)
    statuses = {"completed", "failed", "cancelled", "pending", "running"}
    with client.app.state.resources.session_factory() as session:
        row = session.query(AppSession).one()
        row.revoked_at = old
        setup = ScheduledTransferSetup(
            user_id="audit-user",
            scheduled_date=old.date(),
            hour=9,
            minute=0,
            interval="daily",
            transfer_type="deposit",
            amount=100,
            pot_id="pot_audit",
            account_id="acc_audit",
            status="deactivated",
            created_at=old,
        )
        session.add(setup)
        session.flush()
        session.add_all(
            [
                ScheduledTransfer(
                    setup_id=setup.setup_id, scheduled_for=old, status=status
                )
                for status in statuses
            ]
        )
        session.add(
            ScheduledTransferSetup(
                user_id="audit-user",
                scheduled_date=old.date(),
                hour=10,
                minute=0,
                interval="daily",
                transfer_type="deposit",
                amount=100,
                pot_id="pot_audit",
                account_id="acc_audit",
                status="deactivated",
                created_at=old,
            )
        )
        session.commit()
    prune_history(client.app.state.resources.session_factory)
    with client.app.state.resources.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AppSession)) == 0
        assert (
            session.scalar(select(func.count()).select_from(UsedAppRefreshToken)) == 0
        )
        transfers = session.query(ScheduledTransfer).all()
        assert len(transfers) == len(statuses)
        assert {transfer.status for transfer in transfers} == statuses
        assert session.query(ScheduledTransferSetup).count() == 2


def test_pruning_removes_inactive_sessions_but_preserves_live_sessions(
    client, settings
):
    from app.schemas.monzo import MonzoTokenResponse
    from app.services.sessions import issue_app_session

    pair = login(client, settings)
    rotate_app_refresh_token(
        pair.refresh_token, client.app.state.resources.session_factory, settings
    )
    with client.app.state.resources.session_factory() as session:
        inactive = session.query(AppSession).one()
        inactive.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        live = issue_app_session(
            MonzoTokenResponse(
                user_id="live-user", access_token="synthetic", expires_in=3600
            ),
            client.app.state.resources.session_factory,
            settings,
        )
    prune_history(client.app.state.resources.session_factory)
    with client.app.state.resources.session_factory() as session:
        assert session.query(AppSession).one().user_id == "live-user"
        assert session.query(UsedAppRefreshToken).count() == 0
    assert (
        rotate_app_refresh_token(
            live.refresh_token, client.app.state.resources.session_factory, settings
        )
        is not None
    )
