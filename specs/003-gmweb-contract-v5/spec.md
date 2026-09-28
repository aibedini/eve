# Feature Specification: GMweb Contract v5 Consumer

**Feature Branch**: `feat/gmweb-contract-v5`
**Created**: 2026-09-28
**Status**: Implemented
**Input**: Adopt the exact GMweb PR #17 contract v5 as a backward-compatible EVE consumer.

## User Scenarios & Testing

### User Story 1 - Trustworthy carrier evidence (Priority: P1)

As an SMS operator, I can distinguish gateway acceptance and Android submission from a carrier delivery receipt so I never mistake an accepted or submitted message for a delivered SMS.

**Independent Test**: Ingest accepted, submitted, delivered, failed, duplicate, conflicting, and reversed-order signed events and verify the immutable timeline and projections.

**Acceptance Scenarios**:

1. **Given** a signed `send.sent` event, **When** its timeline is read, **Then** submission is confirmed as sent and carrier delivery remains pending or unavailable.
2. **Given** a signed `sms.delivered` event, **When** its timeline is read, **Then** carrier delivery is confirmed with carrier evidence.
3. **Given** a signed `sms.delivery_failed` event, **When** its timeline is read, **Then** carrier failure is confirmed without creating an automatic retry.
4. **Given** a delivered event arrives before a delayed send event, **When** both are projected, **Then** carrier state remains delivered.
5. **Given** no carrier receipt exists, **When** the timeline is read, **Then** carrier state is unavailable or pending and never inferred as failed.

---

### User Story 2 - End-to-end notification correlation (Priority: P1)

As an investigator, I can correlate one logical EVE notification through GMweb and Android without storing recipient data or message bodies in the evidence journal.

**Independent Test**: Send a logical notification with retries and verify the stable EVE identity and all bounded correlation fields are retained across outbound metadata and signed evidence.

**Acceptance Scenarios**:

1. **Given** an EVE notification has a durable identity, **When** it is sent or retried, **Then** the same opaque `eveNotificationId` is supplied every time.
2. **Given** a v5 event supplies request, gateway request, carrier event, message, trace, and notification identifiers, **When** EVE persists it, **Then** the identifiers are queryable and no phone number, message body, or credential is stored.
3. **Given** a duplicate event ID and identical payload, **When** the callback is replayed, **Then** it is accepted as a duplicate and only one durable event remains.
4. **Given** a duplicate event ID with a different payload, **When** the callback is replayed, **Then** EVE rejects the conflict.

---

### User Story 3 - Bounded delivery-event diagnostics (Priority: P2)

As an operator, I can search GMweb's bounded delivery-event read model and compare it with EVE's signed evidence to investigate missed callbacks without replacing callbacks with polling.

**Independent Test**: Exercise the client with all supported filters, a capped limit, scope refusal, timeout, server error, and malformed response while preserving existing EVE evidence.

**Acceptance Scenarios**:

1. **Given** GMweb v5 is available, **When** an authorized bounded search is made, **Then** EVE returns only the declared, privacy-safe operational fields.
2. **Given** GMweb v4 lacks the endpoint, **When** EVE probes it during the rollout window, **Then** the feature reports unavailable without breaking sends, callbacks, or stored timelines.
3. **Given** GMweb has an event missing from EVE, **When** controlled reconciliation is requested, **Then** the discrepancy is reported for repair and normal delivery remains callback-driven.

---

### User Story 4 - Actionable transport diagnostics (Priority: P2)

As an SMS operator, I can inspect optional carrier-report and callback-outbox diagnostics without historical failures making current transport health falsely unhealthy.

**Independent Test**: Parse v4-compatible health payloads and v5 payloads with optional diagnostics, unknown keys, bounded counts, and historical dead letters.

**Acceptance Scenarios**:

1. **Given** optional diagnostics are absent, **When** health is loaded, **Then** the existing health view continues to work.
2. **Given** optional diagnostics are present, **When** health is loaded, **Then** bounded carrier and callback-outbox facts are visible separately from current readiness.
3. **Given** a historical dead letter but healthy current transport, **When** health is evaluated, **Then** current transport is not automatically marked unhealthy.

### Edge Cases

- Events can arrive late, duplicated, or in callback order different from occurrence order.
- A stronger carrier-delivered fact must not be downgraded by weaker or older evidence.
- Missing DLR capability or evidence is not a delivery failure.
- GMweb v4 may return 404 for the v5 search endpoint during the consumer-first rollout.
- Optional diagnostics and unknown future keys must not invalidate an otherwise valid health response.
- Search results and callbacks must reject or omit forbidden recipient, text, credential, and callback-body fields.

## Requirements

### Functional Requirements

