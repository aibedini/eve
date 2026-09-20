# Feature Specification: V3 Client Normalization

**Feature Branch**: `main`

**Created**: 2026-09-20

**Status**: Draft

**Input**: Production memory evidence shows 52,372 materialized client rows for 11,766 unique clients (4.45x duplication), two full fleet copies in Web and Background, and roughly 500 MiB more retained by Background than Web. Correct telemetry, explain the unexplained retention, and normalize v3 clients without changing external dashboard behavior or combining later memory cleanups.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Trustworthy Memory Evidence (Priority: P1)

As an operator, I can trust the memory report and inspect a bounded background-fetch lifecycle so that I can distinguish transient allocation from accidentally retained state before deploying a representation change.

**Why this priority**: The current compressed-snapshot report returns zero for a populated snapshot, and the unexplained Background-Web gap could otherwise cause the wrong optimization to be selected.

**Independent Test**: Use a populated compressed snapshot and a controlled background fetch cycle; verify that reported block sizes are non-zero and lifecycle samples identify each checkpoint without exposing client values.

**Acceptance Scenarios**:

1. **Given** a compressed manifest referencing multiple populated server blocks, **When** memory telemetry is collected, **Then** the report returns the exact manifest, total block, and largest-block byte counts.
2. **Given** a background fetch cycle, **When** the cycle passes through fetch, processing, commit, publish, release, and settlement, **Then** a bounded aggregate sample exists for every reached checkpoint with PSS, USS, row counts, and bounded work counts.
3. **Given** a failed or partial fetch cycle, **When** diagnostics are inspected, **Then** completed checkpoints remain available and contain no credentials or client/account values.

---

### User Story 2 - Store One V3 Client Entity (Priority: P1)

As an operator of a large v3 fleet, I want each logical client retained once per server with small inbound membership references, so mirrored inbound assignments no longer multiply the full client object graph.

**Why this priority**: Production holds 40,606 duplicate rows per full copy, and both Web and Background retain a complete copy.

**Independent Test**: Build a server snapshot where one v3 UUID is assigned to four inbounds; verify that the canonical store has one client entity, four memberships, and the same externally observable values for every inbound.

**Acceptance Scenarios**:

1. **Given** one v3 client UUID assigned to four inbounds, **When** the snapshot is normalized, **Then** exactly one canonical client entity and four membership references are retained.
2. **Given** two legacy clients with the same email but distinct inbound semantics, **When** the snapshot is normalized, **Then** they remain distinct and retain their current behavior.
3. **Given** an unreliable or missing v3 UUID, **When** identity is resolved, **Then** an explicitly normalized email fallback is used only within the same server and never overrides a reliable UUID.

---

### User Story 3 - Preserve Existing Operations and Views (Priority: P1)

As an administrator or reseller, I can continue using dashboard, search, renew, edit, rotate, usage, online-state, subscription, and bulk operations with unchanged results after normalization.

**Why this priority**: A memory reduction is unacceptable if it changes customer-visible state or makes mutations scan the full fleet.

**Independent Test**: Run representative reads and mutations against equivalent legacy and normalized snapshots; verify identical visible results, correct membership invalidation, and work proportional to the changed client's memberships.

**Acceptance Scenarios**:

1. **Given** a normalized snapshot, **When** an existing API requests an inbound or server view, **Then** only the requested view is materialized and its response remains contract-compatible.
2. **Given** a shared v3 client mutation, **When** its canonical entity changes, **Then** every affected inbound view is invalidated and no unrelated inbound is rebuilt.
3. **Given** membership deletion from one inbound, **When** the client remains assigned elsewhere, **Then** only that membership is removed; account deletion removes the entity and all memberships.
4. **Given** an old-format or unsupported-format snapshot, **When** a worker reads it during a controlled rollout, **Then** it uses the documented compatibility behavior or rejects it safely without serving corrupted data.

### Edge Cases

