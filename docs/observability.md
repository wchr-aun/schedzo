# Application telemetry

Schedzo can export endpoint metrics, application event logs, and request traces
straight to Grafana Cloud using OpenTelemetry's OTLP/HTTP protobuf exporters.
Telemetry is disabled by default. No collector, inbound port, local Loki database,
or `/metrics` endpoint is required. This document describes configuration;
production deployment and enabling the integration are operator actions.

## Configuration

Use the **OpenTelemetry / OTLP** connection details in your Grafana Cloud stack,
not the Loki-only push endpoint. Create a stack-scoped Cloud Access Policy token
with `metrics:write`, `logs:write`, and `traces:write`. Copy the OTLP base URL and
Authorization header from the setup page. A dashboard service-account token is
not the ingestion credential.

Add these settings to the application's private environment file:

```dotenv
OTEL_ENABLED=true
OTEL_SERVICE_NAME=schedzo
OTEL_EXPORTER_OTLP_ENDPOINT=https://YOUR-GRAFANA-OTLP-HOST/otlp
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20YOUR-BASE64-CREDENTIALS"
OTEL_TRACE_SAMPLE_RATIO=1.0
```

The Authorization value above is a placeholder: use the value Grafana supplies.
Header values are URL-decoded, so `%20` represents a space. Headers are excluded
from the settings representation. Do not put real credentials in Git, command
history, issue reports, or screenshots. The existing systemd service already
reads `/srv/monzo-scheduler/.env`. Outbound HTTPS is needed; no inbound monitoring
ports are needed.

Only the settings listed above are application configuration. Transport is
always HTTP/protobuf. The app appends `/v1/metrics`, `/v1/logs`, and `/v1/traces` to
the base URL. Do not include one of these signal suffixes in the base URL.
Telemetry-enabled startup validates the URL and headers without contacting the
Cloud service. Bad configuration fails with a sanitized message; Cloud outages
do not gate startup. Set `OTEL_ENABLED=false` and restart to disable exporting.

## Captured data

The official `opentelemetry-instrumentation-fastapi` library generates server
spans and endpoint duration metrics around the application middleware stack,
including early authentication, transport, and body-limit rejection responses.
A small correlation middleware adds request IDs and log context; it does not
create spans or time requests. The application opts into the library's stable
HTTP semantic conventions (`OTEL_SEMCONV_STABILITY_OPT_IN=http`) and drops other
library metric instruments through SDK views. Request counts still come from the
seconds-based duration histogram. Do not additionally launch this application
through `opentelemetry-instrument`: programmatic instrumentation already owns the
providers and lifecycle.

The library uses route templates for metric dimensions. Unmatched requests have
no route label and share a bounded metric series; exported unmatched spans/logs
use `unmatched`. Nonstandard methods use the library's `_OTHER` metric value and
`OTHER` in sanitized span names. Raw paths/resource IDs are not metric labels.

`/health`, `/docs`, `/redoc`, and `/openapi.json` are excluded. WebSockets, requests
rejected by nginx/Uvicorn before application dispatch, and host CPU/disk metrics
are outside this integration. Scheduled jobs retain their event logs, but do not
have custom job spans or business metrics. Monzo calls and SQL queries do not
have child spans. These can be instrumented separately later.

The histogram is `http.server.request.duration`, measured in seconds. Its
attributes are `http.request.method`, `http.route`, and
`http.response.status_code`. Histogram counts provide request counts, including
4xx/5xx, independently of trace sampling. Explicit bucket boundaries are
5/10/25/50/100/250/500 ms and 1/2.5/5/10 seconds.

Request spans include the same attributes and the locally generated request ID.
5xx responses and uncaught exceptions mark the span as an error. Exception
messages, events, stack traces, headers, bodies, client IPs, URL query strings,
baggage, and upstream `tracestate` are excluded from exported telemetry. The
library can populate sensitive attributes/status descriptions in live spans;
`SanitizingSpanProcessor` replaces them with an attribute allowlist and removes
all events, links, status descriptions, and trace state **before enqueueing**.
Request/response header capture is disabled in the instrumentation configuration,
and the export boundary also removes any headers captured through environment
settings. W3C `traceparent` is accepted
for correlation. Parent sampling decisions are respected; the configured ratio
applies to root traces. Lower sampling ratios cannot guarantee a trace for every
error. Metrics are not sampled.

Cloud logs export `schedzo` application logger events at INFO and above. The
bridge exports an approved event name as the body, severity, logger name, fixed
allowlisted failure reasons, and numeric status/duration/count fields. Request
logs also include request ID and route template; the logging bridge attaches the
active trace/span IDs. After application-session authentication succeeds, all
subsequent application logs in that request include the verified `user_id`,
including service logs and middleware error logs. It is an identifying value
sent to Grafana Cloud as structured log metadata, not a metric or indexed stream
label. Failed authentication, public requests, and scheduled jobs do not receive
this request-specific identity. Context is isolated between requests and cleared
when the request ends. Identity supplied in log messages or `extra` fields is not
trusted. Transfer/account/pot IDs, arbitrary `extra` fields,
exception content, and free-form messages are omitted. Unknown event messages
become `application_log_redacted`. The event/reason allowlists live in
`app/telemetry/logs.py`; extend them deliberately when introducing a new event.
Console logging retains the existing format. Uvicorn/library/exporter logs are
not forwarded, preventing duplication and exporter recursion.

