# Tasks: 3x-ui 3.7.x / 3.8.x Version-Gated Compatibility

**Feature**: 001-3xui-37-38-compat | **Branch**: `feat/3xui-37-38-compat`
**Input**: [spec.md](./spec.md), [plan.md](./plan.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

Tests are included because the specification and the mission require provable
compatibility evidence (SC-001 … SC-009).

## Phase 1: Setup

- [x] T001 Create the compatibility service module `panel/services/xui_compat.py` with module docstring, imports and the `PanelVersion` / `PanelCompatibilityProfile` / `PanelCompatibility` dataclasses per `data-model.md`
- [x] T002 Create the contract fixture directory `tests/fixtures/xui/` and record the audited upstream refs (`v3.7.0=f727d04f6522bb94a8fb52e8352fdcafb51c11e1`, `v3.8.0=837addf66e945a80080273b5d2a315dea765d748`) in `tests/fixtures/xui/README.md`

## Phase 2: Foundational (blocks every user story)

- [x] T003 [P] Implement `normalize_version(raw)` in `panel/services/xui_compat.py` — optional leading `v`, 2- or 3-component, strip build suffixes, reject `dev+`/garbage/empty; never string-compare (FR-004, D2)
- [x] T004 [P] Implement the profile table `PROFILES` in `panel/services/xui_compat.py` with the exact flag values from `data-model.md` (baseline_v3 / xui_3_7 / xui_3_8); do **not** add a `persistent_keepalive` flag (FR-010, D2)
- [x] T005 Implement `select_profile(version)` in `panel/services/xui_compat.py` as a whitelist over `(major, minor)` with baseline fallback (FR-005, FR-006, FR-007, G1–G7)
- [x] T006 Implement the per-server compatibility cache with bounded TTL and `invalidate_compatibility(server_id)` in `panel/services/xui_compat.py`, mirroring the existing `XUI_CAPABILITY_CACHE` pattern (FR-037, D5)
- [x] T007 Extend `invalidate_xui_caches()` in `panel/adapters/xui.py` to also clear the compatibility cache (FR-038)
- [x] T008 Re-export the new compatibility surface from `app.py` so existing `from app import X` callers and tests keep working (repo convention)

## Phase 3: US3 — Version-specific behaviour must not leak (P1)

Goal: family-based profile selection that provably never promotes an uncertified version.
Independent test: drive `select_profile` across the version matrix in `quickstart.md` §4.

- [x] T009 [US3] Implement `detect_panel_compatibility(server, session, *, force=False)` in `panel/services/xui_compat.py` reading `panelVersion` from the `/panel/api/server/status` payload (S1, authoritative) (FR-002)
- [x] T010 [US3] Add the corroborating `getPanelUpdateInfo` → `obj.currentVersion` path; its failure MUST NOT degrade an S1-resolved version (FR-003, S2)
- [x] T011 [US3] Ensure `panel/adapters/xui.py:_normalize_server_status_payload` surfaces `panelVersion` as the canonical version input while preserving the existing `xui_version` key (FR-002, FR-041)
- [x] T012 [P] [US3] Unit tests for normalisation + selection + future/unknown versions in `tests/test_3xui_compat.py` (SC-002, SC-003 baseline)
- [x] T013 [US3] Emit named warnings `panel_version_unknown` and `future_version_uncertified` with certification state `unverified` / `certification_required` (FR-007, FR-008)

## Phase 4: US2 — A modern panel must never be mistaken for legacy (P1)

Goal: typed probe outcome; 401/403 never downgrade capability.
Independent test: `quickstart.md` §5 auth matrix.

- [x] T014 [US2] Introduce the typed probe outcome (`SUPPORTED | ROUTE_MISSING | AUTH_INVALID | SCOPE_INSUFFICIENT | TRANSPORT_ERROR | INVALID_RESPONSE`) in `panel/adapters/xui.py` (FR-011, D4)
- [x] T015 [US2] Rework `_probe_v3_client_api` in `panel/adapters/xui.py` to return the typed outcome instead of a bare bool, and to keep returning a bool view for existing callers (FR-011, FR-043)
- [x] T016 [US2] Stop caching `v3_clients = False` for 401/403: only a definitive route-missing or a definitive success may write the capability cache (FR-012, FR-013, N4)
- [x] T017 [US2] Send `X-Requested-With: XMLHttpRequest` on the probe **only** for the `xui_3_7` profile, where it is the only way to distinguish 401 from 404 (D4, gating table)
- [x] T018 [US2] Surface `api_auth_invalid` / `api_token_scope_insufficient` as actionable operator messages; never attempt scope escalation (FR-014, FR-015)
- [x] T019 [US2] Invalidate cached capability/compat state on a detected authentication failure (FR-016)
- [x] T020 [P] [US2] Auth-matrix tests (admin / monitor / node-sync / expired / rotated / invalid) asserting none reports "legacy" — extend `tests/test_3xui_compat.py` (SC-003)

## Phase 5: US1 — Renewing must not destroy the device limit (P1, P0 defect)

Goal: persisted `limitHwid` survives every unrelated mutation.
Independent test: the automated preservation matrix for both profiles, plus
`quickstart.md` §3 persisted-state proof against the controlled real 3.8.x panel.

- [x] T021 [US1] Implement `read_authoritative_client_settings(server, session, email)` in `panel/adapters/xui.py` using `GET /clients/get/{email}` → `obj.client.limitHwid`, returning `None` when the panel does not expose the field (FR-019, FR-020, D3)
- [x] T022 [US1] Extend `_v3_client_payload` in `panel/adapters/xui.py` to accept and inject a preserved `limitHwid` **only** when it is not `None`; never default it to 0 (FR-019, forbidden-defaults)
- [x] T023 [US1] Make `v3_update_client` in `panel/adapters/xui.py` preserve the device limit by default, taking the authoritative read when the caller did not supply one (FR-018)
- [x] T024 [US1] Make `v3_enable_client` in `panel/adapters/xui.py` preserve the device limit on both the `bulkEnable` and the fallback update path (FR-018)
- [x] T025 [US1] Fail closed when the authoritative read fails: do not issue a payload that would clear the value; return an actionable error (FR-021, D3)
- [x] T026 [US1] Verify every v3 mutation caller in `panel/routes/clients.py` (renew, edit, toggle, reset, rotate, add) flows through the preserving path (FR-018)
- [x] T027 [P] [US1] Preservation unit tests: value preserved, explicit change honoured, `0` round-trips as `0`, field absent when the panel does not expose it, read-failure fails closed — `tests/test_3xui_compat.py` (SC-001)
- [x] T028 [P] [US1] Field-preservation matrix test for `resetDay`, `resetMax`, `trafficReset`, `trafficResetDay`, `keepAlive`, `flow`, `subId`, `comment`, `enable`, `expiryTime`, `totalGB` — `tests/test_3xui_compat.py` (FR-022, SC-004)

## Phase 6: US7 — Existing supported behaviour preserved (P1)

- [x] T029 [US7] Assert the baseline profile changes no request EVE already issues: method, path, headers and body identical for legacy and pre-3.7 panels (FR-041, G7, SC-005)
- [x] T030 [P] [US7] Regression-guard the existing behaviours: nested-JSON vs JSON-string inbound fields, UUID identity, legacy fallback chain, VLESS/VMess/Trojan/Shadowsocks/WireGuard — extend `tests/test_3xui_compat.py` (FR-042, FR-043)

## Phase 7: US4 — Panel-side lifecycle automation guardrail (P2)

- [x] T031 [US4] Detect panel-side automation fields (`resetDay`, `resetMax`, `trafficReset`, `trafficResetDay`) in `panel/services/xui_compat.py` and produce a `LifecycleAutomationFinding` (FR-024, data-model)
- [x] T032 [US4] Surface `panel_lifecycle_automation_detected` and classify the service as `partially_managed`; never write or zero the fields (FR-025, FR-026)
- [x] T033 [P] [US4] Tests: settings survive unrelated mutations; finding is emitted; no EVE lifecycle event is generated (FR-027)

## Phase 8: US5 — Subscription path authority (P2)

- [x] T034 [US5] Consume `subPath` (and audit `subJsonPath` / `subClashPath`) from the panel settings read already performed in `panel/services/subscription.py:326` (FR-028, D7)
- [x] T035 [US5] Apply panel-authoritative path on the `xui_3_8` profile with an explicit, observable fallback to the configured `sub_path` when unavailable (FR-028, FR-029)
- [x] T036 [US5] Ensure pre-3.8 behaviour is unchanged and no new panel call is added on the public subscription path (FR-030, FR-031, FR-040)
- [x] T037 [P] [US5] Tests: randomised 3.8 path, operator-changed path, setting endpoint unavailable ⇒ fallback marked, older version unchanged (SC-006)

## Phase 9: US6 — Operator visibility (P2)

- [x] T038 [US6] Add a compatibility block to `panel/routes/doctor.py:doctor_summary` exposing detected version, profile, detection source, certification and auth state (FR-032)
- [x] T039 [US6] Expose the named degraded states (`version_unknown`, `future_version_uncertified`, `api_auth_invalid`, `api_scope_insufficient`, `panel_lifecycle_automation_detected`, `subscription_path_fallback`) (FR-033)
- [x] T040 [US6] Never report a server healthy when management cannot work due to insufficient token scope (FR-034)
- [x] T041 [P] [US6] Assert no token, password or subscription secret appears in the compatibility/doctor output (FR-035, SC-006)

## Phase 10: Polish, documentation and acceptance

- [x] T042 Update `3XUI_V3_API.md`: remove the "tokens never expire" claim; document 3.7 scoped and expiring tokens, the admin-scope requirement, 401-vs-403 semantics, token rotation, the 3.7 and 3.8 profiles, 3.8 subscription-path behaviour, and the unknown-future-version policy (FR-015, mission §15)
- [x] T043 Add the version-support matrix to `3XUI_V3_API.md` with rows adjusted to repository evidence (legacy / 3.5.x / 3.7.x / 3.8.x / 3.9.x+)
- [x] T044 Bump `APP_VERSION` in `app.py` by one patch level per `AGENTS.md`, and add the matching CHANGELOG entry (repo versioning policy)
- [x] T045 Run the targeted suites: `tests/test_3xui_compat.py`, renew/enable, subscription, server polling/fetch, telemetry integration, lifecycle/SMS generation, allow_insecure/security
- [x] T046 Run the full EVE suite plus `scripts/release_check.py --json`, `scripts/ui_design_audit.py --check`, `tests/test_docs_index.py` (SC-008)
- [x] T047 Controlled acceptance against a real 3.8.x panel: run the P0 preservation proof and the auth matrix, asserting persisted panel state (SC-001, SC-009)
- [x] T048 WAIVED / NOT REQUIRED FOR RELEASE — controlled acceptance against a real 3.7.x panel. Reason: product decision — real 3.7 panel acceptance intentionally excluded from acceptance scope. 3.7 implementation is complete and its automated/contract compatibility tests remain required (SC-009).
- [x] T049 Record the extended-feature status (TUIC / AmneziaWG / HWID management) as implemented / partial / not implemented without downgrading the core verdict (FR-044)

## Dependencies

- Phase 1 → Phase 2 → all user-story phases.
- Phase 3 (profiles) blocks Phase 4, 5, 8 — they consume the profile.
- Phase 5 (T021) blocks T023/T024/T026.
- Phase 10 documentation tasks depend on implementation being final (T042/T043 after T021–T041).
- T048 is waived by product decision and is not a release blocker; T047 remains
  the required controlled real-panel acceptance task for 3.8.x.

## Parallel opportunities

- T003, T004 → parallel (different functions, same new file — sequence the writes).
- T012, T020, T027, T028, T030, T033, T037, T041 → parallel test authoring.
- T021 → T022 → T023/T024 sequential (same file, dependent).

## MVP scope

US1 + US3 (Phase 3 + Phase 5) — the confirmed P0 defect is closed and version
gating exists. US2 is required before the 3.7/3.8 verdicts can be called PASS.

## Phase 11: Convergence

Convergence run 1 (2026-09-15). Remaining work, traced to the artifact requiring it.

- [x] T050 Surface panel-side lifecycle automation through the service/doctor layer per FR-024/FR-026: normal client reads and cache recomputation now classify affected clients as partially managed and publish `panel_lifecycle_automation_detected` without creating an EVE lifecycle event.
- [x] T051 Implement 3.8 subscription-path authority per FR-028/FR-029: certified 3.8 profiles consume cached panel-advertised `subPath`, `subJsonPath`, and `subClashPath`; configured fallback remains explicit and Doctor-visible, while pre-3.8/future/unknown profiles retain configured behaviour.
- [x] T052 Verify the panel_compatibility doctor block through the authenticated route per FR-032/FR-034: the authenticated integration test observes version, profile, source, certification, and auth state and verifies that credentials, subscription paths, and private-key material are not exposed.
- [x] T053 WAIVED / NOT REQUIRED FOR RELEASE — real 3.7.x panel acceptance was not run and no panel was obtained, built, or started. Reason: product decision — real 3.7 panel acceptance intentionally excluded from acceptance scope. This waiver closes the former convergence blocker without claiming execution; required 3.7 automated/contract evidence remains green.
- [x] T054 Re-run and record the full pytest result per SC-008: after stabilizing the timing harness, the full suite completed with 1502 passed, 3 skipped, 22 subtests passed, and zero failures. The focused compatibility/Doctor/benchmark suite and the docs, release, and UI guards also pass.

### Note on FR-023 (raised as CRITICAL in analysis.md)

Closed during implementation rather than deferred: the authoritative read is
restricted to xui_compat.PRESERVED_CLIENT_FIELDS (a single-entry allowlist) and
returns None for anything else, so no secret or write-only field is read back or
resubmitted. Asserted by
ClientDeviceLimitPreservationTests.test_only_the_device_limit_is_on_the_preservation_allowlist
and test_secret_bearing_fields_are_never_invented.

## Format validation

All tasks use `- [ ] TNNN [P?] [USn?] description with file path`. Setup and
Polish phases carry no story label; all user-story phases carry one.


