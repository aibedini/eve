# Feature Specification: 3x-ui 3.7.x / 3.8.x Version-Gated Compatibility

**Feature Branch**: `feat/3xui-37-38-compat`

**Created**: 2026-09-15

**Status**: Draft

**Input**: User description: "Implement and prove safe, version-gated compatibility between EVE and 3x-ui 3.7.x / 3.8.x. Behavior introduced for 3.7.x or 3.8.x MUST NOT automatically apply to older versions, 3.9.x, 4.x, or any unknown future version. Core certified compatibility is the release blocker; extended feature parity (TUIC, AmneziaWG, HWID management) may be staged separately."

## Context

EVE drives third-party 3x-ui panels over their HTTP API. Upstream 3.7.0 and 3.8.0
changed API semantics in ways that can silently corrupt operator configuration or
misclassify a modern panel as legacy. The compatibility contract must be explicit,
version-gated, and provable — not an opportunistic "version >= 3.7" branch.

**Verified upstream references** (exact tags, cloned and inspected):

| Tag | Commit SHA |
| --- | --- |
| `v3.7.0` | `f727d04f6522bb94a8fb52e8352fdcafb51c11e1` |
| `v3.8.0` | `837addf66e945a80080273b5d2a315dea765d748` |

**Verified upstream deltas that drive this feature** (each confirmed against the
exact tagged Go source and generated OpenAPI, not from release notes):

| Behaviour | 3.7.0 | 3.8.0 |
| --- | --- | --- |
| `POST /clients/update/{email}` binds `limitHwid` as a **sibling** of the client object, defaulting to `0` when absent, and writes it **unconditionally** | yes | yes |
| Scoped API tokens (`admin` / `monitor` / `node-sync`) with allowlist enforcement and `403` on scope miss | yes | yes |
| API tokens may carry an expiry (`ApiToken.ExpiresAt`) | yes | yes |
| A rejected **Bearer** token yields `401` | **no** — yields `404` unless `X-Requested-With: XMLHttpRequest` is present | **yes** |
| Per-client lifecycle automation fields (`resetDay`, `resetMax`, `trafficReset`, `trafficResetDay`) | yes | yes |
| `amneziawg` protocol | yes | yes |
| `tuic` protocol | **no** | **yes** |
| Fresh-install subscription path is randomised (`"/" + random.NumLower(16) + "/"`) | **no** — fixed `/sub/` | **yes** |
| `GET /panel/api/server/status` exposes `obj.panelVersion` locally (no internet needed) | yes | yes |
| `GET /panel/api/server/getPanelUpdateInfo` exposes `obj.currentVersion` | yes | yes |

**Confirmed defect this feature must close (P0).** On a live 3.8.0 panel:

```text
[before] limitHwid = 2
POST /panel/api/clients/update/{email}   (body carries no limitHwid — exactly what EVE sends)
[after]  limitHwid = 0
```

The device cap is destroyed by a mutation that had nothing to do with it. EVE
cannot echo the value from its normal read path because
`GET /panel/api/inbounds/list` → `settings.clients[]` does **not** contain
`limitHwid` at all (verified), while `GET /panel/api/clients/list/paged` does.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Renewing a customer must not destroy their device limit (Priority: P1)

An operator has configured a per-client device (HWID) limit on a 3.7.x or 3.8.x
panel. A customer renews through EVE. After the renewal the device limit must be
exactly what it was before.

**Why this priority**: This is silent security erosion. The operator believes a
shared-account protection is active; an unrelated EVE action turns it off. It is
already happening in production on any 3.7+/3.8+ panel.

**Independent Test**: Seed a client with `limitHwid = 2` on a real panel, run each
EVE mutation path (renew, change volume, enable, edit, rotate, reset, admin edit),
and assert the persisted value is still `2` after every one.

**Acceptance Scenarios**:

1. **Given** a 3.7.x/3.8.x client with `limitHwid = 2`, **When** EVE renews the
   client, **Then** the panel still reports `limitHwid = 2`.
2. **Given** the same client, **When** EVE changes the volume, enables, edits or
   rotates it, **Then** the panel still reports `limitHwid = 2`.
3. **Given** an operator explicitly asks EVE to change the device limit,
   **When** the change is applied, **Then** the new requested value is persisted.
4. **Given** a panel whose client record does not expose a device limit at all,
   **When** EVE mutates the client, **Then** no device-limit field is invented.

---

### User Story 2 - A modern panel must never be mistaken for a legacy panel (Priority: P1)

