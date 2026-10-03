from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import respx
from fastapi.testclient import TestClient

from app.db.models import MonzoCredential
from app.schemas.monzo import MonzoTokenResponse
from app.services.sessions import issue_app_session
from app.services.token_crypto import decrypt_token


def test_oauth_state_is_bound_to_the_browser_cookie(client):
    redirect = client.get("/monzo-redirect", follow_redirects=False)
    state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]

    other_browser = TestClient(client.app)
    callback = other_browser.get(
        "/monzo-callback", params={"code": "authorization-code", "state": state}
    )
    other_browser.close()

    assert callback.status_code == 400
    assert client.cookies.get("monzo_oauth_state") == state


def test_oauth_redirect_and_token_exchange_happy_path(client, settings):
    with respx.mock(assert_all_called=True) as monzo_mock:
        token_route = monzo_mock.post("https://api.monzo.com/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "test-access-token",
                    "refresh_token": "test-refresh-token",
                    "token_type": "Bearer",
                    "expires_in": 21600,
                    "user_id": "user_test123",
                },
            )
        )
        redirect = client.get("/monzo-redirect", follow_redirects=False)
        assert redirect.status_code == 302

        redirect_url = urlparse(redirect.headers["location"])
        redirect_params = parse_qs(redirect_url.query)
        assert redirect_url.scheme == "https"
        assert redirect_url.netloc == "auth.monzo.com"
        assert redirect_params["client_id"] == [settings.monzo_client_id]
        assert redirect_params["redirect_uri"] == [settings.monzo_redirect_uri]
        assert redirect_params["response_type"] == ["code"]
        state = redirect_params["state"][0]
        assert client.cookies.get("monzo_oauth_state") == state
        cookie = redirect.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Max-Age=600" in cookie

        callback = client.get(
            "/monzo-callback", params={"code": "authorization-code", "state": state}
        )

    assert callback.status_code == 200
    response_body = callback.json()
    assert response_body["expiresIn"] == 900
    assert callback.headers["cache-control"] == "no-store"
    assert callback.headers["pragma"] == "no-cache"
    assert callback.headers["referrer-policy"] == "no-referrer"
    assert 'monzo_oauth_state="";' in callback.headers["set-cookie"]
    from app.services.oauth_state import consume_oauth_state

    assert not consume_oauth_state(
        state, state, settings, client.app.state.resources.session_factory
    )
    jwt_claims = jwt.decode(
        response_body["token"], settings.jwt_secret_key, algorithms=["HS256"]
    )
    assert jwt_claims["sub"] == "user_test123"
    assert "access_token" not in callback.text

    with client.app.state.resources.session_factory() as session:
        credential = session.get(MonzoCredential, "user_test123")
        assert credential is not None
        assert decrypt_token(credential.access_token, settings) == "test-access-token"
        assert decrypt_token(credential.refresh_token, settings) == "test-refresh-token"

    assert token_route.called
    form = parse_qs(token_route.calls.last.request.content.decode())
    assert form == {
        "grant_type": ["authorization_code"],
        "client_id": [settings.monzo_client_id],
        "client_secret": [settings.monzo_client_secret],
        "redirect_uri": [settings.monzo_redirect_uri],
        "code": ["authorization-code"],
    }


def test_concurrent_app_refresh_requests_share_one_rotation(client, settings):
    token_pair = issue_app_session(
        MonzoTokenResponse(
            user_id="user_test123",
            access_token="test-access-token",
            refresh_token="test-monzo-refresh-token",
            expires_in=21600,
        ),
        client.app.state.resources.session_factory,
        settings,
    )

    def refresh():
        return client.post(
            "/auth/refresh",
            json={"refresh_token": token_pair.refresh_token},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _: refresh(), range(8)))

    assert all(response.status_code == 200 for response in responses)
    successful = responses[0].json()
    assert all(response.json() == successful for response in responses)
    from app.db.models import AppSession, UsedAppRefreshToken

    with client.app.state.resources.session_factory() as session:
        assert session.query(UsedAppRefreshToken).count() == 1
        assert session.query(AppSession).one().revoked_at is None
    assert (
        client.post(
            "/auth/refresh", json={"refreshToken": successful["refreshToken"]}
        ).status_code
        == 200
    )


