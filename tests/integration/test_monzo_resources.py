import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx

from app.db.models import MonzoCredential
from app.schemas.monzo import MonzoTokenResponse
from app.services.monzo_credentials import resolve_monzo_access_token
from app.services.token_crypto import decrypt_token, encrypt_token


def _session_token(settings, user_id="user_test123"):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "ver": 0, "iat": now, "exp": now + timedelta(hours=1)},
        settings.jwt_secret_key,
        algorithm="HS256",
    )


def _save_credential(client, *, expired=False):
    now = datetime.now(timezone.utc)
    with client.app.state.resources.session_factory() as session:
        session.add(
            MonzoCredential(
                user_id="user_test123",
                access_token=encrypt_token(
                    "test-access-token", client.app.state.resources.settings
                ),
                refresh_token=encrypt_token(
                    "test-refresh-token", client.app.state.resources.settings
                ),
                token_type="Bearer",
                expires_at=now + timedelta(hours=-1 if expired else 1),
                updated_at=now,
            )
        )
        session.commit()


@pytest.mark.parametrize(
    ("path", "upstream_url", "upstream_body", "expected_body", "expected_query"),
    [
        (
            "/balance?account_id=acc_123",
            "https://api.monzo.com/balance",
            {
                "balance": 5000,
                "total_balance": 6000,
                "currency": "GBP",
                "spend_today": 100,
            },
            {"balance": 5000, "total_balance": 6000, "currency": "GBP"},
            {"account_id": "acc_123"},
        ),
        (
            "/pots?current_account_id=acc_123",
            "https://api.monzo.com/pots",
            {
                "pots": [
                    {
                        "id": "pot_123",
                        "name": "Savings",
                        "style": "beach_ball",
                        "balance": 133700,
                        "currency": "GBP",
                        "created": "2017-11-09T12:30:53.695Z",
                        "updated": "2017-11-09T12:30:53.695Z",
                        "deleted": False,
                        "available_for_bills": True,
                        "cover_image_url": "https://public-images.monzo.com/pots/gallery_covers/tickets_v1.webp",
                        "type": "default",
                    }
                ]
            },
            {
                "pots": [
                    {
                        "id": "pot_123",
                        "name": "Savings",
                        "balance": 133700,
                        "currency": "GBP",
                        "deleted": False,
                        "cover_image_url": "https://public-images.monzo.com/pots/gallery_covers/tickets_v1.webp",
                        "type": "default",
                    }
                ]
            },
            {"current_account_id": "acc_123"},
        ),
    ],
)
def test_resource_routes_pass_through_monzo_responses(
    client,
    settings,
    path,
    upstream_url,
    upstream_body,
    expected_body,
    expected_query,
):
    _save_credential(client)
    with respx.mock(assert_all_called=True) as monzo_mock:
        upstream = monzo_mock.get(upstream_url).mock(
            return_value=httpx.Response(200, json=upstream_body)
        )
        response = client.get(
            path,
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 200
    assert response.json() == expected_body
    assert upstream.calls.last.request.headers["Authorization"] == (
        "Bearer test-access-token"
    )
    assert dict(upstream.calls.last.request.url.params) == expected_query


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"cover_image_url": None},
        {"cover_image_url": "", "type": "savings"},
    ],
)
def test_pots_preserves_optional_metadata(client, settings, metadata):
    _save_credential(client)
    pot = {
        "id": "pot_123",
        "name": "Savings",
        "balance": 0,
        "currency": "GBP",
        "deleted": False,
        "type": "default",
    }
    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.get("https://api.monzo.com/pots").mock(
            return_value=httpx.Response(200, json={"pots": [{**pot, **metadata}]})
        )
        response = client.get(
            "/pots?current_account_id=acc_123",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "pots": [
            {
                **pot,
                "cover_image_url": metadata.get("cover_image_url"),
                "type": metadata.get("type", "default"),
            }
        ]
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"type": None},
    ],
)
def test_pots_rejects_invalid_metadata(client, settings, metadata):
    _save_credential(client)
    pot = {
        "id": "pot_123",
        "name": "Savings",
        "balance": 0,
        "currency": "GBP",
        "deleted": False,
        **metadata,
    }
    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.get("https://api.monzo.com/pots").mock(
            return_value=httpx.Response(200, json={"pots": [pot]})
        )
        response = client.get(
            "/pots?current_account_id=acc_123",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Monzo returned an invalid response"}


def test_accounts_with_balances_include_details_for_each_account(client, settings):
    _save_credential(client)
    accounts_body = {
        "accounts": [
            {
                "id": "acc_123",
                "description": "Personal Account",
                "created": "2015-11-13T12:17:42Z",
            },
            {
                "id": "acc_456",
                "description": "Joint Account",
                "created": "2020-06-01T08:00:00Z",
            },
        ]
    }
    balances = {
        "acc_123": {
            "balance": 5000,
            "total_balance": 6000,
            "currency": "GBP",
            "spend_today": 100,
        },
        "acc_456": {
            "balance": 7000,
            "total_balance": 8000,
            "currency": "GBP",
            "spend_today": 200,
        },
    }

    with respx.mock(assert_all_called=True) as monzo_mock:
        accounts = monzo_mock.get(
            "https://api.monzo.com/accounts", params={"account_type": "uk_retail"}
        ).mock(return_value=httpx.Response(200, json=accounts_body))
        balance_routes = {
            account_id: monzo_mock.get(
                "https://api.monzo.com/balance", params={"account_id": account_id}
            ).mock(return_value=httpx.Response(200, json=balance))
            for account_id, balance in balances.items()
        }
        response = client.get(
            "/accounts-with-balances?account_type=uk_retail",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "accounts": [
            {
                "id": account["id"],
                "description": account["description"],
                "balance_details": {
                    "balance": balances[account["id"]]["balance"],
                    "total_balance": balances[account["id"]]["total_balance"],
                    "currency": balances[account["id"]]["currency"],
                },
            }
            for account in accounts_body["accounts"]
        ]
    }
    assert accounts.calls.last.request.headers["Authorization"] == (
        "Bearer test-access-token"
    )
    for route in balance_routes.values():
        assert route.calls.last.request.headers["Authorization"] == (
            "Bearer test-access-token"
        )


def test_accounts_endpoint_is_removed(client):
    response = client.get("/accounts")

    assert response.status_code == 404


def test_accounts_returns_sanitized_monzo_error_and_logs_code(client, settings, caplog):
    _save_credential(client)
    caplog.set_level(logging.WARNING)
    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.get("https://api.monzo.com/accounts").mock(
            return_value=httpx.Response(
                403,
                json={"code": "forbidden", "message": "User approval required"},
            )
        )
        response = client.get(
            "/accounts-with-balances",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Monzo request failed"}
    assert "monzo_request_failed operation=accounts upstream_status=403" in caplog.text
    assert "monzo_code='upstream_error'" in caplog.text
    assert "monzo_message='upstream_error'" in caplog.text


def test_accounts_sanitizes_unapproved_monzo_error(client, settings, caplog):
    _save_credential(client)
    caplog.set_level(logging.WARNING)
    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.get("https://api.monzo.com/accounts").mock(
            return_value=httpx.Response(
                403,
                json={
                    "code": "forbidden",
                    "message": "Access forbidden due to insufficient permissions.",
                },
            )
        )
        response = client.get(
            "/accounts-with-balances",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Monzo request failed"}
    assert "monzo_code='upstream_error'" in caplog.text
    assert "monzo_message='upstream_error'" in caplog.text


def test_accounts_with_balances_only_returns_fields_in_service_schema(client, settings):
    _save_credential(client)
    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.get("https://api.monzo.com/accounts").mock(
            return_value=httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "id": "acc_123",
                            "description": "Personal Account",
                            "created": "2015-11-13T12:17:42Z",
                            "new_monzo_field": "not exposed",
                        }
                    ],
                    "new_top_level_field": "not exposed",
                },
            )
        )
        monzo_mock.get(
            "https://api.monzo.com/balance", params={"account_id": "acc_123"}
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "balance": 5000,
                    "total_balance": 6000,
                    "currency": "GBP",
                    "spend_today": 100,
                    "new_balance_field": "not exposed",
                },
            )
        )
        response = client.get(
            "/accounts-with-balances",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "accounts": [
            {
                "id": "acc_123",
                "description": "Personal Account",
                "balance_details": {
                    "balance": 5000,
                    "total_balance": 6000,
                    "currency": "GBP",
                },
            }
        ]
    }


