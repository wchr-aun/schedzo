# Project guide

## Overview

This is a Python 3.14+ FastAPI service for scheduling Monzo savings-pot deposits and withdrawals.

## Structure

- `main.py` is the Uvicorn entry point and re-exports `app` and `create_app`.
- `app/main.py` composes the FastAPI app and registers routers.
- `app/lifespan.py`, `app/runtime.py`, and `app/background_jobs.py` own typed shared resources and background-job setup.
- `app/http.py` configures HTTP middleware and sanitized error handlers.
- `app/dependencies.py` supplies focused services, configuration, and authentication contexts to routers.
- `app/domain/` contains commands, results, errors, and recurrence/time rules independent of HTTP and persistence.
- `app/config.py` reads configuration from environment variables.
- `app/db/` defines SQLAlchemy models and database engine/session setup.
- `app/routers/` contains HTTP request handling. Keep API concerns here.
- `app/schemas/` defines request and response validation models.
- `app/services/` contains scheduling, transfer execution, notifications, resource and OAuth services, application sessions, provider credentials, and the injectable Monzo client.
- `docs/architecture.md` describes module and transaction boundaries and HTTP client ownership.
- `migrations/versions/` contains Alembic schema migrations.
- `deploy/` contains nginx and application systemd templates; deployment prerequisites are documented in `docs/deployment-security.md`.
- `scripts/` contains the Git-index sensitive-file check and the isolated history-cleanup preparation script.

Keep route handlers small. Put reusable business logic in services and pure rules in the domain. Keep provider clients out of the router layer: routers must not import, receive, construct, or fetch `MonzoClient` or HTTP transport clients. Inject `ResourceService` and `OAuthService` through focused dependency providers instead. Providers construct services with the lifespan-owned client; services never access `app.state` or create a per-request transport. Do not inject the full `ApplicationResources` container into routers. Shared resources remain typed in `app.state.resources`. Services must not depend on routers, FastAPI request objects, or HTTP request schemas.

## Configuration and security

- Read secrets from environment variables; never hard-code or commit them.
- Keep the registered Monzo redirect URI aligned with `MONZO_REDIRECT_URI`.
- Preserve OAuth `state` validation on the callback.
- Do not log access tokens, client secrets, or authorization codes.
- Monzo access and refresh tokens are encrypted with Fernet in SQLite; application refresh tokens are stored only as hashes. Avoid exposing token values in logs or responses and protect the database file and backups.
- `JWT_SECRET_KEY` signs the application token returned by the OAuth callback.
- Startup requires a valid `TOKEN_ENCRYPTION_KEY` and a `JWT_SECRET_KEY` of at least 32 bytes. Keep these keys distinct and preserve the encryption key across deployments and migrations.
- Apply schema changes with `uv run alembic upgrade head`; do not use `metadata.create_all()` in application startup.
- The scheduler, user locks, request limits, and refresh retry cache are process-local, so run one worker. OAuth state is signed, bound to the browser cookie, and its consumption is persisted.
- Application refresh tokens rotate with a fixed five-second retry window and expire after 60 days without a successful rotation. Preserve session-specific logout and reuse detection, and invalidate all sessions on emergency stop/disconnect.
- Emergency stop/disconnect persist a scheduling pause and retry provider revocation until confirmed. Reconnection requires explicit resume; cancelled setups stay deactivated.
- Production requires `APP_ENV=production`, an HTTPS redirect URI, and a trusted proxy that replaces forwarding headers. API docs remain available.

## Development

Install locked dependencies with `uv sync --frozen`, copy `.env.example` to a private `.env`, replace the placeholders, and apply migrations before running `uv run uvicorn main:app --reload`. See `README.md` for key generation and setup. Declare dependencies in `pyproject.toml`, update the lockfile with `uv lock`, and commit the corresponding `uv.lock` updates.

The liveness endpoint is `GET /health`. Interactive docs are at `/docs` and `/redoc`, with the schema at `/openapi.json`. Configure `DATABASE_URL`, `JWT_SECRET_KEY`, and `TOKEN_ENCRYPTION_KEY` before startup, and Monzo credentials before enabling OAuth.

## Tests

After every code, schema, migration, or test change, run the full unit and integration suite with `uv run pytest` and report whether it passes. If the suite cannot run, state why and do not claim the changes are verified. Unit tests cover local logic and error branches; integration tests use `respx` to mock Monzo's HTTP responses while exercising the app's HTTP flow. Keep integration tests deterministic and offline. The architecture test guards against provider clients and the full resource container leaking into routers.

Run `uv run python scripts/check_sensitive_files.py` before committing. It checks the Git index for credential files, SQLite databases/sidecars, and private-key content; it does not scan unstaged edits or Git history. Never commit database files or real credentials.

## License

This project uses the MIT license in `LICENSE`. Keep README license information aligned with that file.
