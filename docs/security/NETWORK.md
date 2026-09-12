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

## Per-server insecure opt-in (Allow insecure connection)

Some operators run a panel that only speaks plaintext HTTP or presents a
self-signed/expired certificate. That must not require disabling verification
for the whole panel, so the exception is **per server**:

- `Server.allow_insecure` is a NOT NULL boolean defaulting to `false` (migration
  `c9d8e7f6a5b4`); every existing server keeps full verification;
- `panel_tls_verify(server)` is the single place that decides the `requests`
  `verify` value: `False` only for a server whose flag is on, otherwise `True` or
  the configured `EVE_XUI_CA_BUNDLE`;
- `enforce_panel_transport(url, allow_insecure=...)` refuses plaintext for every
  server that did not opt in, and accepts it for the one that did;
- the flag travels with the session: `get_xui_session()` pins the policy on the
  `requests.Session`, and every X-UI request (login, token auth, inbounds,
  onlines, status, clients, renew/reset, capability probes, connection test, the
  X-UI database backup and the subscription fetch) reads it back through
  `session_tls_verify()`. No caller hardcodes the value;
- `session_tls_verify()` is why the backup cannot disagree with the rest of the
  flow: the X-UI database download uses the same helper and refuses a plaintext
  panel that did not opt in;
- `EVE_ALLOW_INSECURE_PANEL=1` is deprecated. It still works as a process-wide
  fallback for callers without a server object, but an explicit per-server
  decision always wins, and the app logs one warning when the fallback is used.

Changing `host`, `username`, `password`, `api_token`, `allow_insecure` or
`panel_type` drops every X-UI session, capability and cookie cache for that
server, and the cached session key includes the transport policy, so flipping the
flag can never reuse a session that was built under the other policy. Turning the
flag **off** while the stored host is plaintext is refused: it would leave a
server that can never connect.

Enabling or disabling the flag writes an `AuditLog` row (`server.allow_insecure`)
with the old and new value and the host; passwords and tokens never appear in the
row or in any log line. `Server.to_dict()` exposes `allow_insecure` and
`has_password` metadata only.

## Outbound TLS

Certificate verification is never disabled globally and no caller hardcodes the
value (an opted-in server goes through `panel_tls_verify()` only).
`outbound_tls_verify()` returns `True` (platform trust store) or a
purpose-specific PEM bundle for private PKI:

- `EVE_XUI_CA_BUNDLE` for X-UI panels,
- `EVE_WHATSAPP_CA_BUNDLE` for the WhatsApp gateway,
- `EVE_OUTBOUND_CA_BUNDLE` as the shared fallback.

Every `requests.<method>` call in the application passes a timeout, and no call
has a literal `verify=False`. `tests/test_network_hardening.py` parses the
application sources and fails if a literal `verify=False`, an
`ssl.CERT_NONE`/unverified context, or a `requests.*` call without a timeout is
introduced; `tests/test_allow_insecure_server.py` covers the per-server matrix,
the isolation between two servers and the cache invalidation.

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
cannot leak the password either. A server whose operator enabled **Allow insecure
connection** is the only exception (see above); `EVE_ALLOW_INSECURE_PANEL=1` is the
deprecated process-wide fallback.

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