- **FR-001**: EVE MUST publish a byte-identical copy of GMweb PR #17 commit `2ba7ec0837b248ddc4e6e8c84ab90f5c3ee5af0c` contract v5.
- **FR-002**: EVE MUST expose a bounded, read-only consumer for delivery-event search with the declared filters and a maximum limit of 100.
- **FR-003**: Signed callbacks MUST remain the canonical push/evidence authority; search MUST be diagnostic and controlled, not continuous polling.
- **FR-004**: Outbound sends MUST include a stable opaque EVE notification identity when one exists, and retries MUST reuse it.
- **FR-005**: The EVE notification identity MUST match `^[A-Za-z][A-Za-z0-9_-]{0,119}$` and MUST NOT encode recipient data, SMS text, or credentials.
- **FR-006**: Evidence MUST correlate trace/correlation, message, GMweb request, gateway request, carrier event, EVE notification, and run identity when supplied.
- **FR-007**: Submission state and carrier state MUST be projected independently from the immutable event journal.
- **FR-008**: Only authenticated `sms.delivered` or explicit carrier status `delivered` is positive carrier-delivery evidence.
- **FR-009**: Only authenticated `sms.delivery_failed` or explicit carrier status `failed` is carrier-failure evidence.
- **FR-010**: Acceptance, HTTP success, queue state, Android connection, `gateway.accepted`, and `send.sent` MUST NOT imply carrier delivery.
- **FR-011**: Missing carrier evidence MUST remain unavailable or pending, never failed.
- **FR-012**: Event projection MUST use occurrence time, evidence strength, and event identity so reversed arrival never downgrades delivered state.
- **FR-013**: Existing callback authentication, replay window, delivery ID validation, idempotency, conflict rejection, body bound, and PII exclusion MUST remain intact.
- **FR-014**: Message timeline output MUST separately describe submission and carrier state, confirmation, and evidence.
- **FR-015**: Transport health parsing MUST tolerate absent and unknown optional diagnostics and expose bounded relevant carrier-report and callback-outbox facts separately from readiness.
- **FR-016**: Carrier failure MUST be persisted and displayed without introducing automatic retry policy.
- **FR-017**: EVE v5 MUST remain compatible with GMweb v4 during the consumer-first rollout; unavailable v5-only reads MUST degrade safely.
- **FR-018**: Schema changes MUST use one Alembic revision, preserve one migration head, and document forward recovery.
- **FR-019**: SMS operational evidence MUST contain no phone number, SMS body, credential, or raw callback body.
- **FR-020**: The operations UI MUST use the existing Eve design system and represent carrier state with text as well as color.

### Key Entities

- **SMS Gateway Event**: Immutable signed evidence with event identity, occurrence time, type, bounded correlation identifiers, and privacy-safe diagnostics.
- **Submission Projection**: Derived gateway/device submission state and evidence strength for a logical send.
- **Carrier Projection**: Derived receipt state (`unavailable`, `pending`, `delivered`, `failed`) whose terminal receipt facts cannot be downgraded by weaker evidence.
- **EVE Notification Identity**: Stable opaque public identifier for one logical notification across retries.
- **Delivery Event Search Result**: Bounded, project-isolated GMweb operational read model used only for diagnostics/reconciliation.

## Success Criteria

### Measurable Outcomes

- **SC-001**: All contract drift checks report exact equality with the named GMweb peer commit.
- **SC-002**: 100% of tested submitted-only messages are reported as not carrier-confirmed.
- **SC-003**: 100% of normal, late, duplicated, and reversed-order carrier scenarios converge to the expected carrier state.
- **SC-004**: All supported diagnostic searches enforce a maximum of 100 results and fail safely without modifying stored evidence.
- **SC-005**: Static and runtime privacy checks find zero phone numbers, message bodies, credentials, or raw callback bodies in operational evidence.
- **SC-006**: EVE's complete automated test suite, migration tests, contract tests, SMS lifecycle tests, UI audit, and release checks pass before review.
- **SC-007**: During a v4-provider/v5-consumer rollout simulation, existing sends, status checks, callbacks, and transport-health views continue to operate.

## Assumptions

- GMweb PR #17 commit `2ba7ec0837b248ddc4e6e8c84ab90f5c3ee5af0c` is the provider contract authority.
- Existing callback keys and signature construction remain unchanged.
- Carrier-failure retry policy is intentionally deferred; this change records and displays failure only.
- The current SMS operations permission remains for the minimal v5 adoption; permission decomposition is deferred and documented.

## Out of Scope

- Merging GMweb PR #17 or bypassing either repository's branch protection.
- Continuous polling that replaces signed callbacks.
- Automatic retry after carrier failure.
- Claiming real Android, SIM, carrier, staging, or production verification from automated tests.
