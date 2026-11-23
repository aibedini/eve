# EVE and GOD AI integration

EVE v2.23.22 implements XR-GA-009 and XR-GA-010. Apply the normal EVE upgrade so Alembic creates the machine API tables before starting the web and background processes. The background worker indexes existing customer phones in batches after startup. A valid lookup may return an empty list until the relevant batch completes.

## Deployment

Set these variables in the protected environment for the EVE web and background processes.

| Variable | Value |
| --- | --- |
| `EVE_MACHINE_INTERNAL_HOST` | Exact internal Host header used by the proxy below. The machine API stays disabled when unset. |
| `EVE_MACHINE_ALLOWED_CIDRS` | Comma-separated private source CIDRs. Defaults to IPv4 and IPv6 loopback. Restrict to GOD AI's addresses. |
| `EVE_MACHINE_PHONE_PEPPER` | Shared with GOD AI. Both sides use the UTF-8 bytes of the configured value as the HMAC key. |
| `EVE_MACHINE_PAYMENT_PEPPER` | Separate HMAC key for normalized sender cards. Leave unset to return null card hashes. |
| `EVE_MACHINE_SERVICE_KEY_SECRET` | EVE-only key for service and operation references. Keep it stable across processes, backups and upgrades. Changing it invalidates existing GOD AI references. |
| `EVE_GOD_AI_ORIGIN` | Fixed GOD AI HTTPS origin, without an API path. |
| `EVE_GOD_AI_WEBHOOK_SECRET` | Shared HMAC key for EVE webhooks. |

Generate separate random values for the peppers and secrets. EVE requires at least 32 UTF-8 bytes per value. Place the machine key issued by **GOD AI Integration** in GOD AI's secret store. Select only the scopes it needs. The admin screen displays the key once, tracks last use, and can revoke access immediately. The API allows 120 requests per key per UTC minute and returns `Retry-After` when that window is exhausted. Pass a UUID in `X-Request-ID` to correlate responses with EVE audit logs.

Use a dedicated HTTPS listener on loopback for a same-host deployment. Restrict the app upstream to the proxy, and set `EVE_TRUSTED_PROXIES` to the actual proxy addresses. When GOD AI has a separate host, bind the listener to a private interface and restrict the firewall, the Nginx `allow` list, and `EVE_MACHINE_ALLOWED_CIDRS` to GOD AI's private addresses. Configure GOD AI to trust the listener certificate and resolve the internal hostname to the bound address.

```nginx
server {
    listen 127.0.0.1:8443 ssl;
    server_name eve-internal.example;
    ssl_certificate /etc/nginx/tls/eve-internal.crt;
    ssl_certificate_key /etc/nginx/tls/eve-internal.key;
    access_log off;
    error_log /dev/null crit;

    location ^~ /machine/v1/ {
        allow 127.0.0.1;
        deny all;
        client_max_body_size 64k;
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host eve-internal.example;
        proxy_set_header X-Forwarded-Host eve-internal.example;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Prefix "";
        proxy_set_header Cookie "";
    }

    location / { return 404; }
}
```

Set the upstream port to the installed EVE port and set `EVE_MACHINE_INTERNAL_HOST=eve-internal.example` for this example. On existing public vhosts add `location ^~ /machine/ { access_log off; return 404; }` before enabling the internal listener. The installer includes that block in newly generated public vhosts. Keep request URL, header and body logging disabled for this upstream, including in any load balancer or tracing agent. Check the effective configuration with `nginx -t` before reloading.

## Wire details

- Treat `service_key` and `operation_id` as opaque strings. EVE uses deterministic authenticated encryption for service references and HMAC for operation references so a reference never exposes a VPN credential or a client-supplied idempotency key. URL-escape service keys on paths.
- The knowledge response has four named collections. FAQ items have `id`, `title`, `content`, and `platform`. Renewal templates have `id`, `name`, and `content`. Notification templates add `type`. Online chat scripts expose `id`, `name`, and `is_active`. EVE keeps executable embeds private.
- SMS notification items have `notification_id`, `status`, `state`, `recipient_masked`, and `created_at`. Payment responses include completed customer-wallet transactions and cash payments linked to a verified renewal under the current ownership. Wallet payment IDs are positive, and cash payment IDs are negative.
- Approving or rejecting a proposal records a human decision in EVE. The API and review screen have no proposal execution path.
- Share [eve-god-ai-v1-vectors.json](eve-god-ai-v1-vectors.json) with GOD AI for response projections, phone normalization and webhook verification. Its HMAC key is a public test vector. GOD AI verifies the signature over the raw body, enforces a 300-second timestamp window, and returns `202` for both accepted and duplicate deliveries. EVE retries with a fresh signed timestamp and the original delivery ID and body.