An operator configures EVE with a read-only or node-scoped token on a 3.7.x/3.8.x
panel. EVE must report an actionable authorization problem — it must not decide
the panel is old and issue legacy requests that cannot work.

**Why this priority**: Misclassification causes member-visible breakage (renewals
that leave the user inactive) and is already a fixed bug class in EVE for
token-less panels. Scoped tokens re-open it.

**Independent Test**: Point EVE at a 3.7.x/3.8.x panel with `admin`, `monitor`,
`node-sync`, expired and rotated tokens; assert the resulting classification and
the operator-facing diagnostic for each.

**Acceptance Scenarios**:

1. **Given** a valid admin token, **When** EVE probes the panel, **Then** the
   panel is classified as supported and version detection succeeds.
2. **Given** a monitor or node-sync token, **When** EVE probes, **Then** the
   panel is reported as *authorization insufficient* — never as legacy.
3. **Given** an expired or rotated token (rejected with 401), **When** EVE
   probes, **Then** the panel is reported as *authentication invalid* — never as
   legacy — and the cached capability result is not poisoned to "not v3".
4. **Given** a genuinely absent route (404 on a route that exists on supported
   versions), **When** EVE probes, **Then** route absence is reported distinctly
   from both auth failures.

---

### User Story 3 - Version-specific behaviour must not leak to other versions (Priority: P1)

An operator runs a panel on 3.5.x, 3.6.x, 3.9.x or 4.x. EVE must keep behaving
exactly as it does today for those panels, and must not silently adopt 3.8
semantics for a version it has never certified.

**Why this priority**: The whole point of the feature. An unversioned change here
would convert a compatibility fix into a fleet-wide regression risk.

**Independent Test**: Drive version detection with 3.5.x, 3.6.x, 3.7.x, 3.8.x,
3.9.0, 4.0.0 and an unparseable value; assert the selected compatibility profile
and that 3.9/4.x never select the 3.8 profile.

**Acceptance Scenarios**:

1. **Given** a panel reporting `3.7.4`, **When** the version is resolved,
   **Then** exactly the 3.7 family profile is selected.
2. **Given** a panel reporting `3.8.1`, **When** the version is resolved,
   **Then** exactly the 3.8 family profile is selected.
3. **Given** a panel reporting `3.9.0` or `4.0.0`, **When** the version is
   resolved, **Then** the baseline-safe profile is selected, a warning is
   emitted, and the 3.8 profile is **not** inherited.
4. **Given** a panel where version discovery fails or returns a malformed value,
   **When** the version is resolved, **Then** the version is recorded as unknown,
   the baseline-safe profile is used, and no 3.7/3.8-specific mutation is attempted.
5. **Given** a version string prefixed with `v` (e.g. `v3.8.0`), **When** it is
   normalised, **Then** it resolves identically to the unprefixed form.

---

### User Story 4 - Panel-side lifecycle automation must not silently bypass EVE (Priority: P2)

A 3.7.x/3.8.x client may carry panel-side automatic renewal or traffic-reset
settings. EVE owns lifecycle truth (renewal, generation, notification
supersession). An operator must be told when a panel will act on its own, and EVE
must not silently erase or silently accept those settings.

**Why this priority**: Correctness of the notification/lifecycle guarantees, but
not a destructive default — no current EVE path writes those fields.

**Independent Test**: Seed a client with panel-side automation enabled, run EVE
mutations, and assert the settings survive and the incompatibility is surfaced.

**Acceptance Scenarios**:

1. **Given** a client with panel-side automatic lifecycle settings, **When** EVE
   reads it, **Then** the condition is reported as a named incompatibility.
2. **Given** the same client, **When** EVE performs an unrelated mutation,
   **Then** those settings are unchanged.
3. **Given** a lifecycle mutation on such a service, **When** EVE proceeds,
   **Then** the affected service is classified as at most partially managed
   rather than silently fully managed.

---

### User Story 5 - Subscription links must use the panel's real path (Priority: P2)

A fresh 3.8.x install uses a randomised subscription path. EVE must build
subscription links from the panel's authoritative setting, or fall back visibly.

**Why this priority**: Broken customer-facing links, but recoverable and visible.

**Independent Test**: For a 3.8 panel whose sub path is randomised and one where
it was changed by the operator, assert EVE uses the panel value; when the setting
is unavailable, assert an explicit, observable fallback.

**Acceptance Scenarios**:

1. **Given** a 3.8.x panel advertising a randomised sub path, **When** EVE builds
   a subscription link, **Then** it uses the advertised path.
