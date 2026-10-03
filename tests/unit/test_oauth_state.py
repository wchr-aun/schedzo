from app.services.oauth_state import consume_oauth_state, create_oauth_state


def test_signed_state_is_bound_expiring_and_single_use(client, settings):
    state = create_oauth_state(settings, now=1000)
    factory = client.app.state.resources.session_factory
    assert not consume_oauth_state(
        state, "different-browser", settings, factory, now=1001
    )
    assert not consume_oauth_state(
        state + "x", state + "x", settings, factory, now=1001
    )
    assert not consume_oauth_state(state, state, settings, factory, now=999)
    assert not consume_oauth_state(state, state, settings, factory, now=1600)
    assert consume_oauth_state(state, state, settings, factory, now=1001)
    assert not consume_oauth_state(state, state, settings, factory, now=1002)


def test_login_starts_have_a_separate_limit(client):
    for _ in range(5):
        assert client.get("/monzo-redirect", follow_redirects=False).status_code == 302
    assert client.get("/monzo-redirect", follow_redirects=False).status_code == 429
    assert client.get("/health").status_code == 200


def test_other_clients_cannot_fill_a_global_pending_pool(client, settings):
    from sqlalchemy import func, select

    from app.db.models import ConsumedOAuthState

    for _ in range(1100):
        create_oauth_state(settings)
    with client.app.state.resources.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ConsumedOAuthState)) == 0
    assert client.get("/monzo-redirect", follow_redirects=False).status_code == 302


def test_non_ascii_states_are_rejected_without_server_errors(client, settings):
    factory = client.app.state.resources.session_factory
    assert not consume_oauth_state(
        "1000.nonce.\u2603", "1000.nonce.\u2603", settings, factory, now=1001
    )
    assert not consume_oauth_state("\u2603", "\u2603", settings, factory, now=1001)
