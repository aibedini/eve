# Tasks: V3 Client Normalization

**Input**: Design documents from `specs/002-v3-client-normalization/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

## Phase 1: Setup

- [X] T001 Verify repository ignore files already cover Python, environment, local graph, and benchmark artifacts in `.gitignore` and `.dockerignore`
- [X] T002 Record the changed-path Tier 1 test mapping for the planned modules in `scripts/affected_tests.py`

## Phase 2: Foundational

- [X] T003 [P] Add compressed-manifest and multi-block byte-count regression tests in `tests/test_memory_report.py`
- [X] T004 [P] Add bounded, ordered, PII-free lifecycle-probe tests in `tests/test_memory_probe.py`
- [X] T005 [P] Add normalized identity, legacy isolation, membership, materialization, and deletion tests in `tests/test_snapshot_model.py`
- [X] T006 [P] Add legacy/v2/unknown Redis block round-trip tests in `tests/test_snapshot_redis.py`

## Phase 3: User Story 1 - Trustworthy Memory Evidence (Priority: P1)

**Goal**: Correct Redis size telemetry and expose bounded background lifecycle evidence.

**Independent Test**: A compressed multi-block fixture reports exact sizes and one controlled cycle reports every bounded checkpoint without PII.

- [X] T007 [US1] Decode the compressed manifest through the canonical codec in `panel/core/memory_report.py`
- [X] T008 [US1] Implement fixed-size aggregate lifecycle sampling and safe PSS/USS fallback in `panel/core/memory_probe.py`
- [X] T009 [US1] Expose lifecycle samples in the existing memory report without changing unavailable payload shape in `panel/core/memory_report.py`
- [X] T010 [US1] Instrument fetch, process, commit, publish, release, and settled checkpoints without retaining payloads in `panel/jobs/schedulers.py` and `panel/jobs/refresh.py`
- [X] T011 [US1] Run affected Tier 1 tests for `panel/core/memory_report.py`, `panel/core/memory_probe.py`, `panel/jobs/schedulers.py`, and `panel/jobs/refresh.py`

## Phase 4: User Story 2 - Store One V3 Client Entity (Priority: P1)

**Goal**: Retain one canonical v3 client entity with inbound memberships while preserving legacy blocks.

**Independent Test**: One UUID mirrored on four inbounds produces one entity/four refs; legacy same-email rows remain distinct.

- [X] T012 [US2] Implement schema-v2 normalization, reliable identity rules, membership overlays, indexes, iterators, and bounded materializers in `panel/core/snapshot_model.py`
- [X] T013 [US2] Normalize processed v3 blocks before retained commit while leaving legacy blocks expanded in `panel/jobs/schedulers.py` and `panel/jobs/refresh.py`
- [X] T014 [US2] Publish schema-v2 per-server blocks and load legacy/v2 blocks with last-good fallback for unknown versions in `panel/core/redis_client.py`
- [X] T015 [US2] Count normalized entities and memberships accurately from the shared retained graph in `panel/core/memory_report.py`
- [X] T016 [US2] Add old/new retained, JSON, compressed, publish-peak, and hydrate-peak measurements in `scripts/measure_snapshot_footprint.py`
- [X] T017 [US2] Run affected Tier 1 tests for the normalized model, Redis round trip, schedulers, refresh, and measurements

## Phase 5: User Story 3 - Preserve Operations and Views (Priority: P1)

**Goal**: Preserve all existing reads and mutations while keeping the normalized graph as the only retained source of truth.

**Independent Test**: Existing full/delta responses and client operations match legacy behavior, and a mutation touches only the changed entity's memberships.

- [X] T018 [P] [US3] Add dependency-aware delta and requested-view materialization tests in `tests/test_snapshot_delta.py` and `tests/test_snapshot_model.py`
- [X] T019 [P] [US3] Validate create/patch/remove/renew/rotate/traffic/online mutations against the shared-reference compatibility graph with affected tests
- [X] T020 [P] [US3] Validate reseller visibility, ownership, subscription/link generation, search, bulk work, telemetry, and Telegram lookup through the existing regression matrix
- [X] T021 [US3] Make a shared entity mutation fingerprint every dependent inbound and materialize only requested dashboard inbounds
- [X] T022 [US3] Keep cached mutation helpers and server-stat recomputation on the shared-reference compatibility graph in `panel/jobs/refresh.py`
- [X] T023 [US3] Materialize normalized dashboard full/delta responses in `panel/routes/dashboard.py`; retain existing route readers on the compatibility graph
- [X] T024 [US3] Validate ownership, subscription, billing, lifecycle, depletion, messaging, and Telegram readers on the compatibility graph without broad rewrites
- [X] T025 [US3] Preserve remaining `app.py` cached readers and compatibility exports on the shared graph
- [X] T026 [US3] Verify hot internal consumers retain membership lists of shared entity references rather than expanded copies
- [X] T027 [US3] Run affected Tier 1 tests for consumers and mutation paths

## Phase 6: Polish & Cross-Cutting Validation

- [X] T028 Update `APP_VERSION` and `CHANGELOG.md` for the isolated behavior change in `app.py` and `CHANGELOG.md`
- [X] T029 Run `scripts/measure_snapshot_footprint.py --json` and record measured old/new results in `docs/performance/MEMORY.md` while clearly separating estimates from measurements
- [X] T030 Run Tier 2 once for every changed application path; do not run Tier 3/full suite
- [X] T031 Re-run Codebase Memory `detect_changes`, verify material path coverage, and review the final diff for unrelated changes
- [X] T032 [US1] Complete lifecycle raw/processed/in-flight/retained aggregate counts and test their PII-safe cross-process report
- [X] T033 [US3] Preserve shared identity in add/clone mutations and restrict delta fingerprinting to affected membership inbounds

## Dependencies & Execution Order

- Phase 2 tests depend on Phase 1 mapping verification and must fail before implementation.
- US1 is completed first because trustworthy telemetry is a gate for memory claims.
- US2 depends on US1 and foundational schema tests.
- US3 depends on the US2 model and Redis format.
- Polish depends on all user stories.

## Parallel Opportunities

- T003-T006 touch independent test files.
- T018-T020 touch independent test files after US2.
- Consumer migration is sequential where shared helpers or `refresh.py` overlap.

## Implementation Strategy

1. Deliver telemetry correction and bounded probe as an independently testable slice.
2. Deliver normalized storage/Redis with unit round trips.
3. Migrate reads and mutations, keeping external payloads unchanged.
4. Measure the isolated normalization, then run Tier 2 once.
