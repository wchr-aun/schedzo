import pytest

from app.db.models import AppSession, MonzoCredential
from app.domain.monzo import MonzoTokenResponse
from app.services.sessions import issue_app_session
from app.services.token_crypto import decrypt_token


@pytest.mark.parametrize("existing", [False, True])
def test_failed_session_signing_rolls_back_credentials_and_session(
    client, settings, monkeypatch, existing
):
    factory = client.app.state.resources.session_factory
    if existing:
        issue_app_session(
            MonzoTokenResponse(
                user_id="user", access_token="original", expires_in=3600
            ),
            factory,
            settings,
        )

    def fail_signing(*args):
        raise RuntimeError("signing unavailable")

    monkeypatch.setattr("app.services.sessions.encode_access_token", fail_signing)
    with pytest.raises(RuntimeError, match="signing unavailable"):
        issue_app_session(
            MonzoTokenResponse(
                user_id="user", access_token="replacement", expires_in=3600
            ),
            factory,
            settings,
        )
    with factory() as session:
        assert session.query(AppSession).count() == int(existing)
        credential = session.get(MonzoCredential, "user")
        if existing:
            assert decrypt_token(credential.access_token, settings) == "original"
        else:
            assert credential is None
