# Production deployment security

The [deployment workflow](../.github/workflows/deploy.yaml) runs on pushes to
`main` or manual dispatch; its deployment job runs only for `main`. It uses
native OpenSSH, verifies the server host key, and receives
SSH credentials in a job that runs no third-party actions. Build actions are
pinned to reviewed commit IDs. Deployment reuses the
[CI workflow](../.github/workflows/ci.yaml), waiting for lockfile validation,
dependency compatibility checks, the test suite, and a production-only
installation/migration/startup check. It deploys
only the tested SHA, using a fast-forward update with no reset of server files.

Before enabling this workflow, configure the `production` GitHub environment
with HOST, USER, KEY, and SSH_KNOWN_HOSTS secrets. Obtain the host key through the
Droplet console or another trusted channel; verify the fingerprint before adding
the OpenSSH known_hosts entry. Do not trust an unverified ssh-keyscan result.
The workflow intentionally fails if the host identity is missing or mismatched.

Restrict environment access to main, require the security/test jobs in branch
protection, and protect workflow changes through code review. Restrict the deploy
SSH user to the required git/uv/service commands; do not grant unrestricted sudo.
Use a distinct SSH key for this service, disable forwarding and interactive use,
and restrict source addresses where operationally possible.

Configure GitHub secret scanning/push protection and a policy requiring action
commit pins. Review Dependabot updates before merging them. Keep the application
service account unprivileged and restrict its database and secret files.

Set `APP_ENV=production` and an HTTPS `MONZO_REDIRECT_URI`. Startup requires a
valid Fernet `TOKEN_ENCRYPTION_KEY`, a `JWT_SECRET_KEY` of at least 32 bytes,
and `JWT_EXPIRATION_SECONDS` between 1 and 3600. Production rejects HTTP
requests, signing secrets starting with `replace-`, `your-`, or `test-`, identical
signing/encryption keys, and the known all-zero/test encryption keys. These checks
do not establish that an arbitrary secret is strong; generate independent random
keys as described in the [README](../README.md#local-setup).
Configure hostname routing at the reverse proxy. API docs/schema
remain available in production. OAuth and refresh do not require a BFF key.

Review [deploy/monzo-scheduler.service](../deploy/monzo-scheduler.service) and
[deploy/nginx.conf](../deploy/nginx.conf) as server templates. Provision Python
3.14+ and uv at `/var/lib/monzo-scheduler/.local/bin/uv`, and a service-owned
checkout at `/srv/monzo-scheduler`. The deployment user needs access to the
specific commands used in the workflow, and the service user needs Git read
access to the repository.
Use DATABASE_URL=sqlite:////var/lib/monzo-scheduler/monzo_scheduler.db so database
writes fit the service sandbox. Keep the service's EnvironmentFile owner-readable
only (0600) and /var/lib/monzo-scheduler private (0700). Before deploying, provision
monzo-scheduler-migrate.service as a Type=oneshot unit using the same EnvironmentFile
as the app service. **This migration unit is required by the workflow but is not
included in `deploy/`; it must be created on the host before enabling deployment.**
Its ExecStartPre must validate APP_ENV=production, and ExecStart must run
`/var/lib/monzo-scheduler/.local/bin/uv run --no-sync alembic upgrade head`
from /srv/monzo-scheduler as the monzo-scheduler user, with write access to the
database directory. Use `UV_CACHE_DIR=/var/lib/monzo-scheduler/uv-cache`, matching
the application template, and allow
the deployment user to start this specific unit with sudo -n. Deployment stops
the app, waits for the migration unit to succeed, then restarts the app. Failure
leaves the app stopped. The workflow installs frozen production dependencies
before stopping the app; schedule deployments for a suitable maintenance window.
The service binds only to loopback,
uses one worker, trusts forwarding headers only from the local proxy, and cannot
write its source tree. The proxy replaces incoming forwarding headers and logs
only method/path/status, omitting query strings and tokens. Review error logging
and external monitoring separately. Restrict firewall ingress to HTTPS and
administrative SSH; never expose port 8000 publicly.

Templates are not applied to the Droplet automatically. Validate `nginx -t` and
`systemd-analyze verify` on the host, provision the certificate and hostname, and
review the sandbox paths before installing them. Maintain encrypted backups with
0700 directories/0600 files and keep backup encryption keys separately. Never
back up plaintext historical databases alongside current credentials.

After deployment, check `https://<your-host>/health` and the service logs.
The workflow checks systemd service activity, but does not perform an HTTPS
health request or verify Monzo connectivity. `/health` reports application
liveness only. See the [recovery guide](token-exposure-recovery.md) for handling
historical databases and credentials.
