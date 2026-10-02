# SMS evidence rollout and production acceptance

This runbook closes the gap between historical EVE send logs and the signed GMweb lifecycle evidence introduced by `sms-operations-trace-v1`.

## Truth model

- `SmsSendLog.status` is EVE's historical/local send log. It is **not** retroactive proof of a signed GMweb callback and is never carrier-delivery proof.
- `gateway.accepted` / `send.queued` prove gateway acceptance/queueing only.
- `send.sent` / `send.completed` prove Android/modem submission recorded by GMweb.
- `sms.delivered` / `sms.delivery_failed`, or a separately persisted carrier DLR with provenance, are the only carrier terminal evidence.
- Rows without `eve_notification_id` are legacy/unmeasured for signed-callback purposes. Do not backfill or synthesize signed evidence for them.

## Required production configuration

Generate one random secret outside source control and configure the **same** value in EVE and GMweb. Never paste it into Git, logs, shell history, or a support ticket.

EVE:

```text
EVE_SMS_EVENTS_SECRET=<same random secret, at least 32 characters>
# Optional; default 300, allowed 60..3600 seconds
EVE_SMS_EVIDENCE_GRACE_SECONDS=300
```

GMweb:

```text
EVE_SMS_EVENTS_URL=https://<eve-public-host>/internal/gmweb/sms/events
EVE_SMS_EVENTS_SECRET=<same random secret>
```

`EVE_SMS_EVENTS_URL` must be HTTPS and contain no embedded credentials. Do not point the legacy `WEBHOOK_URL` at the signed event endpoint.

## Safe rollout order

1. Deploy EVE first. The ingestion endpoint and the new neutral legacy states are backward-compatible.
2. Confirm EVE migrations are at the expected head and `sms_gateway_events` exists.
3. Configure EVE's callback secret and restart EVE web/background processes according to the deployment's normal service manager.
4. Deploy/configure GMweb with the matching URL/secret and restart it.
5. Open SMS Center → Gateway. GMweb transport health already exposes `diagnostics.callbackOutbox`; verify:
   - `configured=true`
   - `validConfig=true`
   - `worker_running=true`
   - `dead_letter=0`
6. Check EVE `GET /api/sms/evidence-health` as an authenticated operator. `not_configured` is a configuration error. `degraded` means callback-era EVE sends older than the grace period have no matching signed callback. Legacy rows are excluded by design.

## Canary acceptance

Use one controlled recipient and one normal EVE send. Do not bulk-send for acceptance.

Within the grace period:

1. EVE creates a send log carrying `eve_notification_id`.
2. GMweb accepts the request and enqueues immutable signed callback evidence.
3. The GMweb callback outbox reaches `delivered`; `last_success_at` advances.
4. EVE's message timeline correlates events by **all available bounded identities** (`message_id/request_id`, `trace_id`, `eve_notification_id`, `gateway_request_id`) rather than stopping after the first populated identifier.
5. After `send.sent`, Gateway submission is `sent` and confirmed.
6. If no carrier DLR capability is exposed, Carrier outcome is the neutral `not_exposed` state. It must not be shown as failed or as a yellow delivery promise.
7. If a real DLR arrives, only then may Carrier outcome become `delivered` or `failed`.

## Expected historical UI

Messages recorded before the signed callback identity existed show:

```text
Gateway submission: legacy record
Submission evidence: Historical evidence was not collected for this message
Carrier outcome: historical unavailable
Carrier evidence: Historical evidence was not collected for this message
```

These are neutral informational states. A green historical EVE send-log status may still be shown separately, labelled **EVE send log**, because it describes EVE's historical record rather than callback/carrier proof.

## Actionable callback failure

For a callback-era log (`eve_notification_id` present) with no matching signed event:

- younger than the grace period → `pending` / `callback_pending` (not actionable yet)
- older than the grace period → `callback_missing` / `callback_missing` with `actionable=true`

When callback evidence is missing:

1. Check SMS Center → Gateway → `Callback outbox`.
2. If `dead_letter > 0`, inspect `last_http_status`, `last_error_code`, `last_failure_at` and URL/secret configuration.
3. Fix configuration/reachability before replaying.
4. GMweb already provides the bounded admin operation `POST /admin/eve-callbacks/requeue` to requeue immutable dead letters. Requeue only after the cause is fixed; do not mutate callback bodies or delivery IDs.
5. Refresh SMS Center and verify EVE's `missing_after_grace` falls to zero and `last_callback_received_at` advances.

## Rollback

The change is display/correlation/audit-only for historical rows; it does not rewrite old send logs or synthesize delivery evidence. If rollback is required, revert the EVE application release. The signed callback journal is append-only evidence and should not be deleted during rollback.
