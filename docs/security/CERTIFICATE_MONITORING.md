# Certificate monitoring (Eve Doctor)

## What is monitored

| Source | How | Why |
|--------|-----|-----|
| Panel certificate | The PEM the reverse proxy serves is parsed from disk | Expiry is visible even for a self-signed or already-expired certificate, and no network round trip is needed |
| https panel endpoints | A real TLS handshake against the platform trust store (or the configured CA bundle) | Catches an expired, wrong-host or untrusted panel certificate before the UI reports a bare connection error |
| Public panel URL | Same probe, when it is an https URL | The operator's own URL is the one customers see |

Plain-http panel endpoints are not probed: phase 7 already refuses to send panel
credentials over them.

## Thresholds and cadence

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_CERT_WARN_DAYS | 21 | below this many days: warning |
| EVE_CERT_CRIT_DAYS | 7 | below this many days: critical |
| EVE_CERT_CHECK_INTERVAL_SECONDS | 21600 (6 h) | cache lifetime and watchdog cadence |

States: ok, warning, critical, expired, error (verification/hostname/tls
failure), unknown (not configured). A certificate that is already past its
notAfter is always expired, never critical.

## Verification is never disabled

certificates.probe_tls_endpoint() always opens the connection with the platform
(or configured) trust store. An expired certificate is reported as expired, a
name mismatch as hostname_mismatch, anything else as verification_failed. There
is no unverified fallback handshake, and no code path sets verify=False or
ssl.CERT_NONE - tests/test_network_hardening.py enforces that across the tree.

The private key is never read: only the public certificate file is opened, and
only display-safe fields (subject, issuer, SANs, serial, SHA-256 fingerprint,
validity) are returned. Tests assert that no report can contain PRIVATE KEY.

## Where the results appear

- Background watchdog: one HealthLog row with category tls is written only when
  at least one certificate needs attention (level critical for expired/critical,
  warning otherwise). The check runs at most once per EVE_CERT_CHECK_INTERVAL_SECONDS
  even though the health cycle itself runs every 60 s.
- Settings -> System Logs: the TLS certificates card shows the panel certificate
  and each probed endpoint with state and days remaining, plus a "Check now"
  button that forces a refresh. The log list can be filtered by the
  TLS Certificate category.
- API:
  - GET /api/doctor/tls[?refresh=1] - full report (settings.read);
  - GET /api/doctor - compact health summary (database, disk, secret key, TLS,
    recent errors) plus the configured thresholds.

Both endpoints require the settings.read permission; resellers receive 403.

## Failure handling

The check is best-effort by design: a probe timeout, an unreachable endpoint or
an unexpected exception is reported as a detail and never breaks the health
cycle. The report is cached so repeated dashboard loads do not open sockets; a
forced refresh (manual run, "Check now", watchdog interval) rebuilds it.
