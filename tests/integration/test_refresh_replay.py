import pytest

from app.db.models import AppSession, UsedAppRefreshToken
from app.db.session import create_session_factory
from app.services.sessions import rotate_app_refresh_token
from tests.integration.test_security_races import login


@pytest.fixture
def replay_clock(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.services.refresh_replay.monotonic", lambda: clock[0])
    return clock


def test_retries_do_not_extend_window_and_expired_reuse_revokes(
    client, settings, replay_clock
):
    pair = login(client, settings)
    body = {"refreshToken": pair.refresh_token}
    first = client.post("/auth/refresh", json=body)
    assert first.status_code == 200
    replay_clock[0] += 4
    retry = client.post("/auth/refresh", json=body)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    replay_clock[0] += 1
    assert client.post("/auth/refresh", json=body).status_code == 401
    assert (
        client.post(
            "/auth/refresh", json={"refreshToken": first.json()["refreshToken"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/logout", headers={"Authorization": f"Bearer {first.json()['token']}"}
        ).status_code
        == 401
    )


def test_duplicates_do_not_use_rotation_quota(client, settings, monkeypatch):
    pair = login(client, settings)
    monkeypatch.setattr("app.services.sessions.MAX_REFRESHES_PER_USER_PER_HOUR", 1)
    first = client.post("/auth/refresh", json={"refreshToken": pair.refresh_token})
    assert first.status_code == 200
    retry = client.post("/auth/refresh", json={"refreshToken": pair.refresh_token})
    assert retry.status_code == 200
    assert retry.json() == first.json()
    with client.app.state.resources.session_factory() as session:
        assert session.query(UsedAppRefreshToken).count() == 1
        assert session.query(AppSession).one().revoked_at is None
    assert (
        client.post(
            "/auth/refresh", json={"refreshToken": first.json()["refreshToken"]}
        ).status_code
        == 429
    )


@pytest.mark.parametrize("operation", ["/logout", "/emergency-stop"])
def test_replay_cache_cannot_bypass_revocation(client, settings, operation):
    pair = login(client, settings)
    response = client.post("/auth/refresh", json={"refreshToken": pair.refresh_token})
    assert response.status_code == 200
    assert (
        client.post(
            operation,
            headers={"Authorization": f"Bearer {response.json()['token']}"},
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/auth/refresh", json={"refreshToken": pair.refresh_token}
        ).status_code
        == 401
    )


def test_new_factory_does_not_replay_another_app_cache(client, settings):
    pair = login(client, settings)
    assert (
        rotate_app_refresh_token(
            pair.refresh_token, client.app.state.resources.session_factory, settings
        )
        is not None
    )
    restarted_factory = create_session_factory(
        client.app.state.resources.database_engine
    )
    assert (
        rotate_app_refresh_token(pair.refresh_token, restarted_factory, settings)
        is None
    )
