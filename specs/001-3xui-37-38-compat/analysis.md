# Specification Analysis Report

**Feature**: 001-3xui-37-38-compat
**Scope**: spec.md (45 FR, 9 SC, 7 user stories) × plan.md × tasks.md (54 tasks) × constitution
**Mode**: read-only

## Findings

| ID | Category | Severity | Location(s) | Summary | Recommendation |
|----|----------|----------|-------------|---------|----------------|
| C1 | Coverage gap | **CRITICAL** | spec.md FR-023 / tasks.md | FR-023 ("MUST NOT resubmit fields whose upstream contract forbids or makes write-only, and MUST NOT read back or echo secrets") has **zero** mapped task. It is a security requirement and it constrains exactly the code path T021–T025 touch (the new authoritative client read + payload echo). | Add a task in Phase 5 that scopes the preservation read/echo strictly to the client-record allowlist and excludes secret/write-only fields; assert it in T027/T041. |
| C2 | Coverage gap | MEDIUM | spec.md FR-036 / tasks.md | FR-036 ("version/compat resolution MUST NOT add a panel request per rendered page or client operation") has no dedicated task; it is only implied by T006 (cache) and T036 (sub paths). Not independently verifiable as written. | Add an explicit assertion task: after warm cache, a page render / client operation issues zero additional panel requests. |
| C3 | Product decision | **RESOLVED** | spec.md SC-009 / tasks.md T048, T053 / quickstart.md §7 | Real-panel 3.7.x acceptance is explicitly **WAIVED / NOT REQUIRED FOR RELEASE** and was not run. Required 3.7 automated/contract evidence remains release-blocking; the verdict is `PASS — automated/contract verified`. | No remaining blocker; do not claim real-panel 3.7 execution. |
| C4 | Terminology drift | LOW | research.md D6 vs spec.md FR-026 | research.md uses "panel-side automatic lifecycle settings"; spec.md/contracts use "panel-side lifecycle automation". Same concept. | Normalise to "panel-side lifecycle automation" in research.md D6. |
| C5 | Ambiguity | LOW | tasks.md T034 | "audit `subJsonPath` / `subClashPath`" does not state the acceptance condition (consume vs merely observe). | State: consume `subPath`; record presence/absence of the other two for the doctor surface. |
| C6 | Constitution alignment | LOW | plan.md Constitution Check, Principle V | The per-worker cache divergence window is documented as acceptable. The constitution permits process-local state for non-durable facts, so this is aligned — recorded here only because it is a judgement call a reviewer should see. | No change required; surfaced for reviewer awareness. |

## Coverage Summary

| Requirement | Has task? | Task IDs |
|---|---|---|
| FR-001 … FR-022 | yes | see table below |
| **FR-023** | **no** | — (see C1) |
| FR-024 … FR-035 | yes | T031–T041 |
| **FR-036** | partial | T006, T036 (see C2) |
| FR-037 … FR-044 | yes | T006–T007, T036, T041, T042–T044, T049 |
| FR-045 | n/a by design | explicitly "do not implement" — no task is correct |

Mapping (abbreviated): FR-001→T009/T014 · FR-002→T009/T011 · FR-003→T010 ·
FR-004→T003 · FR-005/006/007/008→T005/T012/T013 · FR-009→T001 · FR-010→T004 ·
FR-011→T014/T015 · FR-012/013→T016 · FR-014/015→T018/T042 · FR-016→T019 ·
FR-017→T023 · FR-018→T023/T024/T026 · FR-019/020→T021/T022 · FR-021→T025 ·
FR-022→T028 · FR-024→T031 · FR-025/026→T032 · FR-027→T033 · FR-028→T034/T035 ·
FR-029→T035 · FR-030/031→T036 · FR-032→T038 · FR-033→T039 · FR-034→T040 ·
FR-035→T041 · FR-037→T006 · FR-038→T007 · FR-039→T006 · FR-040→T036 ·
FR-041→T029 · FR-042→T030 · FR-043→T015/T030 · FR-044→T049

**Success criteria coverage**: SC-001→T027/T047 · SC-002→T012 · SC-003→T020 ·
SC-004→T028 · SC-005→T029 · SC-006→T037/T041 · SC-007→T006 (see C2) ·
SC-008→T046/T054 · SC-009→T047/T048/T053

**Unmapped tasks**: none.

## Constitution Alignment Issues

None. The plan's Constitution Check passes pre- and post-design. Principle II
(lifecycle generation) is the only principle with real exposure and FR-024–027
constrain it conservatively (detect and surface only; never write, never
translate into EVE events).

## Metrics

- Total requirements: 45 FR + 9 SC = 54
- Total tasks: 54
- Requirement coverage: 52/54 (96%) — gaps FR-023 (zero), FR-036 (partial)
- Ambiguity count: 2 (C5, and the C2 partial)
- Duplication count: 0
- Terminology drift: 1 (C4)
- Critical issues: 1 (C1)

## Next Actions

- **Resolve C1 before or during implementation.** FR-023 sits directly on the new
  preservation payload path; shipping T021–T025 without it would widen the
  secret/field surface of a security-relevant code path. Remediation: constrain
  the authoritative read and the payload echo to an explicit allowlist
  (`limitHwid` and the verified client-record fields only) and add the assertion
  to T027.
- C2 is low-cost and should be folded into T006's test.
- C3, C4, C5 are documentation-level and can be fixed inline.
- Concurrency: **no blocking issue for implementation beyond C1**; the artifact set
  is coherent enough to proceed.

## Remediation

Applied during implementation rather than as a separate edit pass:

- **C1** → added to T021/T022 acceptance and asserted in T027 (allowlist-only
  preservation; no secret or write-only field is read, echoed or resubmitted).
- **C2** → folded into T006 (cache hit ⇒ zero additional panel request).
- **C4/C5** → normalised terminology and made the acceptance condition explicit in
  the tasks that carry them.
- **C3** → superseded by product decision: T048/T053 are complete as explicit
  waivers, not unresolved blockers; quickstart §7 and SC-009 carry the final
  compatibility verdict and required release gates.
