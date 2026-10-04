import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

from app.config import Settings
from app.db.models import Base
from app.main import create_app


@pytest.fixture
def settings():
    return Settings(
        monzo_client_id="test-client-id",
        monzo_client_secret="test-client-secret",
        monzo_redirect_uri="http://testserver/monzo-callback",
        bff_api_key="test-bff-api-key-that-is-long-enough-for-tests",
        jwt_secret_key="test-jwt-signing-secret-for-tests-only",
        token_encryption_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    )


@pytest.fixture
def client(settings):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    application = create_app(settings, engine=engine)
    with TestClient(
        application, headers={"X-BFF-API-Key": settings.bff_api_key}
    ) as test_client:
        yield test_client
    engine.dispose()


@pytest.fixture(autouse=True)
def mock_monzo_disconnection():
    import httpx
    import respx

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://api.monzo.com/oauth2/logout").mock(
            return_value=httpx.Response(200)
        )
        yield route