def test_accounts_with_balances_tolerates_all_balance_requests_failing(
    client, settings, caplog
):
    _save_credential(client)
    caplog.set_level(logging.WARNING)
    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.get("https://api.monzo.com/accounts").mock(
            return_value=httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "id": "acc_123",
                            "description": "Personal Account",
                            "created": "2015-11-13T12:17:42Z",
                        },
                        {
                            "id": "acc_456",
                            "description": "Joint Account",
                            "created": "2020-06-01T08:00:00Z",
                        },
                    ]
                },
            )
        )
        monzo_mock.get(
            "https://api.monzo.com/balance", params={"account_id": "acc_123"}
        ).mock(
            return_value=httpx.Response(
                403,
                json={"code": "forbidden", "message": "Balance unavailable"},
            )
        )
        monzo_mock.get(
            "https://api.monzo.com/balance", params={"account_id": "acc_456"}
        ).mock(side_effect=httpx.ConnectError("Monzo is offline"))
        response = client.get(
            "/accounts-with-balances",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "accounts": [
            {
                "id": "acc_123",
                "description": "Personal Account",
                "balance_details": None,
            },
            {
                "id": "acc_456",
                "description": "Joint Account",
                "balance_details": None,
            },
        ]
    }
    assert "monzo_request_failed operation=balance upstream_status=403" in caplog.text
    assert (
        "monzo_request_failed operation=balance reason=monzo_unreachable" in caplog.text
    )