2. **Given** a panel whose settings endpoint does not provide the path, **When**
   EVE builds a link, **Then** it uses the configured fallback and marks the
   source as a fallback.
3. **Given** an older panel version, **When** EVE builds a link, **Then** today's
   behaviour is unchanged.

---

### User Story 6 - Operators can see compatibility state per panel (Priority: P2)

An operator must be able to tell, per configured panel, which version was
detected, which compatibility profile is active, how it was detected, whether it
is certified, and whether authentication is healthy.

**Why this priority**: Without it, every degraded state is invisible and
undiagnosable.

**Independent Test**: Render the doctor surface for healthy, version-unknown,
future-uncertified, auth-invalid, scope-insufficient, lifecycle-automation and
subscription-path-fallback states; assert each is distinguishable and leaks no
credentials.

**Acceptance Scenarios**:

1. **Given** a healthy 3.8.x panel, **When** the doctor surface is read,
   **Then** version, profile, detection source, certification and auth state are
   all present and correct.
2. **Given** a panel with insufficient token scope, **When** the doctor surface is
   read, **Then** the server is **not** reported healthy.
3. **Given** any state, **When** the doctor surface is read, **Then** no API
   token, password or subscription secret appears.

---

### User Story 7 - Existing supported behaviour is preserved (Priority: P1)

Every panel version EVE supports today must keep working exactly as it does now.

**Why this priority**: Non-negotiable; this feature must not be a rewrite.

**Independent Test**: The pre-existing compatibility suite plus the full EVE
suite must stay green, with no change to legacy/older-version request shapes.

**Acceptance Scenarios**:

1. **Given** a legacy or pre-3.7 panel, **When** any supported operation runs,
   **Then** the exact same requests are issued as before this feature.
2. **Given** nested-JSON and JSON-string inbound field shapes, UUID identity,
   and the existing legacy fallback chain, **When** they are exercised,
   **Then** they behave identically to before.

### Edge Cases

- Panel reachable but `/server/status` returns `success:false` → version unknown,
  baseline profile, warning; no version-specific mutation.
- Panel returns a version the parser cannot understand (empty, `dev+`, a commit
  hash, `unknown`) → version unknown; never guessed into 3.7/3.8.
- Version endpoint requires internet and the panel is air-gapped → the local
  `panelVersion` source is authoritative on its own; failure of the internet-backed
  source alone must not degrade the profile.
- Token expires or is rotated between two operations → the cached capability and
  version results are invalidated; the panel must not be re-reported as older.
- Panel is upgraded from 3.6.x to 3.8.x while configured in EVE → the stale cache
  must not survive indefinitely, and an explicit refresh/connection test must
  re-detect.
- A client legitimately has `limitHwid = 0` (no limit) → preservation must keep
  `0`, not "restore" a non-zero value.
- The authoritative device-limit source is temporarily unavailable during a
  mutation → the mutation must not silently proceed in a way that clears the value.
- Concurrent EVE operations on the same client → the last writer must not clear
  the limit.
- A 3.9.x/4.x panel that happens to expose 3.8-era capabilities → capabilities
  alone must not promote it to the 3.8 profile.

## Requirements *(mandatory)*

### Functional Requirements

**Version detection and profiles**

- **FR-001**: EVE MUST resolve a panel's version independently from generic
  API-capability probing, and MUST keep `panel_version`, `panel_capabilities`
  and `authentication_status` as separate concepts.
- **FR-002**: The primary version source MUST be the panel's own locally available
  identity (`GET /panel/api/server/status` → `obj.panelVersion`), which requires
  no outbound internet access from the panel.
- **FR-003**: The internet-dependent source (`GET /panel/api/server/getPanelUpdateInfo`
  → `obj.currentVersion`) MAY be used as a corroborating source, and its failure
  MUST NOT by itself degrade a version that the local source resolved.
- **FR-004**: Version values MUST be normalised structurally (numeric major, minor,
  patch), accepting an optional leading `v`. Versions MUST NOT be compared as
  strings.
- **FR-005**: EVE MUST derive a compatibility **family** as `(major, minor)` and
  select a compatibility profile from that family.
- **FR-006**: Exactly `3.7.x` MUST select the 3.7 profile; exactly `3.8.x` MUST
  select the 3.8 profile. Patch-level differences within a family MUST NOT change
  the profile.
- **FR-007**: Any version outside the certified families — including `3.9.x`,
  `4.x`, and anything greater — MUST select the baseline-safe profile, MUST NOT
  inherit the 3.8 profile, and MUST emit a certification-required warning.