## Resource use and reliability

Providers are owned by the application lifespan and are not registered globally.
The lifespan installs official FastAPI instrumentation after constructing the
providers, rebuilds the HTTP stack, and removes instrumentation during cleanup,
including failed startup. Constructing/importing the app starts no exporter threads. Logs/traces use
background batches every five seconds, with 2,048 queued records per signal and
128 records per batch. Metrics export every 60 seconds. Each exporter has a
five-second timeout; SDK retry behavior operates within its export deadline.
Attribute counts and lengths are bounded and metric attributes are allowlisted.

No persistent telemetry spool is written. Queues may drop records when full and
an outage/process crash can lose telemetry. Console logs remain in journald,
whose retention is configured independently by the host. This does not provide
reliable delivery of every log.

At shutdown the log handler is detached immediately and the providers drain in
a daemon worker with an eight-second wait budget. The wait runs off the async
event loop. If the budget expires, the worker can finish in the background;
process termination may discard remaining records. Existing scheduler jobs use
`shutdown(wait=False)`, so jobs still running during shutdown may lack final
telemetry. Exporter diagnostics stay in the local journal.

The supported production deployment has one application/telemetry lifespan per
process and one Uvicorn worker, as required by the scheduler. Separate app
factories use explicit providers and request context to prevent cross-app
request log export; background logs are process-wide. The official instrumentor
also patches Starlette background-task tracing while active, so overlapping
instrumented app lifespans in one process are not supported.

## Grafana dashboard and correlation

Import `deploy/grafana/endpoint-observability.json` through Grafana's dashboard
import UI. Select your Cloud Prometheus and Loki data sources when prompted.
The dashboard has a service selector, request rate, 4xx/5xx rates, latency
percentiles, and recent error logs. It is a starter artifact: it has not been
validated against your live Cloud stack.

Grafana Cloud normally translates this histogram into
`http_server_request_duration_seconds_{bucket,count,sum}` with labels
`http_request_method`, `http_route`, and `http_response_status_code`. Its OTLP
resource mapping uses `service.name` as the metrics `job` label and the logs
`service_name` label. Confirm received names if your stack changes translation.

Useful queries:

```promql
sum by (http_route) (
  rate(http_server_request_duration_seconds_count{job="schedzo"}[5m])
)
```

```promql
histogram_quantile(0.95, sum by (le, http_route) (
  rate(http_server_request_duration_seconds_bucket{job="schedzo"}[5m])
))
```

```logql
{service_name="schedzo"} |= "request_failed"
```

Filter logs for an authenticated user using structured metadata:

```logql
{service_name="schedzo"} | user_id="user_example"
```

The logs' severity is normally available as `severity_text` structured metadata:

```logql
{service_name="schedzo"} | severity_text=~"ERROR|FATAL"
```

For log-to-trace links, configure your Loki data source's derived fields using
its **label/structured metadata** matcher for `trace_id`, with an internal link
to your Cloud Tempo data source. An individual log may have no stored trace if
sampling omitted it. In Tempo Explore, search:

```traceql
{ resource.service.name = "schedzo" }
```

For trace-to-log links, map the resource `service.name` attribute to Loki's
`service_name` label and filter by trace ID. This configuration belongs to your
Cloud data sources and is not automatically applied by importing a dashboard.

This setup provides standard metrics/Explore views. It does not provision the
span-metrics and service-graph pipelines that Grafana's full Application
Observability product may require.

## Offline verification and troubleshooting

`uv run pytest` covers request grouping, log/trace correlation, OAuth redaction,
early rejections, failures, the span sanitization boundary, lifecycle cleanup,
disabled behavior, and mocked real
OTLP exporter requests. Tests use memory exporters or mocked urllib3 transport;
they require no Cloud account. The Python log SDK is still under development;
review its release notes when updating the locked OpenTelemetry packages.

If telemetry is missing, inspect `journalctl -u monzo-scheduler` locally. Check
that exporting is enabled, the endpoint is the OTLP base URL, the token is valid
and has all three write scopes, and outbound HTTPS is allowed. 401/403 indicates
credentials or permissions; 429 indicates throttling. Wait at least one metric
export interval and choose a suitable time range. Avoid posting credential
values when sharing diagnostics.

References: [Grafana direct export](https://grafana.com/docs/opentelemetry/instrument/),
[OTLP format and label mapping](https://grafana.com/docs/grafana-cloud/send-data/otlp/otlp-format-considerations/),
[OpenTelemetry Python exporters](https://opentelemetry.io/docs/languages/python/exporters/).
