# Data Model: GMweb Contract v5 Evidence

## SmsGatewayEvent

Append-only authenticated event journal.

| Field | Constraint | Purpose |
|-------|------------|---------|
| `event_id` | existing primary key, max 160 | Idempotent carrier/gateway event identity |
| `trace_id` | existing, max 64, indexed | EVE/GMweb correlation |
| `message_id` | existing, max 128, indexed | GMweb message identity |
| `eve_notification_id` | existing nullable, max 120 effective, indexed | Stable logical EVE notification identity |
| `request_id` | nullable, max 120, indexed | GMweb public request identity when distinct from message identity |
| `gateway_request_id` | nullable, max 120, indexed | Android gateway request correlation |
| `carrier_status` | nullable enum-sized string | `unavailable`, `pending`, `delivered`, or `failed` when supplied |
| `evidence` | nullable bounded string | Privacy-safe evidence label, never raw callback body |
| `event_type` | existing indexed | Gateway/submission/carrier event type |
| `occurred_at` | existing datetime, indexed | Evidence chronology |
| `received_at` | existing datetime | Callback arrival chronology |

No recipient, SMS text, credential, conversation, or callback body is stored.

## Derived submission projection

States: `queued`, `sent`, `failed`, `cancelled`, `superseded`, `unknown`.

The strongest applicable event by occurrence time determines the state. `send.sent` confirms Android/device submission, not carrier delivery.

## Derived carrier projection

States: `unavailable`, `pending`, `delivered`, `failed`.

`sms.delivered`/explicit delivered receipt and `sms.delivery_failed`/explicit failed receipt are terminal carrier evidence. Weaker gateway/submission events cannot downgrade them. With submission evidence but no receipt, state is `pending` when receipt capability is indicated and otherwise `unavailable`.

## Delivery-event diagnostic result

Transient only. It contains bounded identifiers, status/carrier status, occurrence/receipt timestamps, callback state, and privacy-safe evidence fields. It is not inserted into the signed event journal.

## Relationships

- `SmsGatewayEvent.eve_notification_id` ↔ `ServiceNotificationEvent.event_id`
- `SmsGatewayEvent.message_id/request_id` ↔ `SmsSendLog.request_id`
- `SmsGatewayEvent.trace_id` ↔ `SmsSendLog.correlation_id`
- `SmsGatewayEvent.gateway_request_id` ↔ Android/provider evidence only

## Migration

One additive Alembic revision adds the new nullable fields and query-driven indexes on `request_id`, `gateway_request_id`, and `occurred_at`. Existing indexed fields remain unchanged.
