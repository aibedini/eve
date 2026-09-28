# Tasks: GMweb Contract v5 Consumer

**Input**: Design documents from `specs/003-gmweb-contract-v5/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

## Phase 1: Setup (Shared Contract)

- [X] T001 Synchronize `shared/eve-gmweb-contract-v1.json` byte-for-byte from GMweb commit `2ba7ec0837b248ddc4e6e8c84ab90f5c3ee5af0c`
- [X] T002 [P] Extend v5 contract assertions and exact peer comparison in `tests/test_gmweb_contract.py`
- [X] T003 Bump the patch version in `app.py` and add the matching entry in `CHANGELOG.md`

## Phase 2: Foundational (Evidence Model and Client Primitives)

- [X] T004 [P] Add failing migration/schema/index tests for nullable `request_id` max 120, `gateway_request_id` max 120, `carrier_status`, `evidence`, and indexed `occurred_at` in `tests/test_alembic_gmweb_contract_v5.py`
- [X] T005 Add nullable bounded v5 evidence fields and query-driven indexes to `SmsGatewayEvent` in `panel/models/ops.py`
- [X] T006 Add a single-head additive evidence migration in `alembic/versions/*_gmweb_contract_v5_evidence.py`
- [X] T007 Add v5 contract accessors, filter validation, safe event projection, and bounded read client primitives in `panel/services/gmweb_contract.py`

## Phase 3: User Story 1 - Trustworthy Carrier Evidence (Priority: P1) MVP

**Goal**: Keep submission and carrier truth separate across normal, missing, late, duplicate, and reversed evidence.

**Independent Test**: Signed callback sequences converge to truthful projections without automatic retry.

- [X] T008 [P] [US1] Add failing callback/projection tests for normal, failure, late, reversed, and no-DLR sequences in `tests/test_sms_gateway_events.py`
- [X] T009 [US1] Extend bounded signed callback parsing with v5 correlation/carrier fields in `panel/routes/sms_gateway_events.py`
- [X] T010 [US1] Implement pure monotonic submission/carrier projection in `panel/routes/sms_gateway_events.py`
- [X] T011 [US1] Return separate `submission` and `carrier` blocks from the timeline API in `panel/routes/sms_gateway_events.py`
- [X] T012 [US1] Extend SMS overview with only truthfully calculable carrier pending/unavailable values in `panel/routes/sms_gateway_events.py`
- [X] T013 [US1] Render separate submission and carrier states with existing badge semantics in `static/sms-center.js` and `static/style.css`

## Phase 4: User Story 2 - End-to-End Notification Correlation (Priority: P1)

**Goal**: Preserve one privacy-safe EVE identity across sends/retries and expose the full bounded correlation chain.

**Independent Test**: Repeated send attempts reuse the same EVE notification ID and callback evidence correlates without PII.

- [X] T014 [P] [US2] Add failing stable identity and privacy tests in `tests/test_sms_meta_generation.py` and `tests/test_sms_gateway_events.py`
- [X] T015 [US2] Accept and validate `eveNotificationId` in notification metadata in `panel/services/lifecycle.py` and `panel/jobs/messaging.py`
- [X] T016 [US2] Populate stable `eveNotificationId` from existing durable notification IDs at send call sites in `panel/jobs/messaging.py`
- [X] T017 [US2] Expose bounded notification/request/gateway correlation fields in gateway-event and timeline APIs in `panel/routes/sms_gateway_events.py`

## Phase 5: User Story 3 - Bounded Delivery-Event Diagnostics (Priority: P2)

**Goal**: Search and compare GMweb's privacy-safe read model without replacing signed callbacks.

**Independent Test**: All filters, cap, provider errors, v4 absence, malformed payload, isolation assumptions, and no-mutation behavior pass with mocked HTTP.

- [X] T018 [P] [US3] Add failing delivery-event client tests for auth, filters, limit, v4 absence, timeout, 5xx, malformed response, forbidden data, and no local mutation in `tests/test_gmweb_delivery_events.py`
- [X] T019 [US3] Implement bounded GET client and v4 capability detection in `panel/services/gmweb_contract.py`
- [X] T020 [US3] Add read-only SMS Operations search/reconciliation API in `panel/routes/messaging.py`
- [X] T021 [US3] Add on-demand reconciliation controls/results to `templates/sms_center.html`, `static/sms-center.js`, and `static/style.css`

## Phase 6: User Story 4 - Actionable Transport Diagnostics (Priority: P2)

**Goal**: Safely expose optional carrier and callback-outbox diagnostics without conflating history with current readiness.

**Independent Test**: v4 and v5 health payloads parse safely, unknown keys are ignored, and historical dead letters do not change readiness.

- [X] T022 [P] [US4] Add failing optional diagnostic projection/route tests in `tests/test_gmweb_transport_probe.py` and `tests/test_transport_health_route.py`
- [X] T023 [US4] Project bounded `androidActivity`, `carrierReports`, and `callbackOutbox` sections in `panel/services/gmweb_transport_probe.py`
- [X] T024 [US4] Expose optional diagnostics separately in the transport-health route in `panel/routes/messaging.py`
- [X] T025 [US4] Render optional diagnostics as neutral historical facts in `static/sms-center.js` and `static/style.css`

## Phase 7: Documentation and Cross-Cutting Validation

- [X] T026 [P] Update contract v5 and carrier semantics in `docs/GMWEB_CONTRACT.md`, `docs/GMWEB_SMS_CENTER_REQUIREMENTS.md`, and `docs/SMS_GATEWAY_DELIVERY_SPEC.md`
- [X] T027 [P] Add rollout/migration/forward-recovery/deferred-permission guidance in `docs/operations/GMWEB_CONTRACT_V5_ROLLOUT.md` and link it from `docs/README.md`
- [X] T028 Update invariant-to-test mapping in `docs/TELEMETRY_STATE_TRANSITIONS.md`
- [X] T029 Run targeted contract, callback, ordering, client, migration, SMS notification/debt, and SMS Operations tests
- [X] T030 Run UI audit, UI guard, documentation index, release check, syntax/static checks, and affected Tier 2 tests
- [X] T031 Run the complete EVE test suite and record passed/failed/skipped/duration evidence
- [X] T032 Verify byte equality against exact GMweb commit, inspect privacy-sensitive diffs, and confirm v4 rollout compatibility

## Dependencies & Execution Order

- Phase 1 establishes the canonical contract.
- Phase 2 blocks all stories because callbacks and clients depend on the evidence model and contract helpers.
- US1 and US2 are both P1; US2 reuses the US1 journal/API extensions.
- US3 and US4 are independent after Phase 2 but integrate into the same SMS Operations surface sequentially.
- Documentation and full validation follow all implementation stories.

## Parallel Opportunities

- T002 and T003 can proceed after T001 without touching the same files.
- Each story's first test task can be prepared independently.
- T026 and T027 are parallel documentation tasks in distinct files.

## Implementation Strategy

The MVP is Phases 1-4: exact contract, evidence schema, truthful carrier projection, and stable notification identity. Delivery-event diagnostics and optional health diagnostics follow without altering the normal delivery path. The final gate is full-suite and exact cross-repository drift verification.
