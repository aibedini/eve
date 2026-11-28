# EVE and GOD AI integration

EVE implements XR-GA-009 and XR-GA-010. Apply the normal EVE upgrade so Alembic creates the machine API tables before starting the web and background processes. The background worker indexes existing customer phones and builds the service inventory after startup. A valid lookup may return an empty list until the index is ready. Service contact hashes refresh with the background worker as EVE receives fresh panel snapshots.

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
| `EVE_GOD_AI_CA_FILE` | Optional absolute path to a readable PEM CA bundle for the internal GOD AI certificate. Mount it read-only for the worker. If unset, the worker uses Requests' default CA bundle. TLS hostname and certificate validation remain enabled. |
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
    large_client_header_buffers 2 320k;

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

For batches, configure `GUNICORN_CMD_ARGS=--limit-request-line=0` on the EVE web process to let the proxy enforce the request-line limit. A full batch of phone hashes exceeds Gunicorn's default 4094-byte limit. The example proxy bounds request lines at 320 KiB on the dedicated internal listener, including fully URL-encoded service lookups. Keep the Gunicorn upstream reachable only by the proxy and keep the existing request-line limits on public listeners. If other infrastructure imposes a shorter limit, split requests by their encoded size.

## Wire details

- Add `context.customer.read_name` alongside `context.customer.read` to receive the actual `display_name` from customer detail, list and phone lookup. Existing keys continue to receive masked names. Phone, Telegram and card projections are unaffected by this scope.
- Pass multiple values by repeating `phone_hash` or `client_email` in the query string, up to 100 values per request. Each request consumes one rate-limit slot. With one input, response shapes stay the same. A single service lookup returns `404` for no match and `409` for multiple active matches.
- A batch returns HTTP `200` with `items` in request order and a zero-based `index` for each input. Phone batch entries use `{index, items}` with a list of customer matches, including an empty list for a miss. Service batch entries use `{index, status, item?}`. The status is `200`, `404`, or `409` and `item` appears only for `200`. Duplicate inputs keep their positions. A malformed input rejects the whole request with `400`.
- With `context.service.read`, `GET /machine/v1/services?updated_since=&cursor=&limit=` lists the full inventory from EVE's customer ownership, reseller ownership, and panel snapshots. It returns `{items, next_cursor}` plus the standard envelope. Send `updated_since` as RFC 3339 in UTC and pass each `next_cursor` back unchanged with the same filter. The cursor orders rows by the last indexed service change and then service identity. Those changes include ownership, contact, expiry, quota limit, and snapshot status. Routine traffic counter updates do not advance the cursor. `customer_id` is `null` when the service has no active customer association. Email lookups search this same inventory.
- `GET /machine/v1/services:lookup?phone_hash=…` takes one HMAC digest and returns `{items}` plus the standard envelope, with zero or more service projections. It uses the SMS contact extracted first from the service's account name and then from its panel comment, regardless of SMS eligibility or opt-out. This lookup is distinct from `customers:lookup`, which uses the customer's primary phone. Combining `phone_hash` and `client_email` returns `400`.

- Treat `service_key` and `operation_id` as opaque strings. EVE uses deterministic authenticated encryption for service references and HMAC for operation references so a reference never exposes a VPN credential or a client-supplied idempotency key. URL-escape service keys on paths.
- The knowledge response has four named collections. FAQ items have `id`, `title`, `content`, and `platform`. Renewal templates have `id`, `name`, and `content`. Notification templates add `type`. Online chat scripts expose `id`, `name`, and `is_active`. EVE keeps executable embeds private.
- SMS notification items have `notification_id`, `status`, `state`, `recipient_masked`, and `created_at`. Payment responses include completed customer-wallet transactions and cash payments linked to a verified renewal under the current ownership. Wallet payment IDs are positive, and cash payment IDs are negative.
- Approving or rejecting a proposal records a human decision in EVE. The API and review screen have no proposal execution path.
- Share [eve-god-ai-v1-vectors.json](eve-god-ai-v1-vectors.json) with GOD AI for response projections, phone normalization and webhook verification. Hex-decode `public_key_hex` to get the fixture's 32 zero bytes. In production, use the UTF-8 bytes of each configured pepper or signing secret directly. GOD AI verifies the signature over the raw body, enforces a 300-second timestamp window, and returns `202` for both accepted and duplicate deliveries. EVE retries with a fresh signed timestamp and the original delivery ID and body.
