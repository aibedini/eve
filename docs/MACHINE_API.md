# EVE Context API v1 (machine API)

The machine API is EVE's only non-session data surface (`/machine/v1`), consumed by
GOD AI. It answers versioned, masked projections; the one mutation it accepts is a
*pending proposal*, which EVE's operators review and decide themselves. The
contract is `docs/contracts/eve-context-api-v1.md` in the GOD AI repository
(section 6 covers proposals); this page documents the EVE implementation.

## Authentication

```http
Authorization: Bearer eve_mk_<id>.<secret>
X-Request-ID: <opaque id>          # echoed on every response
```

Keys are issued out of band, only their digest is stored, and each key carries an
explicit scope list:

```bash
python scripts/create_machine_api_key.py --name god-ai-phase6 \
    --scope proposals.read --scope proposals.write --created-by operator
```

Negative behaviour: missing/invalid/expired/revoked key -> `401`; a key without the
required scope -> `403` (`error: insufficient_scope`, plus `required_scope`).
`EVE_MACHINE_API_ENABLED=false` disables the whole blueprint (`404`).

## Scopes

| Scope | Grants |
|-------|--------|
| `context.customer.read` | customer projection and phone-hash lookup |
| `context.service.read` | service projection/lookup and SMS notification evidence |
| `context.renewal.read` | verified renewals |
| `context.payment.read_masked` | masked payment summaries |
| `context.support.read` | Telegram service requests |
| `context.knowledge.read` | FAQs, renewal and notification templates |
| `events.subscribe` | reserved for the signed event push |
| `proposals.read` | read the proposal queue (`GET /machine/v1/proposals`) |
| `proposals.write` | submit a proposal (`POST /machine/v1/proposals`) |

Reading and writing proposals are deliberately separate scopes: a key that may
submit suggestions cannot read the queue, and vice versa.

## Endpoints

| Method | Path | Scope |
|--------|------|-------|
| GET | `/machine/v1/health` | none |
| GET | `/machine/v1/customers/{customer_id}` | `context.customer.read` |
| GET | `/machine/v1/customers:lookup?phone_hash=` | `context.customer.read` |
| GET | `/machine/v1/customers/{customer_id}/services` | `context.service.read` |
| GET | `/machine/v1/services/{service_key}` | `context.service.read` |
| GET | `/machine/v1/services:lookup?client_email=` | `context.service.read` |
| GET | `/machine/v1/services/{service_key}/renewals` | `context.renewal.read` |
| GET | `/machine/v1/customers/{customer_id}/payments?since=` | `context.payment.read_masked` |
| GET | `/machine/v1/customers/{customer_id}/support-requests` | `context.support.read` |
| GET | `/machine/v1/ownership-claims?status=pending` | `context.customer.read` |
| GET | `/machine/v1/knowledge/faqs` | `context.knowledge.read` |
| GET | `/machine/v1/sms/notifications/{service_key}` | `context.service.read` |
| POST | `/machine/v1/proposals` | `proposals.write` |
| GET | `/machine/v1/proposals?status=pending&limit=` | `proposals.read` |

## Proposals

A proposal is a suggestion from GOD AI — a reminder text, a suspected duplicate
receipt, a Telegram reply draft. It is **not** an instruction: submitting one has
no side effect, and accepting one records a decision only.

```http
POST /machine/v1/proposals
{
  "proposal_id": "prop_01J...",        # required, unique, idempotency key
  "type": "reminder_text",             # required: reminder_text |
                                       #   duplicate_receipt_flag |
                                       #   telegram_reply_draft
  "payload": {"text": "..."},          # required JSON object, <= 8 KB
  "customer_id": 4182,                 # optional
  "service_key": "eve:12:5b1e...",     # optional
  "generation": 37                     # optional lifecycle generation
}
```

* `201` with `{proposal_id, status: "pending"}` when stored.
* `200` with the existing row when the same `proposal_id`, `type` and payload are
  replayed (idempotent retry after a timeout).
* `409` (`proposal_conflict`) when the same `proposal_id` carries a different type
  or payload; the stored proposal is never overwritten.
* `400` for an unknown type, a missing/invalid `proposal_id`, a payload larger than
  8 KB, or an invalid `service_key`/`generation`.

`GET /machine/v1/proposals` returns proposals newest first, default `status=pending`
and `limit=50` (bounded to 200). The masked projection is `proposal_id`, `type`,
`status`, `customer_id`, `service_key`, `generation`, the submitted `payload`, the
timestamps and a masked `decided_by`; internal ids and key material are never
included.

### The operator decision

The panel page `/proposals` (permission `proposals.review`) lists pending proposals
with customer, service, generation, type, creation time and a payload preview.
Accepting revalidates `generation` against the current
`service_lifecycle_states` row for the proposal's `service_key`:

* row missing or generation different -> the proposal is marked `expired` (reason
  `service_missing` / `generation_stale`) and the operator sees a conflict notice;
* generation current -> the proposal is marked `accepted`, `decided_by` and
  `decided_at` are recorded, and an audit entry (`proposal.accept`) is appended.

Rejecting requires a reason and records `proposal.reject` with that reason. Neither
accepting nor rejecting sends an SMS, renews a service or mutates a panel client —
`proposal.accept`, `proposal.expire` and `proposal.reject` are the only actions.
