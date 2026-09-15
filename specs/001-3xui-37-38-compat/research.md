# Phase 0 Research — 3x-ui 3.7.x / 3.8.x Version-Gated Compatibility

Every decision below is grounded in evidence from the **exact upstream tags**
(`v3.7.0` = `f727d04f6522bb94a8fb52e8352fdcafb51c11e1`,
`v3.8.0` = `837addf66e945a80080273b5d2a315dea765d748`) — cloned source, generated
`frontend/public/openapi.json`, and a live disposable 3.8.0 panel. Release notes
were treated as hints only and were **not** sufficient evidence.

---

## D1 — Where does the panel version come from?

**Decision**: Primary source is `GET /panel/api/server/status` → `obj.panelVersion`.
Corroborating source is `GET /panel/api/server/getPanelUpdateInfo` → `obj.currentVersion`.

**Evidence**:
- `internal/web/service/server.go` declares `PanelVersion string \`json:"panelVersion"\``
  at line `105` (v3.8.0) and `103` (v3.7.0) — present in **both**.
- Present since **v3.3.1** (verified across v3.3.1, v3.4.0, v3.5.0, v3.6.0, v3.7.0, v3.8.0).
- Live 3.8.0 panel returned `{"panelVersion":"3.8.0", ...}` from `/server/status`.
- `GetUpdateInfo()` (`internal/web/service/panel/panel.go`) calls
  `fetchLatestPanelVersion()` **first** and returns `nil, err` when it fails; the
  handler then emits `{"success":false}` with **no `obj`**. It therefore requires
  the panel to reach GitHub.
- Live 3.8.0 panel returned
  `{"channel":"stable","currentVersion":"3.8.0","latestVersion":"v3.8.0","updateAvailable":false}`.

**Rationale**: EVE manages panels that are frequently air-gapped from GitHub. A
version source that fails on those panels would leave the entire feature inert
exactly where it is needed. The local source is authoritative because it is the
binary's own build identity.

**Alternatives considered**:
- `getPanelUpdateInfo` alone — rejected: network-dependent, returns no version at
  all on failure.
- Capability probing on 3.8-only routes (e.g. `/clients/happLink/{id}`,
  `/setting/testDiscord`) — rejected as a *version* source: it cannot distinguish
  3.8 from 3.9/4.x, and the mission forbids promoting a version from capability
  evidence. Retained only as corroboration where useful.
- Reading the SPA bundle fingerprint — rejected: not a contract, changes per build.

---

## D2 — How is a version turned into behaviour?

**Decision**: Normalise to `(major, minor, patch)`, derive a **family** `(major, minor)`,
and map the family through an explicit whitelist to a compatibility profile.
Anything not on the whitelist maps to the baseline-safe profile.

```text
family == (3, 7)  -> XUI_3_7
family == (3, 8)  -> XUI_3_8
anything else     -> BASELINE_V3   (+ certification-required warning)
version unknown   -> BASELINE_V3   (+ version-unknown warning)
```

**Rationale**: `if version >= (3, 7)` is explicitly forbidden by the mission and is
also simply wrong: 3.9/4.x must not inherit 3.8 semantics that were never
verified. A whitelist makes the certified set the only thing that can select a
non-baseline profile, and makes adding 3.9 a deliberate act.

**Alternatives considered**: ordered comparison against a minimum version —
rejected (fails FR-007). Feature-flag-by-capability alone — rejected (fails
FR-001 and cannot express "not yet certified").

---

## D3 — The `limitHwid` defect: how is it fixed without guessing?

**Decision**: Preservation is **evidence-driven**, not version-driven: EVE reads the
authoritative value from the panel's client record and echoes it on every
mutation, whenever the panel record exposes the field.

**Evidence — the defect is real and its mechanism is exact**:
- `internal/web/controller/client.go` (both tags) binds
  `var req struct { model.Client; LimitHwid int \`json:"limitHwid"\` }` — `limitHwid`
  is a **sibling** of the embedded client, so an absent key binds to the Go zero
  value `0`.
- `internal/database/model/model.go` — `model.Client` has **no** `limitHwid` field
  (verified by reading the struct); `ClientRecord` has
  `LimitHwid int \`json:"limitHwid" gorm:"column:limit_hwid;default:0"\``.
- `internal/web/service/client_crud.go:806` calls
  `setClientLimitHwidByEmail(nil, updated.Email, limitHwid)` **unconditionally**.
- `internal/web/service/client_hwid.go:296` writes
  `UpdateColumn("limit_hwid", limit)` **unconditionally**. Identical function in
  v3.7.0 and v3.8.0.