- **FR-008**: When version discovery fails, returns an unparseable value, or is
  ambiguous, EVE MUST NOT guess 3.7 or 3.8. It MUST record the version as unknown,
  mark compatibility `unverified`, use baseline-safe behaviour, and surface an
  operator warning.
- **FR-009**: All profiles MUST be defined in one central compatibility layer.
  Version comparisons MUST NOT be scattered across route, job or adapter modules.
- **FR-010**: Each capability flag encoded in a profile MUST be justified by
  evidence from the exact upstream tag (source or generated OpenAPI) for that
  family. Capabilities MUST NOT be inferred from release notes alone.

**Authentication and authorization semantics**

- **FR-011**: EVE MUST distinguish, for the certified families, at least:
  success; route/capability absent; authentication invalid; authorization
  (scope) insufficient; transport error; and invalid/unparseable response.
- **FR-012**: A 401 rejected-credential response MUST NOT be interpreted as
  "legacy panel", and MUST NOT be cached as "v3 client API unsupported".
- **FR-013**: A 403 scope rejection MUST NOT be interpreted as "legacy panel",
  and MUST NOT be cached as "v3 client API unsupported".
- **FR-014**: EVE MUST NOT attempt to widen, escalate or auto-modify token
  privileges. An insufficient token MUST fail with an actionable operator message.
- **FR-015**: Full EVE management of 3.7.x/3.8.x panels MUST require an
  admin-scoped API token; monitor and node-sync scopes MUST be reported as
  insufficient for management rather than silently treated as usable.
- **FR-016**: An expired or rotated token MUST invalidate the relevant cached
  capability/authentication result so EVE does not continue to claim an older
  panel version.

**Configuration preservation (P0)**

- **FR-017**: A client-side setting that an operator configured on the panel MUST
  survive any EVE mutation that did not request a change to that setting.
- **FR-018**: Specifically, the per-client device limit (`limitHwid`) MUST be
  preserved across renew, volume change, enable, disable/re-enable, edit, reset
  and rotate operations on 3.7.x and 3.8.x.
- **FR-019**: The preserved value MUST come from an authoritative panel read, not
  from a default, a guess or a hard-coded zero.
- **FR-020**: EVE MUST NOT send a device-limit value for panels whose contract
  does not define it, and MUST NOT invent a value where the panel record exposes
  none.
- **FR-021**: When the authoritative value cannot be read at mutation time, EVE
  MUST NOT proceed in a way that clears the stored value.
- **FR-022**: All other upstream client fields supported by the certified
  families — including `resetDay`, `resetMax`, `trafficReset`,
  `trafficResetDay`, `disableFlow`, `allowedIPsByInbound`,
  `forwardedPorts`, `keepAlive`, `flow`, `subId`, `comment`, `enable`,
  `expiryTime`, `totalGB` — MUST survive unrelated EVE mutations.
- **FR-023**: EVE MUST NOT resubmit fields whose upstream contract forbids or
  makes write-only, and MUST NOT read back or echo secrets it is not entitled to.

**Lifecycle-automation guardrail**

- **FR-024**: EVE MUST detect panel-side automatic lifecycle settings on a
  client and report them as a named incompatibility.
- **FR-025**: EVE MUST NOT silently overwrite operator-configured panel-side
  lifecycle settings.
- **FR-026**: EVE MUST NOT silently treat a client with panel-side automation as
  fully EVE-managed; such a service MUST be classified as at most partially
  managed until the operator resolves it.
- **FR-027**: Translating panel-side automatic lifecycle transitions into EVE
  lifecycle events or generation advances is explicitly out of scope for this
  feature.

**Subscription path authority**

- **FR-028**: For the 3.8 profile, EVE MUST resolve subscription paths from the
  panel's authoritative settings where available.
- **FR-029**: Where the authoritative setting is unavailable, EVE MUST use an
  explicitly configured fallback and MUST mark the source as a fallback.
- **FR-030**: EVE MUST NOT silently substitute a default path for a panel that
  advertises a different one.
- **FR-031**: Behaviour for pre-3.8 versions MUST be unchanged.

**Observability**

- **FR-032**: For every configured panel, EVE MUST expose detected version,
  active compatibility profile, detection source, certification state and
  authentication state.
- **FR-033**: EVE MUST expose named degraded states for at least: version unknown,
  future version uncertified, authentication invalid, scope insufficient,
  panel-side lifecycle automation detected, and subscription path fallback.
- **FR-034**: A server whose management operations cannot work because of
  insufficient token scope MUST NOT be reported as healthy.
