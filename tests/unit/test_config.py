from app.config import Settings

SETTING_ENV_VARS = (
    "MONZO_CLIENT_ID",
    "MONZO_CLIENT_SECRET",
    "MONZO_REDIRECT_URI",
    "DATABASE_URL",
    "JWT_SECRET_KEY",
    "TOKEN_ENCRYPTION_KEY",
    "BFF_API_KEY",
    "JWT_EXPIRATION_SECONDS",
    "APP_ENV",
)


def clear_setting_environment(monkeypatch):
    for name in SETTING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_settings_use_local_callback_defaults(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "ENV_FILE", "/missing/test.env")
    clear_setting_environment(monkeypatch)

    assert Settings.from_environment() == Settings(
        monzo_client_id="",
        monzo_client_secret="",
        monzo_redirect_uri="http://127.0.0.1:8000/monzo-callback",
    )


def test_settings_read_environment(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "ENV_FILE", "/missing/test.env")
    clear_setting_environment(monkeypatch)
    monkeypatch.setenv("MONZO_CLIENT_ID", "client")
    monkeypatch.setenv("MONZO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MONZO_REDIRECT_URI", "https://example.test/callback")

    assert Settings.from_environment() == Settings(
        monzo_client_id="client",
        monzo_client_secret="secret",
        monzo_redirect_uri="https://example.test/callback",
    )


def test_settings_load_dotenv_file_with_environment_precedence(tmp_path, monkeypatch):
    from app import config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "MONZO_CLIENT_ID=file-client\n"
        "MONZO_CLIENT_SECRET=file-secret\n"
        "MONZO_REDIRECT_URI=http://localhost/callback\n"
        "JWT_EXPIRATION_SECONDS=1800\n"
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    clear_setting_environment(monkeypatch)
    monkeypatch.setenv("MONZO_CLIENT_ID", "environment-client")

    settings = Settings.from_environment()

    assert settings.monzo_client_id == "environment-client"
    assert settings.monzo_client_secret == "file-secret"
    assert settings.monzo_redirect_uri == "http://localhost/callback"
    assert settings.jwt_expiration_seconds == 1800