- Live 3.8.0 reproduction: `limitHwid` set to `2`, then an EVE-shaped
  `POST /panel/api/clients/update/{email}` with no `limitHwid` key →
  read-back `limitHwid = 0`, while `expiryTime` changed in the same request
  (proving the update applied).
- **EVE cannot currently echo it**: `GET /panel/api/inbounds/list` →
  `settings.clients[]` (EVE's `raw_client` source) does **not** contain
  `limitHwid` — verified against the live panel. But
  `GET /panel/api/clients/get/{email}` → `obj.client.limitHwid` and
  `GET /panel/api/clients/list/paged` → `obj.items[].limitHwid` both do, and
  `ClientRecord` carries the field in both tags' OpenAPI.

**Rationale**: The mission requires the special handling to be confined to
3.7/3.8 and forbids unsafe defaults such as `limitHwid = 0`. Reading the value
from the panel and echoing it satisfies both *and* is strictly safer than a
version gate: the trigger is an observed panel contract, not a version guess, so
a future 3.9 with the same shape is protected without being enrolled into any
3.8-only behaviour. This is the deliberate, documented reading of the gating rule
— the rule forbids *assuming new semantics*, and this assumes nothing; it
preserves what the panel itself reported.

**Where it is read from**: `/clients/get/{email}` (single authoritative read,
same call EVE already makes in `_v3_get_client`) rather than the paged listing,
so the read is per-mutated-client and cannot be confused by pagination.

**Failure behaviour**: if the authoritative read fails, EVE must not send a
payload that would clear the value (FR-021). It fails the mutation with an
actionable error rather than proceeding destructively.

**Alternatives considered**:
- Send a literal `0` — forbidden by the mission and actively harmful.
- Widen the primary inbound read to include `limitHwid` — impossible; the field is
  not in the inbound settings contract.
- Gate strictly on profile (`XUI_3_7`/`XUI_3_8` only) — rejected as *less* safe;
  it would leave a future panel with the same shape unprotected while providing
  no additional protection today. Recorded here as an explicit, reasoned deviation.

---

## D4 — Auth semantics: what does each status code mean?

**Decision**: Replace the boolean probe with a typed outcome:
`SUPPORTED | ROUTE_MISSING | AUTH_INVALID | SCOPE_INSUFFICIENT | TRANSPORT_ERROR | INVALID_RESPONSE`.

**Evidence**:
- v3.8.0 `internal/web/controller/api.go` `checkAPIAuth`: a request carrying
  `Authorization: Bearer …` that fails token matching aborts with
  `401 Unauthorized`; a bare unauthenticated request aborts with `404`.
- **v3.7.0 differs**: it aborts `401` only when
  `X-Requested-With: XMLHttpRequest` is present, otherwise `404`. Verified by
  reading both tagged copies of `checkAPIAuth`.
- `enforceTokenScope` (`api.go`): `ApiScopeAdmin` passes everything;
  `ApiScopeMonitor` and `ApiScopeNodeSync` are allowlisted; **any other scope is
  denied with `403`**.
- The node-sync allowlist does **not** include `/clients/list`,
  `/clients/list/paged`, `/clients/get/:email`, `/clients/subLinks/:subId`,
  `/clients/ips/:email`, `/clients/:email/attach`, `/clients/bulkEnable`,
  `/setting/all`, `/server/getDb`, `/inbounds/get/:id`, `/server/stopXrayService`.
- `ApiToken{Scope, ExpiresAt}` exists in **both** tags, so scoped *and* expiring
  tokens are a 3.7.0+ concern, not 3.8-only.

**Rationale for the 3.7 header detail**: on the 3.7 profile, sending
`X-Requested-With: XMLHttpRequest` makes a rejected credential distinguishable
from a missing route. Without it, 3.7 collapses both into `404` and EVE cannot
satisfy FR-011. This is a profile-gated request header, applied only where the
evidence shows it changes the response — it must not be sent on legacy paths.

**Consequences**: 401 and 403 must never populate the existing
`XUI_CAPABILITY_CACHE` as `v3_clients = false`, which is the mechanism that
currently turns an auth problem into a legacy misclassification.

**Alternatives considered**: treating 404 as "not v3" (current behaviour) —
rejected: that is exactly the misclassification the mission forbids; a scope
problem would silently become a legacy panel.

---

## D5 — Caching and invalidation

**Decision**: Extend the existing module-level, per-server cache pattern with a
bounded TTL, and register invalidation on the signals the mission lists.

**Evidence**: EVE already has `XUI_CAPABILITY_CACHE` /
`XUI_CAPABILITY_TTL = 600` in `panel/adapters/xui.py` and a single
`invalidate_xui_caches()` helper called from `panel/routes/admin.py:692` on
server configuration changes. Reusing this shape keeps the change small and
avoids new infrastructure.

**Invalidation triggers**: server configuration change (existing hook), credential
or token change (same hook), explicit connection test, detected authentication
failure, and manual refresh (FR-038).

**Known, documented limitation**: the cache is per process, so two gunicorn
workers may briefly disagree about a panel that was upgraded mid-TTL. This is
acceptable because the cache is a **hint**, not a durable fact (Constitution V),
and because the one behaviour that must be correct — device-limit preservation —
is read live from the panel at mutation time and does not consult the cache.

**Alternatives considered**: a Redis-backed shared cache — rejected for this
feature: it adds a cross-process dependency and a failure mode to solve a
bounded, non-correctness-critical staleness window. A database column — rejected:
it is derived data and would require a migration for no durable benefit.

---

## D6 — Panel-side lifecycle automation

**Decision**: Detect and surface only. EVE never writes these fields and never
turns them into EVE lifecycle events in this feature.

**Evidence**: `model.Client` in both tags carries `ResetDay` ("Calendar renewal
day 1-31, 0 = interval mode"), `ResetMax` ("Max auto-renew count, 0 = unlimited"),
`TrafficReset` (`never|hourly|daily|weekly|monthly`) and `TrafficResetDay`.
Because the client update is a merge over the existing record for the
`model.Client` part (`applyClientRecordMerge`, `client_crud.go:736`), EVE's
current mutations do **not** erase them — verified by reading the merge helper,
which ignores incoming zero values. The risk is therefore semantic, not
destructive: the panel can renew or reset on its own, bypassing EVE's generation
and notification-invalidation guarantees.

**Rationale**: Constitution II makes EVE the lifecycle authority. Silently
co-existing with panel-side automation would weaken that guarantee without
evidence or policy. Silently zeroing operator settings is equally unacceptable
(FR-025, and the mission's explicit prohibition).

**Alternatives considered**: integrating panel-side automation into EVE
generations — explicitly deferred by FR-027; it is a separate architecture feature.

---

## D7 — Subscription path authority

**Decision**: On the 3.8 profile, prefer the panel's advertised path from the
settings read EVE already performs; fall back to the configured value and mark
the source as a fallback.

**Evidence**: v3.8.0 `internal/web/service/setting.go:356` seeds fresh installs
with `{Key: "subPath", Value: "/" + random.NumLower(16) + "/"}`; v3.7.0 has only
the fixed default `"subPath": "/sub/"` at line 104. So the *reason* the panel path
may not be `/sub/` is a 3.8+ behaviour, even though the settings API that
exposes it exists in both.

**Evidence about EVE**: EVE stores `sub_path` as a manual per-server field
(`panel/models/core.py:108`, default `/sub/`, editable at
`panel/routes/admin.py:609,667`) and builds links from it
(`panel/services/subscription.py:94`, `panel/routes/clients.py:2769,3288`,
`panel/routes/subscription_pages.py:163-166`, `app.py:2812`). EVE already calls
`/panel/api/setting/all` in `panel/services/subscription.py:326` but reads only
`subTitle` and `subUpdates` from it.

**Rationale**: The read already exists and is already cached; consuming one more
field costs no additional panel request, satisfying Constitution IX and FR-040.

**Alternatives considered**: always trusting the EVE-configured value — rejected
(breaks on fresh 3.8 panels). Always trusting the panel — rejected (older panels
and settings failures must keep today's behaviour, FR-031).

---

## D8 — How is preservation actually proven?

**Decision**: Prove it at the layer that can lie: **persisted panel state**, on a
real panel, for every mutation path — not merely by asserting on the outbound
payload.

**Rationale**: The defect is a server-side default, so a test that only inspects
EVE's JSON body would pass while the value is still destroyed. The mission
requires Test C (real handler, persisted value) for exactly this reason.

**Layers**:
1. Unit — version normalisation, family resolution, profile selection, including
   `v`-prefixes, patch variants, `3.9.0`, `4.0.0`, `dev+`, empty.
2. Contract — payload/response shapes and status-code classification derived from
   the exact tagged OpenAPI/source.
3. Integration — through EVE's real wiring, asserting the outbound payload carries
   the preserved value and that auth failures never downgrade capability state.
4. Acceptance — live disposable panels; assert the **persisted** value read back
   from the panel after each mutation.

**Alternatives considered**: mocked panel responses only — insufficient; the
mission explicitly rejects calling a mock "3.8 compatibility".
