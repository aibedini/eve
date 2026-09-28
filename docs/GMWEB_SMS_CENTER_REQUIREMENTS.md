# GMweb requirements for Eve SMS Center

This is the GMweb-side handoff for Eve's current SMS operations UI. The machine-readable source of truth is `shared/eve-gmweb-contract-v1.json` version 5.

## Immediate production blocker

The project key configured in Eve must include all of these scopes:

```text
sms.send
sms.status
sms.cancel
sms.capacity
sms.invalidate
transport:read
```

The current key is missing `transport:read`. GMweb therefore returns `403 scope_denied` for `GET /eve/v1/transport-health`, so Eve cannot truthfully populate Device, Queue, active transport, last device presence, or last ACK. Issue/update the Eve project key with that scope; do not make this endpoint public and do not use the GMweb master token in Eve.

## Required read-only transport endpoint

```http
GET /eve/v1/transport-health
Authorization: Bearer <eve-project-key>
Accept: application/json
```

Required response contract version: `1`. Required top-level sections:

```json
{
  "contract_version": 1,
  "observed_at": "2026-09-27T12:00:00.000Z",
  "gmweb": { "ready": true, "reason": null },
  "transport": { "active": "android", "mode": "pull", "state": "connected", "reason": null },
  "device": {
    "state": "connected",
    "reason": null,
    "last_seen_at": "2026-09-27T11:59:57.000Z",
    "last_seen_age_ms": 3000,
    "age_ms": 3000
  },
  "queue": { "pending": 4, "inflight": 1 },
  "last_ack": { "at": "2026-09-27T11:59:55.000Z", "outcome": "sent" },
  "diagnostics": { "androidPull": null }
}
```

Rules:

- All timestamps are ISO-8601 UTC.
- `device` describes only the active transport. If Chrome is active, Android pull data stays under `diagnostics.androidPull` with `authoritative: false`.
- Never return keys, tokens, phone numbers, SMS text, conversation IDs, or recipient data from this endpoint.
- Return `401` for invalid authentication and `403` with a stable `scope_denied`/`project_scope_denied` signal when `transport:read` is absent.
- Keep `device.age_ms` as the compatibility alias of `device.last_seen_age_ms` during mixed-version rollout.

## Send and status evidence

GMweb must continue returning stable `requestId`, `jobId`, `status`, `statusUrl`, `terminal`, and `successful` fields from `/send` and `/send/status/{requestId}`. Status must distinguish gateway acceptance from physical submission. Eve treats `send.sent` as gateway-recorded device submission, not carrier delivery.

When lifecycle metadata is supplied on `/send`, persist it unchanged: `source`, `serviceKey`, `notificationKind`, `generation`, `correlationId`, optional opaque `eveNotificationId`, and `requiresValidation`. `POST /send/invalidate` must remain idempotent on `eventId` and generation-safe as specified by the shared contract.

`GET /send/status/{requestId}` exposes gateway status separately from
`carrierStatus`. Carrier states are `unavailable`, `pending`, `delivered`, and
`failed`; only authenticated Android DLR evidence can produce a terminal carrier
state. It also returns `gatewayRequestId`, `eveNotificationId`, and up to 50
bounded `carrierEvents` when available.

## Read-only carrier reconciliation

Implement `GET /eve/v1/sms-delivery-events` for keys with `sms.status`. Apply
project isolation and the exact v5 filters: `from`, `to`, `status`, `requestId`,
`eventId`, `callbackState`, and `limit` (maximum 100). The response may expose
only the declared projection. It must never return `to`, `text`, phone numbers,
credentials, or raw callback bodies.

## Signed event delivery to Eve

Configure GMweb with:

```text
EVE_SMS_EVENTS_URL=https://eve.rooteam.ir/internal/gmweb/sms/events
EVE_SMS_EVENTS_SECRET=<same random secret of at least 32 characters as Eve>
```

For every gateway/device state change, post the bounded event body and these headers:

```text
X-GMweb-Timestamp: <unix seconds>
X-GMweb-Delivery-Id: <stable delivery id>
X-GMweb-Signature: sha256=<HMAC-SHA256 hex>
```

The signature input is exactly:

```text
<timestamp>.<delivery-id>.<exact raw request body>
```

GMweb must retry non-2xx callbacks from its durable outbox. Event IDs are stable and replays are byte/meaning consistent. The body contains bounded evidence fields (`event_id`, `trace_id`, `message_id`, optional `request_id`, `gateway_request_id`, `eve_notification_id`, `carrier_status`, `evidence`, `type`, `occurred_at`, optional attempt/device/reason/stage) and never includes SMS text or a full recipient number.

Carrier delivery must only be reported when there is actual receipt evidence. If the Android/SIM path cannot provide a DLR, report physical submission (`send.sent`) and leave carrier delivery unavailable rather than inferring it from HTTP 200/202.

## Acceptance checklist for GMweb

- Eve project key includes all six scopes, especially `transport:read`.
- `GET /eve/v1/transport-health` passes the version-1 response contract for both Android-active and Chrome-active modes.
- `/send` and status polling keep stable IDs and status semantics.
- `/gateway/delivery-report` is authenticated, idempotent, conflict-safe, and commits its DLR plus immutable callback event atomically.
- `/eve/v1/sms-delivery-events` is project-isolated, bounded to 100, and contains no forbidden data.
- Historical callback dead letters are diagnostic and do not make a currently ready transport unready.
- Signed callbacks reach Eve over HTTPS, retry durably, and contain no message body/full phone.
- A staging run proves: queued → device submission/failed event → Eve Delivery log timeline.
- A renewal proves stale queued reminders are invalidated without cancelling a newer generation.
