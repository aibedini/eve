# Implementation Plan: GMweb Contract v5 Consumer

**Branch**: `feat/gmweb-contract-v5` | **Date**: 2026-09-28 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/003-gmweb-contract-v5/spec.md`

## Summary

Synchronize the exact GMweb PR #17 v5 contract, preserve stable EVE notification identity on outbound sends, extend the immutable signed-event journal with v5 correlation fields, derive monotonic submission/carrier projections, add a bounded optional delivery-event search client, and expose truthful carrier and optional health diagnostics in SMS Operations. EVE is deployed first and feature-detects v5-only reads so GMweb v4 production continues to send and report existing health during the rollout window.

## Technical Context

**Language/Version**: Python 3.11+; browser JavaScript supported by the current Eve panel
**Primary Dependencies**: Flask, SQLAlchemy, Alembic, requests; existing single-file Eve CSS system
**Storage**: PostgreSQL production and SQLite tests; append-only `sms_gateway_events` plus existing SMS send/outbox tables
**Testing**: unittest/pytest repository suite, Alembic migration harness, contract drift checks, UI audit, release check
**Target Platform**: Linux web/worker deployment; modern browser operations panel
**Project Type**: Modular Flask web service with background workers and server-rendered operations UI
**Performance Goals**: Delivery-event search is operator-triggered, capped at 100 provider rows, and never added to the normal send path
**Constraints**: Signed callbacks remain canonical; no PII/body/credentials in evidence; no automatic carrier-failure retry; consumer-first v4 compatibility; one Alembic head
**Scale/Scope**: Existing SMS traffic and journal volume; timeline reads remain bounded to 500 local events and provider diagnostics to 100 rows

## Constitution Check

*GATE: Passed before Phase 0 and re-checked after Phase 1 design.*

- **I Canonical Service Identity**: Existing service keys remain unchanged; notification IDs serialize existing event identity rather than introduce a new service identity.
- **II Lifecycle Generation**: Outbound v5 metadata keeps the existing durable generation and retry identity.
- **V Cross-Process Correctness**: Correlation/evidence is database-backed; no process-local truth is introduced.
- **VI Notification Delivery**: Existing durable notification event ID becomes `eveNotificationId`; retry reuses it; callback journal remains append-only and idempotent.
- **VII Renewal Safety**: Contract synchronization preserves invalidation and does not weaken the generation barrier.
- **VIII Security**: Read client uses validated base URL, bearer header, bounded filters/response, no secret/PII logging.
- **X Database Evolution**: One Alembic revision from the existing head adds bounded nullable evidence fields and query-driven indexes; forward recovery is documented.
- **XI Observability**: Submission, carrier, current readiness, historical carrier reports, and callback-outbox state remain distinct.
- **XII Testing**: Contract, callback ordering, client failure, migration, integration, UI, and full-suite coverage are required.
- **XIII Production Acceptance**: Final report explicitly lists real Android/SIM/carrier/staging/production as not verified.
- **Cross-Repository Contract**: Shared feature ID is `gmweb-carrier-dlr-v5`; exact provider commit and compatibility matrix are recorded.
- **Version discipline**: `APP_VERSION` and `CHANGELOG.md` receive the same patch release.

No constitution violation is required.

## Project Structure

### Documentation (this feature)

```text
specs/003-gmweb-contract-v5/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── sms-evidence-v5.md
└── tasks.md
```

### Source Code (repository root)

```text
shared/eve-gmweb-contract-v1.json
panel/models/ops.py
panel/services/gmweb_contract.py
panel/services/gmweb_transport_probe.py
panel/jobs/messaging.py
panel/routes/messaging.py
panel/routes/sms_gateway_events.py
alembic/versions/<new>_gmweb_contract_v5_evidence.py
static/sms-center.js
templates/sms_center.html
static/style.css
docs/GMWEB_CONTRACT.md
docs/GMWEB_SMS_CENTER_REQUIREMENTS.md
docs/SMS_GATEWAY_DELIVERY_SPEC.md
docs/operations/GMweb_CONTRACT_V5_ROLLOUT.md
docs/README.md
CHANGELOG.md
app.py
tests/test_gmweb_contract.py
tests/test_sms_gateway_events.py
tests/test_transport_health_route.py
tests/test_gmweb_transport_probe.py
tests/test_alembic_gmweb_contract_v5.py
tests/test_sms_meta_generation.py
```

**Structure Decision**: Extend the existing model/service/route/job ownership boundaries. The shared JSON remains the wire source of truth; the existing signed callback blueprint remains the sole ingress; the existing SMS Center remains the operations UI.

## Design Decisions

1. `ServiceNotificationEvent.event_id` is the preferred EVE notification identity because it is stable, opaque, retry-safe, ASCII, and already unique. Other send flows use a deterministic bounded identifier derived from their existing idempotency/correlation identity only when a durable event ID exists.
2. Callback journal rows gain nullable `request_id`, `gateway_request_id`, `carrier_status`, and `evidence` fields; `event_id` remains the sole primary identity and raw callback content is never persisted.
3. Submission and carrier projections are pure functions over the immutable ordered journal. Carrier evidence strength prevents a delayed `send.*` event from downgrading a receipt.
4. The GMweb delivery-event search client validates filters, caps `limit` at 100, projects only contract-declared safe fields, and treats 404/405/501 as a v4 capability absence.
5. Controlled reconciliation compares remote event IDs with local journal IDs and reports discrepancies; it does not synthesize signed local events or poll in the delivery path.
6. Optional health diagnostics are allowlisted and bounded. Historical dead-letter counts are informational and never alter the current `gmweb.ready` verdict.
7. Existing `secrets.manage` remains for this minimal adoption; granular SMS operation permissions are documented as deferred rather than broadening scope.

## Compatibility Matrix

| EVE | GMweb | Behaviour |
|-----|-------|-----------|
| v5 consumer | v4 provider | Existing send/status/invalidation/health/callback flows work; delivery-event search reports capability unavailable; optional diagnostics absent. |
| v5 consumer | v5 provider | Stable notification identity, carrier events, v5 search, gateway request correlation, and optional diagnostics are consumed. |
| v4 consumer | v5 provider | Not the rollout target; provider contract retains legacy fields but drift gate intentionally prevents this merge order. |

## Migration and Recovery

- Upgrade adds nullable columns and bounded indexes only; existing event rows remain valid and project to carrier `unavailable` unless their event type proves otherwise.
- Forward recovery: fix application/migration and re-run the idempotent migration runner. No data backfill is required.
- Downgrade: application rollback can ignore additive nullable columns; destructive column removal is intentionally not performed during emergency rollback.
- Single-head assertion is included in the migration test.

## Complexity Tracking

No justified constitution violations.
