# GMweb gateway contract

The consumer side of the SMS gateway integration is defined by
`shared/eve-gmweb-contract-v1.json` (contract version 3). The file is
byte-identical to the gateway's own copy of the same name, so the two sides
cannot drift silently.
`panel/services/gmweb_contract.py` is the only code that reads it, so endpoint
paths, URL rules and request headers cannot drift from the declaration: a
mismatch fails `tests/test_gmweb_contract.py` instead of failing a send in
production.

## Declared surface

| key | method | path | scope |
|-----|--------|------|-------|
| ready | GET | /ready | sms.capacity |
| send | POST | /send | sms.send |
| send_status | GET | /send/status/{requestId} | sms.status |
| send_cancel | POST | /send/cancel/{requestId} | sms.cancel |
| send_capacity | GET | /send/capacity | sms.capacity |
| post_invalidate | POST | /send/invalidate | sms.invalidate |

Every authenticated call sends `Authorization: Bearer <projectKey>` and
`Accept: application/json`; `/send` and `/send/invalidate` also send
`Content-Type: application/json` and, when the caller has one, `Idempotency-Key`.
Request bodies are `{"to", "text", "priority"}` with the priority lane mapped
from Eve's canonical kinds (critical 1, expired 3, expiring 6, announcement 10),
plus an optional `meta` block (see below).

Timeouts come from the settings (`sms_gmweb_timeout`, bounded 3-90 s, default 15)
and are capped at 5 s for the read-only calls. Retries: one extra attempt when an
idempotency key is present, on a transport error **or** a 5xx response — a 5xx is
ambiguous, and the key makes the retry converge. Without a key there is no retry,
because a duplicate send would be worse than a failure.

## Notification metadata on /send

Every automated depletion reminder is tagged so a later renewal can revoke it:

```json
{
  "to": "0912xxxxxxx",
  "text": "...",
  "priority": "expiring",
  "meta": {
    "source": "eve",
    "serviceKey": "eve:<serverId>:<clientUuid>",
    "notificationKind": "volume_ended",
    "generation": 17,
    "correlationId": "<uuid>",
    "requiresValidation": true
  }
}
```

`notificationKind` is one of `near_expiry`, `low_volume`, `expired`,
`volume_ended`, `created`, `renew`. Eve's internal state name `ended` is
translated to the external `volume_ended` in exactly one map
(`panel/services/lifecycle.py`), so existing settings and cooldown keys keep
working.

The transactional `created` / `renew` confirmations carry
`requiresValidation: false` and are **not** in the invalidation-eligible set.
That is what stops a renewal from cancelling the message telling the customer
the renewal worked.

## Lifecycle invalidation: POST /send/invalidate

After a renewal commits, Eve posts:

```json
{
  "source": "eve",
  "serviceKey": "eve:<serverId>:<clientUuid>",
  "currentGeneration": 18,
  "invalidateKinds": ["near_expiry", "low_volume", "expired", "volume_ended"],
  "reason": "renewed",
  "correlationId": "<uuid>",
  "eventId": "lc:eve:<serverId>:<clientUuid>:<operationId>"
}
```

Expected answer:

```json
{
  "ok": true,
  "currentGeneration": 18,
  "cancelledPending": 2,
  "revokedActive": 1,
  "revokedInflight": 1,
  "alreadyTerminal": 0
}
```

Gateway semantics Eve relies on:

* only sends whose stored `meta.source` + `meta.serviceKey` match are touched —
  a phone number is never an identity, so renewing one service cannot silence
  another service that shares the phone;
* a not-yet-started send is cancelled; an already-started one is revoked
  (signalled to stop), never re-invented;
* `currentGeneration` is a monotonic per-service watermark, so a delayed
  invalidation carrying an older generation cannot revoke a newer lifecycle's
  reminder (409 `stale_generation`);
* the request is idempotent on `eventId`: a retry replays the original answer and
  changes nothing.

Eve validates the answer before trusting it
(`lifecycle.validate_invalidation_response`): `ok` must be `true` and every
count must be a non-negative integer, otherwise the answer is treated exactly
like a transport failure and the event stays in the durable outbox.

### Failure semantics

