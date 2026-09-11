# Network and TLS policy

## Topology

A reverse proxy (Nginx/Caddy, or the bundled container proxy) terminates public
TLS. Gunicorn listens on the internal interface only. The application therefore
has to distinguish two addresses: the direct peer it is talking to, and the
client that peer is proxying for.

## Client identity and trusted proxies

`X-Forwarded-*` headers are trusted only when the direct peer is allowed to
proxy. The default policy trusts loopback, private and link-local peers (a
proxy on the same host or on a container network) and never trusts a peer that
connects from a public address. `TrustedProxyMiddleware` runs outside Werkzeug
`ProxyFix` and, for an untrusted peer, removes every forwarding header before
the application sees it:

| Header | Meaning |
|--------|---------|
| X-Forwarded-For | client chain |
| X-Forwarded-Proto | original scheme (drives `request.is_secure`, HSTS, CSP) |
| X-Forwarded-Host | original Host (drives `host_url`, CSRF origin check) |
| X-Forwarded-Port / -Prefix | original port and mount path |
| X-Real-IP / Forwarded | common proxy aliases |

`client_ip()` resolves the effective client: the chain is walked right to left,
skipping trusted proxies, so a client that prepends its own entries cannot
change the answer. Logs, audit rows, agent `last_ip` and the rate limiter key
(`limiter_client_key`) all use it, never the raw header.

Configuration:

- unset: default policy (loopback/private/link-local peers are proxies).
- `EVE_TRUSTED_PROXIES=10.0.0.5,172.18.0.0/16`: only those peers may proxy.
- `EVE_TRUSTED_PROXIES=*`: trust every peer (pre-hardening behaviour; only for a
  deployment where the application is unreachable from anywhere else).

If a request arrives with forwarding headers from an untrusted peer, the app
logs one warning naming the peer so a misconfigured proxy is visible.

## Outbound TLS

Certificate verification is never disabled. `outbound_tls_verify()` returns
`True` (platform trust store) or a purpose-specific PEM bundle for private PKI:

- `EVE_XUI_CA_BUNDLE` for X-UI panels,
- `EVE_WHATSAPP_CA_BUNDLE` for the WhatsApp gateway,
- `EVE_OUTBOUND_CA_BUNDLE` as the shared fallback.

Every `requests.<method>` call in the application passes a timeout, and no call
disables verification. `tests/test_network_hardening.py` parses the application
sources and fails if a `verify=False`, an `ssl.CERT_NONE`/unverified context, or
a `requests.*` call without a timeout is introduced.

## Panel transport (plaintext credentials)

X-UI credentials (password or API token) must not cross the network in
plaintext. `enforce_panel_transport()` classifies a configured panel URL:

- `https://...` is always accepted;
- `http://127.0.0.1`, `http://localhost`, `http://[::1]` are accepted (the panel
  runs on the same host, the traffic never leaves the machine);
- any other `http://` host is refused.

The policy is enforced twice: when a server is created or its host is updated
(HTTP 400 with an actionable message) and again in the X-UI adapter before a
credential-bearing session is built, so a row edited directly in the database
cannot leak the password either. `EVE_ALLOW_INSECURE_PANEL=1` explicitly
re-enables plaintext panel access for a trusted LAN deployment.

The existing http-to-https self-heal still runs from the connection test: a
panel that only answers over TLS is upgraded to `https://` and then passes the
policy.

## Certificates

The certificate of the panel and of every https panel endpoint is monitored with
the same policy: a verified handshake or a reported failure, never an unverified
one. See `CERTIFICATE_MONITORING.md` for thresholds, the health-log category and
the `/api/doctor` API.

## Tests

`tests/test_network_hardening.py` covers the URL policy, the peer/chain
resolution, the middleware (stripping vs. passthrough), server create/update
validation, the audit IP for a spoofed `X-Forwarded-For`, the adapter refusal,
and the egress source guard.
