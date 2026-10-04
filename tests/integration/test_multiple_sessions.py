from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.integration.test_security_races import login


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_two_browsers_can_login_refresh_and_logout_independently(client):
    with TestClient(
        client.app,
        headers={"X-BFF-API-Key": client.app.state.resources.settings.bff_api_key},
    ) as other_browser, respx.mock() as monzo_mock:
        monzo_mock.post("https://api.monzo.com/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "user_id": "multi-device-user",
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                    "expires_in": 3600,
                },
            )
        )
        pairs = []
        for browser in (client, other_browser):
            redirect = browser.get("/monzo-redirect", follow_redirects=False)
            state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]
            response = browser.get(
                "/monzo-callback", params={"code": "synthetic-code", "state": state}
            )
            assert response.status_code == 200
            pairs.append(response.json())

        assert pairs[0]["token"] != pairs[1]["token"]
        assert pairs[0]["refreshToken"] != pairs[1]["refreshToken"]
        for browser, pair in zip((client, other_browser), pairs):
            assert browser.get(
                "/scheduled-transfers", headers=headers(pair["token"])
            ).status_code == 200
            response = browser.post(
                "/auth/refresh", json={"refreshToken": pair["refreshToken"]}
            )
            assert response.status_code == 200
            pair.update(response.json())

        assert client.post("/logout", headers=headers(pairs[0]["token"])).status_code == 204
        assert client.get(
            "/scheduled-transfers", headers=headers(pairs[0]["token"])
        ).status_code == 401
        assert client.post(
            "/auth/refresh", json={"refreshToken": pairs[0]["refreshToken"]}
        ).status_code == 401
        assert other_browser.get(
            "/scheduled-transfers", headers=headers(pairs[1]["token"])
        ).status_code == 200
        assert other_browser.post(
            "/auth/refresh", json={"refreshToken": pairs[1]["refreshToken"]}
        ).status_code == 200


@pytest.mark.parametrize("operation", ["/emergency-stop", "/disconnect"])
def test_global_revocation_invalidates_all_sessions_even_after_new_login(
    client, settings, operation
):
    pairs = [login(client, settings), login(client, settings)]
    assert client.post(operation, headers=headers(pairs[0].access_token)).status_code == 204
    fresh = login(client, settings)
    assert client.get(
        "/scheduled-transfers", headers=headers(fresh.access_token)
    ).status_code == 200
    for pair in pairs:
        assert client.get(
            "/scheduled-transfers", headers=headers(pair.access_token)
        ).status_code == 401
        assert client.post(
            "/auth/refresh", json={"refreshToken": pair.refresh_token}
        ).status_code == 401


def test_new_login_preserves_refresh_retries_and_reuse_revokes_only_one_session(
    client, settings, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr("app.services.refresh_replay.monotonic", lambda: clock[0])
    first = login(client, settings)
    body = {"refreshToken": first.refresh_token}
    rotated = client.post("/auth/refresh", json=body)
    assert rotated.status_code == 200
    second = login(client, settings)
    retry = client.post("/auth/refresh", json=body)
    assert retry.status_code == 200
    assert retry.json() == rotated.json()
    clock[0] += 5
    assert client.post("/auth/refresh", json=body).status_code == 401
    assert client.get(
        "/scheduled-transfers", headers=headers(rotated.json()["token"])
    ).status_code == 401
    assert client.post(
        "/auth/refresh", json={"refreshToken": rotated.json()["refreshToken"]}
    ).status_code == 401
    assert client.get(
        "/scheduled-transfers", headers=headers(second.access_token)
    ).status_code == 200
    assert client.post(
        "/auth/refresh", json={"refreshToken": second.refresh_token}
    ).status_code == 200
