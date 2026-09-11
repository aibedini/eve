# Security Architecture

## Trust boundaries

- Nginx/Caddy terminates public TLS. Native Gunicorn listens only on
  `127.0.0.1`; Docker exposes it only to the internal network.
- `X-Forwarded-*` headers are trusted only from an allowed proxy peer
  (loopback/private by default, `EVE_TRUSTED_PROXIES` to narrow it). Headers from
  any other peer are stripped before routing, so the rate limiter and the audit
  trail keep the real client address. See `NETWORK.md`.
- Browser users authenticate with an HttpOnly, Secure, SameSite session cookie.
  Unsafe session requests must carry a same-origin `Origin` or `Referer`.
- BNQO agents use bearer authentication plus Ed25519 request signatures and do
  not rely on browser sessions.
- X-UI, WhatsApp, and other outbound HTTPS calls always verify certificates.
  Private PKI deployments configure a CA bundle instead of disabling TLS.
- Panel credentials are never sent over plaintext HTTP to a remote host: a panel
  URL must be `https://` unless it points at loopback. `EVE_ALLOW_INSECURE_PANEL=1`
  is the explicit opt-in for a plaintext LAN panel.
- Redis is an ephemeral internal cache. Values use compressed JSON, never
  language-native object deserialization.

## Sensitive data

Application-managed secrets use a versioned `enc:v1:` Fernet envelope backed by
`SERVER_PASSWORD_KEY`. Financial identifiers and backup config URLs use the
same transparent encrypted type. Telegram backup files are encrypted in transit
to Telegram with streaming AES-256-GCM and `EVE_BACKUP_KEY`.

TLS private keys are host-managed, root-readable files. The web API cannot
upload, synchronize, or export them.
