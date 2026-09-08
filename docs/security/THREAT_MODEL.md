# Threat Model

Protected assets include administrator sessions, X-UI credentials, messaging
tokens, customer identifiers, financial records, database backups, and TLS
private keys.

Primary threats are credential theft from Git or backups, session/CSRF abuse,
MITM against outbound panel calls, malicious cache payloads, excessive reseller
authorization, and command/file abuse through operational endpoints.

Controls include least-privilege route guards, same-origin mutation checks,
verified TLS with custom-CA support, encrypted sensitive columns, authenticated
backup encryption, JSON-only Redis payloads, security headers, loopback binding,
and automated secret/dependency/static/container scanning.

Residual risks: a fully compromised application account can access decrypted
data required for current work; database backups retained locally remain
sensitive and require encrypted storage/volume controls; Git history remains an
incident until the operator rotates exposed values and completes the coordinated
rewrite described in the runbook.
