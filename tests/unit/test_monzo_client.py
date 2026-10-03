import asyncio

import httpx
import pytest

from app.services.monzo import MonzoClient, monzo_client_scope
from app.services.resources import ResourceService


def test_resource_workflow_reuses_injected_transport_and_preserves_partial_results():
    authorizations = []

    def respond(request):
        authorizations.append(request.headers["Authorization"])
        if request.url.path == "/accounts":
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {"id": "acc_a", "description": "A", "private": "ignored"},
                        {"id": "acc_b", "description": "B"},
                    ]
                },
            )
        if request.url.params["account_id"] == "acc_b":
            return httpx.Response(503, json={"message": "private upstream data"})
        return httpx.Response(
            200, json={"balance": 100, "total_balance": 100, "currency": "GBP"}
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = MonzoClient(http)
            async with monzo_client_scope(client) as scoped:
                assert scoped is client
                result = await ResourceService(scoped).accounts_with_balances("first-token")
                assert result.accounts[0].balance_details.balance == 100
                assert result.accounts[1].balance_details is None
                assert "private" not in result.model_dump()["accounts"][0]
            assert not http.is_closed
            await client.get_balance("second-token", "acc_a")
        assert http.is_closed

    asyncio.run(run())
    assert authorizations == ["Bearer first-token"] * 3 + ["Bearer second-token"]


def test_owned_transport_closes_on_failure():
    async def run():
        with pytest.raises(RuntimeError, match="failed"):
            async with monzo_client_scope() as client:
                assert not client.http.is_closed
                raise RuntimeError("failed")
        assert client.http.is_closed

    asyncio.run(run())
