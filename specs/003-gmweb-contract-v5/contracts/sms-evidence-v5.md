# Interface Contract: EVE SMS Evidence v5

## Provider delivery-event search

`GET /eve/v1/sms-delivery-events`, bearer scope `sms.status`.

Supported query fields: `from`, `to`, `status`, `requestId`, `eventId`, `callbackState`, `limit`. Time values are Unix milliseconds. `limit` is clamped to `1..100`.

EVE accepts only an object response containing a bounded event list and projects only privacy-safe identifiers, state, timestamps, callback state, and evidence. Forbidden recipient/text/credential/callback-body keys cause rejection of that row or response.

Capability absence (`404`, `405`, `501`) is a normal v4 rollout result. Authentication, scope, timeout, 5xx, and malformed responses are distinct diagnostic failures.

## Signed callback extension

The existing `POST /internal/gmweb/sms/events` remains the sole callback endpoint with the existing HMAC envelope. The bounded body additionally accepts optional snake_case wire forms for:

- `request_id` (max 120)
- `gateway_request_id` (max 120)
- `carrier_status` (`unavailable|pending|delivered|failed`)
- `evidence` (privacy-safe code, max 64)

`eve_notification_id` follows `^[A-Za-z][A-Za-z0-9_-]{0,119}$`.

## EVE timeline response

`GET /api/sms/messages/<log_id>/timeline` returns:

```json
{
  "submission": {"state": "sent", "confirmed": true, "evidence": "android_submission"},
  "carrier": {"state": "delivered", "confirmed": true, "evidence": "carrier_dlr"},
  "gateway_events": []
}
```

No receipt returns carrier `unavailable` or `pending`, `confirmed: false`, never failed.

## Controlled reconciliation

An authenticated SMS Operations read compares provider results with local event IDs and reports `remote_only_event_ids` and `local_only_event_ids`. It does not mutate the journal or trigger sends.
