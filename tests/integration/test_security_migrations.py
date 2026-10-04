import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from alembic import command
from alembic.config import Config

from app.db.models import AppSession, MonzoCredential, UsedAppRefreshToken
from app.db.session import create_database_engine, create_session_factory
from app.services.token_crypto import decrypt_token


def test_existing_plaintext_and_refresh_sessions_upgrade_safely(
    tmp_path, monkeypatch, settings
):
    path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", settings.token_encryption_key)
    monkeypatch.setattr("app.config.ENV_FILE", tmp_path / "missing.env")
    config = Config("alembic.ini")
    command.upgrade(config, "0004_add_session_version")
    marker = "SYNTHETIC_LEGACY_ACCESS_TOKEN_FOR_MIGRATION"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO monzo_credentials (user_id, access_token, refresh_token, token_type, expires_at, updated_at, session_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-user",
                marker,
                "SYNTHETIC_LEGACY_REFRESH",
                "Bearer",
                (now + timedelta(hours=1)).isoformat(),
                now.isoformat(),
                1,
            ),
        )
    assert marker.encode() in path.read_bytes()
    command.upgrade(config, "0007_create_app_sessions")
    old_hash = sha256(b"synthetic-old-refresh").hexdigest()
    current_hash = sha256(b"synthetic-current-refresh").hexdigest()
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO app_sessions (session_id, user_id, session_version, refresh_token_hash, previous_refresh_token_hash, expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-session",
                "legacy-user",
                1,
                current_hash,
                old_hash,
                (now + timedelta(days=60)).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
    command.upgrade(config, "0014_scrub_sqlite_remnants")
    revoked_hash = sha256(b"synthetic-revoked-refresh").hexdigest()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE app_sessions SET expires_at = ?, absolute_expires_at = ? WHERE session_id = ?",
            (
                (now - timedelta(days=1)).isoformat(),
                (now - timedelta(days=1)).isoformat(),
                "legacy-session",
            ),
        )
        db.execute(
            "INSERT INTO app_sessions (session_id, user_id, session_version, refresh_token_hash, expires_at, absolute_expires_at, revoked_at, created_at, updated_at) SELECT ?, user_id, session_version, ?, expires_at, absolute_expires_at, ?, created_at, updated_at FROM app_sessions WHERE session_id = ?",
            ("revoked-session", revoked_hash, now.isoformat(), "legacy-session"),
        )
        db.execute(
            "INSERT INTO app_sessions (session_id, user_id, session_version, refresh_token_hash, expires_at, absolute_expires_at, created_at, updated_at) SELECT ?, user_id, session_version, ?, expires_at, absolute_expires_at, created_at, ? FROM app_sessions WHERE session_id = ?",
            (
                "inactive-session",
                sha256(b"synthetic-inactive-refresh").hexdigest(),
                (now - timedelta(days=61)).isoformat(),
                "legacy-session",
            ),
        )
    command.upgrade(config, "head")
    assert marker.encode() not in path.read_bytes()
    engine = create_database_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA secure_delete").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    with create_session_factory(engine)() as session:
        credential = session.get(MonzoCredential, "legacy-user")
        assert decrypt_token(credential.access_token, settings) == marker
        assert not credential.scheduling_paused and not credential.disconnected
        app_session = session.get(AppSession, "legacy-session")
        assert app_session.refresh_token_hash == current_hash
        assert app_session.revoked_at is None
        assert not hasattr(app_session, "absolute_expires_at")
        assert app_session.expires_at == now + timedelta(days=60)
        assert session.get(UsedAppRefreshToken, old_hash).session_id == "legacy-session"
    from app.services.sessions import rotate_app_refresh_token

    factory = create_session_factory(engine)
    with factory() as session:
        assert session.get(
            AppSession, "inactive-session"
        ).expires_at == now - timedelta(days=1)
    assert (
        rotate_app_refresh_token("synthetic-inactive-refresh", factory, settings)
        is None
    )
    assert (
        rotate_app_refresh_token("synthetic-revoked-refresh", factory, settings) is None
    )
    rotated = rotate_app_refresh_token("synthetic-current-refresh", factory, settings)
    assert rotated is not None and rotated.refresh_expires_in == 60 * 86400
    assert rotate_app_refresh_token("synthetic-old-refresh", factory, settings) is None
    # Reuse must still revoke the current session after migration.
    assert (
        rotate_app_refresh_token("synthetic-current-refresh", factory, settings) is None
    )
    engine.dispose()


def test_first_login_and_schedule_work_with_hardened_foreign_keys(tmp_path, settings):
    from fastapi.testclient import TestClient

    from app.db.models import Base
    from app.main import create_app
    from app.schemas.monzo import MonzoTokenResponse
    from app.services.sessions import issue_app_session
    from tests.integration.test_security_races import BODY

    engine = create_database_engine(f"sqlite:///{tmp_path / 'hardened.db'}")
    Base.metadata.create_all(engine)
    with TestClient(
        create_app(settings, engine=engine),
        headers={"X-BFF-API-Key": settings.bff_api_key},
    ) as client:
        pair = issue_app_session(
            MonzoTokenResponse(
                user_id="new-user", access_token="synthetic-token", expires_in=3600
            ),
            client.app.state.resources.session_factory,
            settings,
        )
        headers = {"Authorization": f"Bearer {pair.access_token}"}
        assert (
            client.post("/schedule-transfer", headers=headers, json=BODY).status_code
            == 200
        )
        assert (
            client.post(
                "/auth/refresh", json={"refreshToken": pair.refresh_token}
            ).status_code
            == 200
        )
        assert client.post("/emergency-stop", headers=headers).status_code == 204
    engine.dispose()