- **FR-035**: Compatibility and doctor surfaces MUST NOT expose API tokens,
  passwords, subscription secrets or client private keys.

**Performance and caching**

- **FR-036**: Version/compatibility resolution MUST NOT add a panel request per
  rendered page or per client operation.
- **FR-037**: Compatibility metadata MUST be cached per server with a bounded TTL.
- **FR-038**: The cache MUST be invalidated on server configuration change,
  credential/token change, explicit connection test, detected authentication
  failure, and manual refresh.
- **FR-039**: A stale compatibility cache MUST NOT survive indefinitely after a
  panel upgrade.
- **FR-040**: The change MUST NOT introduce new synchronous panel calls on public
  subscription-page paths.

**Preserved behaviour**

- **FR-041**: Legacy and pre-3.7 panels MUST issue byte-identical request shapes
  to those issued before this feature.
- **FR-042**: Existing behaviour for already-supported v3 families MUST be
  preserved unless a regression test proves a change is required.
- **FR-043**: Working integration code MUST NOT be broadly rewritten merely
  because newer upstream APIs exist.

**Out of scope (staged separately)**

- **FR-044**: TUIC, AmneziaWG and full HWID-management UI are NOT required for
  core certified compatibility. Their status MUST be reported as
  implemented / partial / not implemented, and their absence MUST NOT downgrade
  a core compatibility verdict.
- **FR-045**: Bulk client APIs, client groups, `lastOnline` optimisations,
  `activeInbounds`, `onlinesByGuid`, Xray route tests, historical server
  metrics, post-quantum key generators, geodata browsing, `descendants`,
  Discord integration and subscription balancers MUST NOT be implemented in this
  feature unless required to fix a proven compatibility defect.

### Key Entities

- **Panel compatibility record** (per configured panel): detected version string,
  normalised numeric version, family `(major, minor)`, selected profile,
  detection source, confidence, certification state, authentication state,
  timestamp, and any named degraded conditions.
- **Compatibility profile**: a named capability set (for example baseline,
  3.7, 3.8) describing token scoping, token expiry, rejected-credential
  semantics, per-client device-limit support, panel-side lifecycle automation,
  subscription-path authority, and protocol availability. Profiles are declared
  centrally; the future/unknown case maps to the baseline profile.
- **Panel client record**: the upstream client object as seen through the read
  path EVE uses to build mutations, plus the authoritative source used to
  preserve settings the read path does not expose.
- **Lifecycle-automation finding**: a per-client condition recording that the
  panel may act on its own, its severity, and the operator action required.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Across every EVE mutation path, a panel-side device limit set to a
  non-zero value is still that exact value afterwards — 100% of mutation paths,
  proven on a real certified panel.
- **SC-002**: A 3.9.x or 4.x simulated panel selects the baseline profile in 100%
  of runs; the 3.8 profile is selected in 0% of those runs.
- **SC-003**: A panel using monitor or node-sync credentials is reported with an
  authorization diagnostic in 100% of runs, and is reported as legacy in 0%.
- **SC-004**: Every field in the preservation matrix survives unrelated mutations
  — zero unintended field changes across the matrix.
- **SC-005**: Panel versions 3.5.x, 3.6.x and legacy issue identical requests to
  the pre-change baseline — zero request-shape diffs.
- **SC-006**: An operator can determine, for any configured panel, its version,
  profile, certification and auth state in a single view, with zero credential
  disclosure.
- **SC-007**: Compatibility resolution adds zero additional panel requests to
  page renders and client operations once the cache is warm.
- **SC-008**: The full existing EVE test suite passes with zero regressions.
- **SC-009**: Core 3.7.x and 3.8.x acceptance each pass against a real panel of
  that exact family.

## Assumptions

- EVE's upstream panels are typically reachable over a network that may block
  outbound internet access from the panel itself, so version detection must not
  depend on the panel reaching GitHub.
- Operators may create scoped and expiring API tokens after upgrading to 3.7+;
  EVE documents and requires the admin scope for full management.
- EVE remains the lifecycle authority for the clients it manages; panel-side
  automation is treated as a conflict to surface, not a feature to adopt, in this
  feature.
- The preservation strategy for settings EVE cannot see through its primary read
  path is to consult an authoritative source that does expose them, rather than
  to widen the primary read.
- Extended protocol support (TUIC, AmneziaWG) and HWID-management UI are
  separate deliveries and do not gate core certification.
- Existing EVE versioning and release policy applies; the application version is
  bumped according to repository rules, not invented here.
