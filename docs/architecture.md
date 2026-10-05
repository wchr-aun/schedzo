# Architecture

Schedzo is a single-worker FastAPI application. HTTP requests and background jobs
call application services using explicit arguments. Services do not import routers,
FastAPI request objects, or HTTP request schemas. Routers must not import, receive,
construct, or fetch provider clients or their HTTP transports. They receive focused
application services instead of the full resource container.

## Boundaries

| Location | Responsibility |
| --- | --- |
| `app/main.py` | Compose the application and register routers |
| `app/config.py` | Load configuration and validate startup requirements |
| `app/runtime.py` | Define the typed resources shared by one application lifespan |
| `app/lifespan.py` | Construct resources and release owned resources, including failed startup |
| `app/background_jobs.py` | Register maintenance jobs and restore persisted occurrences |
| `app/http.py` | HTTP error sanitization, diagnostics, and security middleware |
| `app/dependencies.py` | Adapt shared resources and authentication to FastAPI dependencies |
| `app/routers/` | Parse requests, call services, and map results/errors to HTTP |
| `app/schemas/` | Define HTTP input/output contracts and expose shared resource DTOs |
| `app/domain/` | Define commands, results, errors, and pure recurrence/time rules |
| `app/services/` | Coordinate application workflows and external operations |
| `app/db/` | Define persistence models, session factories, and focused query helpers |
| `migrations/` | Apply schema changes through Alembic |

`ApplicationResources` is published as `app.state.resources`. Dependency providers
expose settings, session factories, transfer jobs, rate limiters, and application
services individually. `get_resource_service` and `get_oauth_service` construct
lightweight service instances with the lifespan-owned Monzo client. The private
client provider is used only within dependency composition and credential
resolution; routers never receive it or the full resource container. The underlying
scheduler remains available for maintenance jobs.
A SQLAlchemy session belongs to one logical operation; the shared object is the
session factory, never a live session.

## Scheduling and execution

`ScheduleTransferRequest.to_command()` converts HTTP input into a validated
`ScheduleTransferCommand`. The command also validates inputs when constructed by
non-HTTP callers. `services/schedules.py` creates, lists, cancels, and resumes
schedules. Creation and listing return `ScheduledTransferDetails`, rather than
SQLAlchemy models or APScheduler jobs.

`domain/recurrence.py` operates on a `Recurrence` value. It preserves UK-local
schedule times across daylight-saving changes and clamps monthly occurrences to
short months while retaining the originally requested day.

`services/scheduler.py` owns APScheduler trigger registration and job removal.
The `TransferJobs` interface exposes scheduling/removal by occurrence ID and
timestamp; application services do not import APScheduler. Its adapter receives
the execution callback during application composition. Startup
reconciliation restores pending/running occurrences from active setups using the
same occurrence IDs and cancels occurrences belonging to inactive setups.

`services/transfer_execution.py` claims a pending occurrence atomically, resolves
provider credentials, performs the transfer, and records the outcome and next
occurrence. Per-user locks serialize execution and scheduling mutations. The
occurrence ID is also the provider deduplication ID. Database failure after job
registration removes the registered job; registration failure rolls back the
corresponding database changes.

`services/notifications.py` sends best-effort feed notifications after the outcome
is persisted. Notification failure does not alter the transfer outcome.

## Authentication and sessions

`AuthenticationContext` contains the authenticated user, application session ID,
and bearer token. `MonzoSession` additionally contains usable provider credentials.
Credentials are excluded from their representations. HTTP dependencies distinguish
application authentication from resolving a usable Monzo connection.

`services/authorization.py` validates signed access claims and persisted session
state. Mutating workflows recheck authorization inside the user lock, so a request
authenticated before logout or disconnect cannot bypass later revocation.

`OAuthService` in `services/oauth.py` receives its client, settings, and session
factory through its constructor. It owns the Monzo login workflow: exchange the
authorization code through the client, then issue the application session. The callback router handles
the browser state cookie, HTTP error mapping, and response headers.

`services/sessions.py` owns issuance, logout, and application refresh rotation.
Issuance persists provider credentials and the new application session in one
transaction. Database query helpers do not commit. `services/token_crypto.py`
handles signing, hashing, and encryption without persistence or session policy.

`services/monzo_credentials.py` resolves and refreshes provider credentials using
separate per-user refresh locks. `services/disconnection.py` deactivates schedules,
cancels pending transfers, and revokes app sessions before attempting provider
revocation, retaining retry state until provider disconnection is confirmed.

Refresh rotation preserves the fixed five-second retry window, 60-day inactivity
expiry, persistent reuse detection, and session-specific logout. Retry results
remain bounded, process-local, and scoped to the application session factory.
Reconnection permits new schedules but does not reactivate cancelled setups.

## Monzo client ownership

`MonzoClient` receives an HTTPX async transport. Resource methods validate provider
payloads and raise sanitized errors defined in `domain/monzo_errors.py`; routers
map those errors to the
existing HTTP responses. All resource routes call workflows in
`ResourceService` in `app/services/resources.py`, which receives the client through
its constructor and owns account aggregation, balance lookup, and pot lookup. The service provides a shared entry point for future business rules;
the client owns provider HTTP requests and response validation. Account aggregation
combines accounts and balances while tolerating individual balance failures.

Services do not own or close their injected clients and never retain user tokens
as instance state. Lightweight service construction creates no HTTP transports.
Request flows reuse the client owned by the application lifespan. Background
execution and disconnection run in separate `asyncio.run()` event loops, so each
owns a client scope in its own loop. That scope reuses its transport for credential
refresh, the transfer/revocation, and notifications, then closes it. An injected
client remains owned by its caller. Clients never retain a user's bearer token
as a shared default header.

## Verification

`tests/unit/test_architecture.py` guards against client imports, client access, and
full resource-container dependencies in routers. Service injection remains
overridable through FastAPI dependency overrides for HTTP tests.

Run `uv run pytest` for deterministic unit and mocked integration tests. Tests
cover recurrence, HTTP contracts, refresh replay/reuse, security races,
disconnection, rollback/job compensation, restart recovery, client ownership,
notification failures, and failed startup cleanup.

Before committing, stage the intended files and run
`uv run python scripts/check_sensitive_files.py`. Continue to apply migrations
with Alembic and run one application worker; this refactor does not change the
schema, deployment model, or public API contract.

## Telemetry ownership

`app/telemetry/` owns explicit OpenTelemetry providers, bounded background
exporters, a sanitized application-event logging bridge, and the official
FastAPI instrumentor. The lifespan constructs providers, installs the library's
HTTP instrumentation, and removes it on cleanup; app imports start no export
workers. Library instrumentation observes security and body-limit rejections.
A small middleware adds request/log correlation, and a span processor filters
automatically captured attributes, exceptions, and trace state before export. Routers and business services
keep using the existing logging helpers. No provider is registered globally and
no monitoring transport uses the Monzo HTTP client. See
[observability](observability.md) for data boundaries and delivery limitations.