def test_older_refresh_reuse_revokes_after_multiple_rotations(client, settings):
    import pytest

    from app.services.authorization import SessionAuthenticationError, decode_user_id
    from app.services.sessions import rotate_app_refresh_token

    original = issue_app_session(
        MonzoTokenResponse(
            user_id="user_reuse", access_token="synthetic", expires_in=3600
        ),
        client.app.state.resources.session_factory,
        settings,
    )
    second = rotate_app_refresh_token(
        original.refresh_token, client.app.state.resources.session_factory, settings
    )
    third = rotate_app_refresh_token(
        second.refresh_token, client.app.state.resources.session_factory, settings
    )
    assert (
        rotate_app_refresh_token(
            "x" * 64, client.app.state.resources.session_factory, settings
        )
        is None
    )
    assert (
        decode_user_id(
            third.access_token, settings, client.app.state.resources.session_factory
        )
        == "user_reuse"
    )
    assert (
        rotate_app_refresh_token(
            original.refresh_token, client.app.state.resources.session_factory, settings
        )
        is None
    )
    assert (
        rotate_app_refresh_token(
            third.refresh_token, client.app.state.resources.session_factory, settings
        )
        is None
    )
    with pytest.raises(SessionAuthenticationError):
        decode_user_id(
            third.access_token, settings, client.app.state.resources.session_factory
        )


def test_active_refresh_extends_inactivity_expiry_without_absolute_lifetime(
    client, settings
):
    from datetime import datetime, timedelta, timezone

    from app.db.models import AppSession, UsedAppRefreshToken
    from app.services.authorization import decode_user_id
    from app.services.maintenance import prune_history
    from app.services.sessions import rotate_app_refresh_token
    from app.services.token_crypto import hash_refresh_token

    with client.app.state.resources.session_factory() as session:
        pair = issue_app_session(
            MonzoTokenResponse(
                user_id="expiry-user", access_token="synthetic", expires_in=3600
            ),
            client.app.state.resources.session_factory,
            settings,
        )
        row = session.query(AppSession).filter_by(user_id="expiry-user").one()
        old = datetime.now(timezone.utc) - timedelta(days=3650)
        row.created_at = old
        row.updated_at = datetime.now(timezone.utc) - timedelta(days=59)
        row.expires_at = row.updated_at + timedelta(days=60)
        session.add_all(
            [
                UsedAppRefreshToken(
                    token_hash=hash_refresh_token(f"used-{i}"),
                    session_id=row.session_id,
                    used_at=old,
                )
                for i in range(4096)
            ]
        )
        session.commit()
    assert pair.refresh_expires_in == 60 * 86400
    prune_history(client.app.state.resources.session_factory)
    response = client.post("/auth/refresh", json={"refreshToken": pair.refresh_token})
    assert response.status_code == 200
    assert response.json()["refreshExpiresIn"] == 60 * 86400
    with client.app.state.resources.session_factory() as session:
        row = session.query(AppSession).filter_by(user_id="expiry-user").one()
        assert row.expires_at == row.updated_at + timedelta(days=60)
        assert row.expires_at > datetime.now(timezone.utc).replace(
            tzinfo=None
        ) + timedelta(days=59)
        refreshed_deadline = row.expires_at
    assert response.json()["expiresIn"] == settings.jwt_expiration_seconds
    assert (
        decode_user_id(
            response.json()["token"],
            settings,
            client.app.state.resources.session_factory,
        )
        == "expiry-user"
    )
    duplicate = rotate_app_refresh_token(
        pair.refresh_token, client.app.state.resources.session_factory, settings
    )
    assert duplicate.refresh_token == response.json()["refreshToken"]
    assert duplicate.access_token == response.json()["token"]
    with client.app.state.resources.session_factory() as session:
        assert (
            session.query(AppSession).filter_by(user_id="expiry-user").one().expires_at
            == refreshed_deadline
        )
    assert (
        client.post(
            "/auth/refresh", json={"refreshToken": duplicate.refresh_token}
        ).status_code
        == 200
    )


def test_expired_refresh_session_cannot_refresh_or_use_access_token(client, settings):
    from datetime import datetime, timedelta, timezone

    from app.db.models import AppSession
    from tests.integration.test_security_races import login

    pair = login(client, settings)
    rotated = client.post(
        "/auth/refresh", json={"refreshToken": pair.refresh_token}
    ).json()
    with client.app.state.resources.session_factory() as session:
        row = session.query(AppSession).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    # Even a cached retry result must not bypass session expiry.
    assert (
        client.post(
            "/auth/refresh", json={"refreshToken": pair.refresh_token}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/refresh", json={"refreshToken": rotated["refreshToken"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/logout", headers={"Authorization": f"Bearer {rotated['token']}"}
        ).status_code
        == 401
    )
