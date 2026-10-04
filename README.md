# Schedzo

A Python 3.14+ FastAPI service for connecting to Monzo and scheduling recurring
savings-pot deposits and withdrawals. It uses SQLite, SQLAlchemy, Alembic, and
APScheduler. Run one application worker: the scheduler, user locks, request
limits, and refresh retry cache are process-local.

## Architecture

Routes handle HTTP concerns, application services coordinate workflows, and
`app/domain/` contains reusable commands, results, and recurrence rules. Shared
resources are typed and owned by the application lifespan. Routers receive resource
and OAuth services through dependency injection; provider clients stay inside
services and composition code. See
[the architecture guide](docs/architecture.md) for module responsibilities,
transaction boundaries, and HTTP client ownership.

## Local setup

Install [uv](https://docs.astral.sh/uv/), then install the locked dependencies:

```sh
uv sync --frozen
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your Monzo OAuth client credentials and registered redirect URI.
The local default is `http://127.0.0.1:8000/monzo-callback`; use that same host when
starting login so the browser sends the OAuth state cookie to the callback.
Generate separate signing and encryption keys:

```sh
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Save the first value as `JWT_SECRET_KEY` and the second as
`TOKEN_ENCRYPTION_KEY` in `.env`. Keep this file private and out of Git.
Apply migrations before starting the app:

```sh
uv run alembic upgrade head
uv run uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive API documentation.
`GET /health` returns `{"message":"ok"}`; it is a liveness endpoint, not a
check of the Monzo connection. ReDoc and the schema are available at `/redoc`
and `/openapi.json`.

## Configuration

Settings load from environment variables and the root `.env` file. Existing
environment variables take precedence.

| Variable | Purpose / default |
| --- | --- |
| `MONZO_CLIENT_ID` | Monzo OAuth client ID; required for login |
| `MONZO_CLIENT_SECRET` | Monzo OAuth client secret; required for token exchange |
| `MONZO_REDIRECT_URI` | Registered callback URI; defaults to `http://127.0.0.1:8000/monzo-callback` |
| `DATABASE_URL` | Defaults to `sqlite:///./monzo_scheduler.db` |
| `JWT_SECRET_KEY` | Signing secret of at least 32 bytes; required at startup |
| `TOKEN_ENCRYPTION_KEY` | Valid Fernet key; required at startup and for token migrations |
| `JWT_EXPIRATION_SECONDS` | Access-token lifetime, 1–3600 seconds; defaults to 900 |
| `APP_ENV` | `development` (default) or `production` |

The app does not create its schema at startup. Always run Alembic with the same
database URL and encryption key as the application. Losing the encryption key
makes stored Monzo credentials unreadable; replacing it requires re-encrypting
those credentials.

## Authentication and sessions

Start login in a browser at `GET /monzo-redirect`. Monzo redirects back to
`GET /monzo-callback`; the callback validates signed, ten-minute OAuth state
against an HttpOnly browser cookie and persists consumption to prevent reuse.
The callback returns JSON containing `token`, `expiresIn`, `refreshToken`, and
`refreshExpiresIn`.

Send the application access token as `Authorization: Bearer <token>` to the
protected endpoints below. If Monzo account access returns a 403 with detail code
`monzo_approval_required`, allow data access in the Monzo app.

When the access JWT expires, call `POST /auth/refresh` with
`{"refreshToken":"..."}`. This endpoint uses the refresh token in the body;
it does not require a bearer token. It returns the same fields as the callback,
including a rotated refresh token. Refresh tokens expire after 60 days without a
successful rotation (`refreshExpiresIn` is 5,184,000 seconds). Ordinary API calls
and duplicate retries do not extend that deadline. There is no absolute session
lifetime or rotation-count ceiling.

Serialize refresh requests per session and save replacement tokens atomically.
The immediately previous refresh token can return the same token pair within a
fixed five-second retry window, while that rotation remains the latest. Retries
do not extend the window. Reuse outside the window or of an older predecessor
revokes that session, including its latest JWT. The bounded retry cache is held
in memory and lost on restart; retrying a consumed token after restart requires
login again. Unknown tokens return 401 without revoking unrelated sessions.
A refresh quota 429 is temporary; retain the current token and respect
`Retry-After`.

Multiple devices or browsers can keep separate sessions. Login preserves
existing sessions. Logout and refresh-token reuse invalidate the affected
session; legacy JWTs without a session ID invalidate all sessions on logout.
Emergency stop and disconnect invalidate all sessions for the user.

## Protected endpoints

| Method and path | Behavior |
| --- | --- |
| `GET /accounts-with-balances` | Optional `account_type`; an account's `balance_details` is `null` if its balance request fails |
| `GET /balance?account_id=<account_id>` | Account balance |
| `GET /pots?current_account_id=<account_id>` | Savings pots |
| `GET /scheduled-transfers` | Paginated transfer occurrences and their setup status |
| `POST /schedule-transfer` | Create a recurring schedule |
| `DELETE /schedule-transfer/<setup_id>` | Deactivate the setup and cancel its pending occurrence; returns 204 |
| `POST /logout` | Revoke the current app session; preserves schedules and the Monzo connection; returns 204 |
| `POST /emergency-stop` | Pause scheduling, cancel pending transfers, revoke all app sessions, and disconnect Monzo |
| `POST /disconnect` | Same behavior as emergency stop |
| `POST /resume-transfers` | Clear the scheduling pause; does not reactivate cancelled setups; returns 204 |

Transfer history returns `items`, `total`, `limit`, and `offset`. The default
limit is 50 (maximum 100); offset defaults to 0. Filter by `account_id`, `pot_id`,
or comma-separated `status` values: `pending`, `running`, `completed`, `failed`,
and `cancelled`. The default status filter excludes cancelled transfers.

Emergency stop waits for an in-flight transfer before cancelling pending work.
Stop/disconnect return 204 after confirmed provider revocation, or 202 when
revocation remains pending. A 202 still means scheduling is paused, all app
sessions are revoked, and the stored connection is blocked. Revocation retries
every minute, including after restart. Login is blocked until revocation finishes.
Stored Monzo tokens are erased after confirmation. Reconnect through OAuth, then
explicitly resume scheduling before creating new schedules.

## Scheduling

Use a future UK local datetime with the correct `Europe/London` UTC offset,
aligned to a whole minute. Winter uses `+00:00` (or `Z`); summer uses `+01:00`.
Choose `daily`, `weekly`, or `monthly`, and `deposit` or `withdraw`:

```json
{
  "datetime": "2030-01-31T09:15:00Z",
  "interval": "monthly",
  "type": "deposit",
  "amount": 1250,
  "pot_id": "pot_123",
  "account_id": "acc_123"
}
```

Amounts are positive signed 64-bit integers in minor currency units: `1250`
means £12.50 for GBP. Account and pot IDs must contain only letters, digits,
underscores, or hyphens and be 1–255 characters long. Extra request fields are
rejected. Creation and resume require a valid app session, without a recent-login
requirement or application-level monetary caps.

Monthly schedules on days 29–31 use the last valid day of shorter months, then
return to the requested day when it exists. Each setup is active or deactivated;
its occurrences are pending, running, completed, failed, or cancelled. Recurrence
continues after completed or failed occurrences while the setup remains active.
On restart, pending and interrupted running occurrences from active setups are
restored, with overdue work scheduled immediately. After execution, the next
occurrence skips elapsed intervals rather than replaying every missed interval.
The service refreshes expired Monzo access tokens when possible.

## Limits, storage, and logging

The app applies these limits:

- 120 requests per client address per minute, plus five login starts per ten minutes.
- 64 simultaneous requests, 16 KiB request bodies, and ten seconds to receive a body.
  Capacity, size, and timeout failures return 503, 413, and 408 respectively.
- 50 active schedules per user and 100 schedule creations per rolling day;
  cancelling a setup does not remove it from the creation budget.
- Session issuance and refresh budgets of 20 app sessions per rolling day and
  60 refresh rotations per rolling hour, enforced using stored authentication records.

Monzo access and refresh tokens are encrypted with Fernet; app refresh tokens are
stored as hashes. Maintenance runs at startup and hourly, deleting expired app
sessions, sessions revoked for at least a day, their used-token hashes, and expired
OAuth state records. Used-token hashes for live sessions accumulate for reuse
detection. Schedule definitions and transfer history are retained indefinitely.
Protect the database, exports, and backups as financial data.

Application logs go to standard error and omit query strings and authentication
credentials. Requests reaching the request-logging middleware receive
`X-Request-ID`, which is also logged for failed requests; earlier transport/body
rejections can lack that header. Responses use `Cache-Control: no-store` and
`Pragma: no-cache`. Validation errors omit submitted values, context, and untrusted
field names. Configure proxy logs and monitoring to exclude OAuth query strings,
authentication bodies, cookies, and Authorization headers too.

Production requires an HTTPS redirect URI and rejects HTTP requests and invalid
production signing/encryption settings. API docs remain available. Configure the
trusted local proxy to replace forwarding headers and report the real client
address. See the [deployment guide](docs/deployment-security.md) and
[historical token exposure recovery guide](docs/token-exposure-recovery.md).

## Development

The entry point is `main.py`; app construction and lifespan resources live in
`app/main.py`. HTTP handlers are in `app/routers/`, business logic in
`app/services/`, schemas in `app/schemas/`, and database models in `app/db/`.
Schema migrations live in `migrations/versions/`.

Run the unit and integration suite and the Git-index sensitive-file check:

```sh
uv run pytest
uv run python scripts/check_sensitive_files.py
```

Integration tests mock Monzo with `respx` and run offline. The sensitive-file
check inspects staged/tracked content, not unstaged changes or Git history.
Declare dependency changes in `pyproject.toml` and update `uv.lock` with `uv lock`.

## Continuous integration and deployment

[CI](.github/workflows/ci.yaml) runs on every push and pull request, and can also
be started manually. On Ubuntu with Python 3.14 it checks that `uv.lock` matches
`pyproject.toml`, installs the locked dependencies, checks installed dependency
compatibility, and runs the full unit and integration suite and sensitive-file
check. A separate job installs production dependencies only, applies migrations
to a temporary SQLite database, and checks application startup and `/health` in
production mode. CI generates temporary keys and needs no Monzo or deployment
secrets. This service runs from source; it has no wheel or container build.

Require the `Tests` and `Production readiness` checks, along with the existing
`sensitive-files` check, in GitHub branch protection or a ruleset for `main` to
prevent merging failing changes. Workflows report failures; required checks
enforce the merge gate. When adding dependencies, commit both `pyproject.toml`
and the updated `uv.lock`.

The [deployment workflow](.github/workflows/deploy.yaml) reuses these CI jobs
before deploying a tested commit from `main` to DigitalOcean. Configure the
production environment and server prerequisites in the
[deployment guide](docs/deployment-security.md) before enabling deployment.

## License

Licensed under the [MIT License](LICENSE). Copyright (c) 2026 wchr-aun.