A gateway outage must never fail a customer's renewal. The generation bump and
the outbox row are committed together **before** the gateway is contacted; if the
call fails (timeout, 429, 5xx, malformed body, gateway not configured) the row
stays `pending` with its reason, HTTP status and attempt count persisted, and
`invalidation_outbox_worker` retries with a bounded backoff (5 s, 30 s, 2 m,
10 m, 30 m, 1 h, 3 h, then 3 h indefinitely). `pending_invalidation_count()` is
the metric to alert on.

## Base URL validation

`validate_base_url()` runs before any request is made, in every GMweb call:

* the scheme must be `http` or `https`. `file://`, `ftp://`, `gopher://` and
  `javascript:` are refused, so a saved setting cannot turn the SMS worker into
  a local file reader or an arbitrary-protocol client;
* a host is required, and embedded credentials (`https://user:pass@host`) are
  refused — the bearer key must never be sent to an origin the operator did not
  intend;
* a query string or fragment is refused; a path prefix is allowed;
* plaintext `http` to a non-local, non-private host is allowed but logs a warning
  once per process, because the project key then travels in clear text. Local and
  private addresses (127.0.0.1, localhost, 10/172.16-31/192.168/169.254,
  `*.local`, `*.internal`) are treated as trusted transport.

A refused URL produces `gateway_not_configured`-style reasons such as
`invalid_gateway_scheme:file` and the request is never made.

## Status polling

The poller never follows an absolute status URL supplied by the gateway unless it
stays on the configured gateway origin; otherwise it falls back to the declared
`/send/status/{requestId}` path. That keeps a compromised or mistaken gateway
response from turning the status worker into a general-purpose URL fetcher.

## Revocation is durable on the gateway side

The gateway does not merely delete a queued row. A revoked task becomes
**superseded**: terminal, not successful, not billable, not retryable, and not
counted as a gateway failure. The Android bridge answers `status:"superseded"`
from `POST /gateway/validate` before the modem is ever touched, so a reminder
that a renewal invalidated cannot be delivered by a device that was mid-download
when the invalidation landed.

The one thing physics forbids is un-sending: if a revoked task reports a *real*
submission, the physical outcome wins and the gateway records
`sent_after_revocation` instead of pretending it was cancelled. EVE treats that
row as sent, which is the honest reading.

Observable counters (declared as `lifecycleMetrics`, exposed by
`GET /admin/overview` with the master token): `sms_invalidations_total`,
`sms_jobs_superseded_total`, `sms_inflight_revoked_total`,
`sms_sent_after_revocation_total`, `sms_validation_requests_total`,
`sms_validation_invalid_total`, `sms_stale_generation_rejections_total`,
`sms_queue_removal_failures_total`. The last one — stale-generation rejections —
is the signal that a renewal's invalidation arrived out of order, which is
should-never-happen rather than routine.

## Verification

`tests/test_gmweb_contract.py` runs the real client functions against a local
fake gateway and asserts the wire contract:

* the contract file declares version 3, the five scopes and the six endpoints,
  and `messaging.py` no longer hardcodes any of the paths;
* URL validation accepts https and local http, warns on remote http, and refuses
  non-http schemes, credentials, missing hosts and query/fragment URLs;
* `/send` sends the declared method, path, auth and idempotency headers and the
  exact JSON body, and parses `requestId`/`status`;
* a 429 is surfaced with `Retry-After`; a 5xx is retried once with the *same*
  idempotency key and no retry happens without one;
* a malformed capacity body is reported as `invalid_capacity_response`;
* cancel quotes the reference into the declared path;
* an invalid base URL never reaches the gateway;
* a status URL from a foreign origin is not followed.

## Lifecycle consistency docs

The end-to-end design (stable `serviceKey`, the durable generation, the scanner
stale-snapshot guard and the outbox) is documented in
[docs/SMS_LIFECYCLE_INVALIDATION.md](SMS_LIFECYCLE_INVALIDATION.md), including a
sequence diagram of the renewal flow.

## Residual risk

* The gateway is external: a change on its side is detected when the contract
  test runs against the fake server only for the parts Eve controls. The
  documented response fields are parsed defensively (missing fields become
  `None`), so a partial response does not raise.
* Plaintext http to a private address is still plaintext on the wire; the warning
  exists because many gateway deployments live on a LAN.
* Idempotency depends on the gateway honouring the key. Eve sends the same key on
  a retry, which is the strongest guarantee the consumer can make.
