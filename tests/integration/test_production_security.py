import secrets
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app


def production(settings):
    return replace(
        settings,
        environment="production",
        jwt_secret_key=secrets.token_urlsafe(48),
        token_encryption_key=Fernet.generate_key().decode(),
        monzo_redirect_uri="https://testserver/monzo-callback",
    )


def test_production_rejects_http_and_serves_api_docs(client, settings):
    application = create_app(
        production(settings), engine=client.app.state.resources.database_engine
    )
    with TestClient(application) as secure_client:
        assert secure_client.get("/health").status_code == 400
        response = secure_client.get("https://testserver/health")
        assert response.status_code == 200
        assert response.headers["strict-transport-security"]
        assert response.headers["content-security-policy"]
        bad_host = secure_client.get("https://attacker.example/health")
        assert bad_host.status_code == 200
        assert bad_host.headers["cache-control"] == "no-store"
        assert secure_client.get("https://testserver/docs").status_code == 200
        assert secure_client.get("https://testserver/redoc").status_code == 200
        assert (
            "content-security-policy"
            not in secure_client.get("https://testserver/docs").headers
        )
        assert (
            "content-security-policy"
            not in secure_client.get("https://testserver/redoc").headers
        )
        assert secure_client.get("https://testserver/openapi.json").status_code == 200


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"monzo_redirect_uri": "http://testserver/monzo-callback"}, "HTTPS"),
        ({"jwt_secret_key": "replace-with-a-long-random-secret"}, "placeholders"),
    ],
)
def test_production_fails_closed_for_insecure_configuration(
    client, settings, changes, message
):
    with pytest.raises(RuntimeError, match=message):
        with TestClient(
            create_app(
                replace(production(settings), **changes),
                engine=client.app.state.resources.database_engine,
            )
        ):
            pass


def test_production_oauth_and_refresh_do_not_require_bff_headers(client, settings):
    application = create_app(
        production(settings), engine=client.app.state.resources.database_engine
    )
    with TestClient(application, base_url="https://testserver") as secure_client:
        redirect = secure_client.get("/monzo-redirect", follow_redirects=False)
        assert redirect.status_code == 302
        assert "Secure" in redirect.headers["set-cookie"]
        response = secure_client.post("/auth/refresh", json={"refreshToken": "x" * 64})
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or expired refresh token"


def test_bff_address_header_cannot_override_oauth_rate_limit(client):
    for index in range(5):
        assert (
            client.get(
                "/monzo-redirect",
                follow_redirects=False,
                headers={"X-BFF-Client-IP": f"192.0.2.{index}"},
            ).status_code
            == 302
        )
    assert (
        client.get(
            "/monzo-redirect",
            follow_redirects=False,
            headers={"X-BFF-Client-IP": "192.0.2.99"},
        ).status_code
        == 429
    )