- A v3 account has a stable UUID but different inbound-specific enablement or traffic metadata.
- Two v3 records share an email but have different reliable UUIDs.
- A legacy panel reuses an email across inbounds with different account semantics.
- A client entity changes while a snapshot publisher and a view reader operate concurrently.
- A membership refers to a missing entity, or a server block has an unknown schema version.
- A fetch fails between raw response, processing, commit, and publication.
- A settlement sample fires after a newer fetch cycle has begun.
- The operating system cannot provide proportional or private memory values.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The memory report MUST decode the snapshot manifest through the same canonical codec used to write it.
- **FR-002**: The memory report MUST return exact compressed manifest bytes, total server-block bytes, server-block count, and largest server-block bytes for a populated snapshot.
- **FR-003**: Background lifecycle diagnostics MUST record bounded aggregate samples for idle-before-fetch, after-panel-fetch, after-processing, after-snapshot-commit, after-publication, after-worker-result-release, and settled-after-cycle checkpoints.
- **FR-004**: Each diagnostic sample MUST include available PSS and USS, raw inbound and client-row counts, processed-row count, in-flight work count, and retained per-server result count when measurable.
- **FR-005**: Diagnostics MUST NOT continuously deep-walk the object graph or record credentials, account values, client identifiers, emails, URLs, or raw payloads.
- **FR-006**: Diagnostic history MUST be strictly bounded and must not become a material source of memory growth.
- **FR-007**: The system MUST use a versioned canonical normalized representation for v3 server snapshots.
- **FR-008**: Within a server, each reliable v3 client UUID MUST identify exactly one retained canonical client entity, with inbound assignments represented as memberships.
- **FR-009**: When a reliable v3 UUID is unavailable, the system MAY use an explicitly normalized email fallback scoped to the server; the fallback MUST NOT merge distinct reliable UUIDs.
- **FR-010**: Legacy clients MUST retain inbound-specific identity and semantics and MUST NOT be globally merged solely by email.
- **FR-011**: Account-global fields MUST be stored on the client entity; truly inbound-specific fields MUST be stored on the membership; derived display fields MUST remain outside canonical state unless required for semantic compatibility.
- **FR-012**: Compatibility accessors MUST support existing consumers without permanently retaining a fully expanded fleet.
- **FR-013**: Materialization MUST be limited to the requested server, inbound, delta, or client operation.
- **FR-014**: A client mutation MUST update one canonical entity and invalidate all and only the inbound views affected by that client's memberships.
- **FR-015**: Mutation work MUST be constant with respect to total fleet size and proportional only to the changed client's membership count.
- **FR-016**: Membership deletion and account deletion MUST remain distinct operations with independently testable outcomes.
- **FR-017**: Dashboard reads, search, ownership, usage/depletion telemetry, renew/edit/rotate, traffic and online state, reseller visibility, bulk operations, subscription/link generation, and Telegram lookup MUST preserve current externally observable behavior.
- **FR-018**: The per-server serialized block MUST declare its schema version and preserve independent per-server publication.
- **FR-019**: Old and unsupported schema versions MUST follow explicit, tested compatibility or safe-rejection behavior; no partially decoded snapshot may replace the last good state.
- **FR-020**: Delta invalidation MUST propagate from a changed client entity through its memberships without fingerprinting or materializing the complete fleet.
- **FR-021**: This change MUST NOT also remove `raw_client`, formatted fields, or full Web hydration except for a minimal compatibility adjustment that is required for normalization correctness.
- **FR-022**: The implementation MUST provide isolated before/after measurements for retained memory, serialized bytes, compressed bytes, publication peak, and hydration peak using production-shaped counts.
- **FR-023**: Existing public response shapes and administrator/reseller-visible behavior MUST remain compatible.

### Key Entities

- **Normalized Server Snapshot**: Versioned per-server canonical state containing client entities, inbound metadata, memberships, status, and revision information.
- **Client Entity**: One account-level v3 client state identified by server-scoped reliable UUID or explicit fallback key.
- **Inbound Membership**: The relationship between one client entity and one inbound, containing only inbound-specific state.
- **Materialized View**: A temporary compatibility projection for a requested server, inbound, delta, or operation; it is not a second retained source of truth.
- **Background Lifecycle Sample**: A bounded, PII-free aggregate observation for one fetch-cycle checkpoint.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A populated compressed snapshot report returns non-zero block counts and byte totals exactly matching the stored values.
- **SC-002**: A complete controlled fetch cycle records every required lifecycle checkpoint while retaining no more than the configured bounded sample count.
- **SC-003**: A v3 client mirrored across four inbounds is retained as one client entity plus four memberships, not four full client objects.
- **SC-004**: On the production-shaped fixture of 52,372 memberships and approximately 11,766 unique clients, normalization materially reduces retained memory and reports the measured reduction without treating the estimated 180 MiB floor as fact.
- **SC-005**: The normalized representation preserves all required read and mutation acceptance tests for v3 and legacy panels.
- **SC-006**: A single-client mutation performs work proportional to that client's memberships and does not scan the 52,372-row fleet.
- **SC-007**: Publication and hydration peak memory do not regress relative to the pre-normalization baseline.
- **SC-008**: Old/new snapshot compatibility tests prove that unsupported data cannot silently replace a valid in-memory snapshot.
- **SC-009**: Targeted Tier 1 tests pass during implementation and the affected Tier 2 suite passes once before handoff; Tier 3 is not run locally.

## Assumptions

- Production evidence from version 2.7.27 is the comparison baseline: 52,372 rows, approximately 11,766 unique clients, two full copies, and about 10.02 MiB compressed.
- The Background-Web memory difference is investigated and reported before normalization conclusions are finalized; a clear accidental retention bug is not silently folded into normalization.
- Existing per-server blocks, changed-server hints, and shared revision mechanisms remain available.
- No database schema change is expected; if implementation discovers one is necessary, planning must stop and add an Alembic-backed design before code changes.
- Deployment, production measurement, pushing, and combining the subsequent `raw_client`/formatted cleanup are outside this feature's implementation scope.
