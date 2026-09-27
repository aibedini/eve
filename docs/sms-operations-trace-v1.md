# SMS operations trace v1

Shared feature ID: `sms-operations-trace-v1` (GMweb spec `004-sms-operations-trace`).

GMweb is the source of truth for gateway acceptance and physical device submission.
EVE is the source of truth for eligibility, recipient decisions and notification debt.
Neither HTTP 200/202 nor `gateway.accepted` is an SMS send. `send.sent` indicates
gateway-recorded physical submission, not carrier delivery. No carrier-delivered
metric should be displayed until a separate receipt with provenance exists.

Set the same random secret of at least 32 characters as `EVE_SMS_EVENTS_SECRET` in
both processes. Configure GMweb `EVE_SMS_EVENTS_URL` to EVE's HTTPS
`/internal/gmweb/sms/events` endpoint. Do not use the legacy GMweb webhook secret.
The receiver checks `X-GMweb-Timestamp` within five minutes and verifies
`X-GMweb-Signature` as HMAC-SHA256 of
`<timestamp>.<X-GMweb-Delivery-Id>.<exact request body>` before parsing JSON.
It persists only bounded evidence fields (no SMS body or full phone) with a unique
event ID. Identical callbacks return a duplicate acknowledgement; contradictory
replays are rejected. GMweb retries non-2xx responses from its SQLite outbox.

The Android source now includes a durable carrier-report bridge, but this is not
production acceptance evidence. Carrier support and the app's delivery-report
setting vary, and cross-process callback recovery plus a real EVE/GMweb/Android
staging test remain unverified. The three sides must not be switched on
separately as a completed delivery-reporting feature.