def test_accounts_rejects_invalid_monzo_response(client, settings, caplog):
    _save_credential(client)
    caplog.set_level(logging.ERROR)
    with respx.mock(assert_all_called=True) as monzo_mock:
        monzo_mock.get("https://api.monzo.com/accounts").mock(
            return_value=httpx.Response(200, json={"accounts": [{"id": "acc_123"}]})
        )
        response = client.get(
            "/accounts-with-balances",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Monzo returned an invalid response"}
    assert "schema_errors=accounts.0.description:missing" in caplog.text


def test_expired_access_token_is_refreshed_and_saved(client, settings):
    _save_credential(client, expired=True)
    with respx.mock(assert_all_called=True) as monzo_mock:
        refresh = monzo_mock.post("https://api.monzo.com/oauth2/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-access-token",
                    "refresh_token": "new-refresh-token",
                    "token_type": "Bearer",
                    "expires_in": 21600,
                    "user_id": "user_test123",
                },
            )
        )
        accounts = monzo_mock.get("https://api.monzo.com/accounts").mock(
            return_value=httpx.Response(200, json={"accounts": []})
        )
        response = client.get(
            "/accounts-with-balances",
            headers={"Authorization": f"Bearer {_session_token(settings)}"},
        )

    assert response.status_code == 200
    assert accounts.calls.last.request.headers["Authorization"] == (
        "Bearer new-access-token"
    )
    assert parse_qs(refresh.calls.last.request.content.decode()) == {
        "grant_type": ["refresh_token"],
        "client_id": [settings.monzo_client_id],
        "client_secret": [settings.monzo_client_secret],
        "refresh_token": ["test-refresh-token"],
    }
    with client.app.state.resources.session_factory() as session:
        credential = session.get(MonzoCredential, "user_test123")
        assert decrypt_token(credential.access_token, settings) == "new-access-token"
        assert decrypt_token(credential.refresh_token, settings) == "new-refresh-token"


def test_concurrent_expired_token_requests_refresh_once(client, settings, monkeypatch):
    _save_credential(client, expired=True)
    calls = 0

    async def refresh(self, refresh_token, configured_settings):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return MonzoTokenResponse(
            user_id="user_test123",
            access_token="rotated-access-token",
            refresh_token="rotated-refresh-token",
            expires_in=3600,
        )

    monkeypatch.setattr("app.services.monzo.MonzoClient.refresh_access_token", refresh)

    async def resolve_twice():
        return await asyncio.gather(
            resolve_monzo_access_token(
                "user_test123", client.app.state.resources.session_factory, settings
            ),
            resolve_monzo_access_token(
                "user_test123", client.app.state.resources.session_factory, settings
            ),
        )

    first, second = asyncio.run(resolve_twice())

    assert first == second == "rotated-access-token"
    assert calls == 1


@pytest.mark.parametrize(
    ("headers", "expected_detail"),
    [
        ({}, "Bearer token required"),
        ({"Authorization": "Bearer invalid"}, "Invalid or expired bearer token"),
    ],
)
def test_accounts_with_balances_requires_valid_application_jwt(
    client, headers, expected_detail
):
    response = client.get("/accounts-with-balances", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == expected_detail


def test_accounts_with_balances_rejects_jwt_without_stored_credentials(
    client, settings
):
    response = client.get(
        "/accounts-with-balances",
        headers={"Authorization": f"Bearer {_session_token(settings)}"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Monzo connection is missing or expired"


def test_logout_revokes_existing_application_token(client, settings):
    _save_credential(client)
    token = _session_token(settings)
    headers = {"Authorization": f"Bearer {token}"}

    logout = client.post("/logout", headers=headers)
    after_logout = client.get("/scheduled-transfers", headers=headers)

    assert logout.status_code == 204
    assert after_logout.status_code == 401


def test_authentication_failure_is_traceable_without_logging_token(client, caplog):
    bearer_token = "sensitive-invalid-jwt"
    caplog.set_level(logging.WARNING)

    response = client.get(
        "/accounts-with-balances",
        headers={"Authorization": f"Bearer {bearer_token}"},
    )

    request_id = response.headers["x-request-id"]
    assert response.status_code == 401
    assert (
        "authentication_failed path=/accounts-with-balances "
        "reason=invalid_or_expired_jwt" in caplog.text
    )
    assert f"request_id={request_id}" in caplog.text
    assert "status_code=401" in caplog.text
    assert bearer_token not in caplog.text


def test_expired_access_token_is_logged_at_info_without_request_warning(
    client, settings, caplog
):
    caplog.set_level(logging.INFO)
    expired_token = jwt.encode(
        {
            "sub": "user_test123",
            "ver": 0,
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        settings.jwt_secret_key,
        algorithm="HS256",
    )

    response = client.get(
        "/accounts-with-balances",
        headers={"Authorization": f"Bearer {expired_token}"},
    )

    assert response.status_code == 401
    assert (
        "authentication_failed path=/accounts-with-balances "
        "reason=access_token_expired" in caplog.text
    )
    assert "request_completed_with_error" not in caplog.text
    record = next(
        record
        for record in caplog.records
        if "reason=access_token_expired" in record.getMessage()
    )
    assert record.levelno == logging.INFO
