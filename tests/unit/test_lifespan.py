from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.main import create_app
from app.services.monzo import monzo_client_scope


@pytest.mark.parametrize("owned_engine", [True, False])
@pytest.mark.parametrize("failure_point", ["jobs", "start"])
def test_failed_startup_releases_owned_resources(
    settings, monkeypatch, owned_engine, failure_point
):
    engine = Mock(spec=Engine)
    scheduler = Mock()
    scheduler.running = False
    monkeypatch.setattr("app.lifespan.create_database_engine", lambda _: engine)
    monkeypatch.setattr("app.lifespan.create_session_factory", lambda _: Mock())
    monkeypatch.setattr("app.lifespan.BackgroundScheduler", lambda **_: scheduler)
    clients = []

    @asynccontextmanager
    async def tracked_client_scope():
        async with monzo_client_scope() as client:
            clients.append(client)
            yield client

    monkeypatch.setattr("app.lifespan.monzo_client_scope", tracked_client_scope)

    def register(_):
        if failure_point == "jobs":
            raise RuntimeError("startup failed")

    monkeypatch.setattr("app.lifespan.register_background_jobs", register)
    if failure_point == "start":
        scheduler.start.side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        with TestClient(create_app(settings, engine=None if owned_engine else engine)):
            pass
    assert len(clients) == 1
    assert clients[0].http.is_closed
    assert engine.dispose.call_count == int(owned_engine)
    scheduler.shutdown.assert_not_called()
