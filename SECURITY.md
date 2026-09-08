# Security Policy

Do not open a public issue for a suspected vulnerability or leaked credential.
Report it privately to the repository owner with the affected version, impact,
and minimal reproduction. Do not include live credentials or customer data.

Supported security fixes target the current `main` branch and latest `2.x`
release. Operators should keep Eve, PostgreSQL/SQLite, Redis, the reverse proxy,
and X-UI panels updated.

If a secret or database was committed, treat every contained credential and
personal datum as exposed: rotate first, preserve evidence, then follow
[the incident runbook](docs/security/INCIDENT_RESPONSE.md).

Deployment and key-handling requirements are documented under
[`docs/security/`](docs/security/).
