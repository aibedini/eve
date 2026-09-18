# Phase 1 Data Model — 3x-ui 3.7.x / 3.8.x Version-Gated Compatibility

All entities are **derived/in-memory**. This feature adds no database table and no
migration (Constitution X; plan Complexity Tracking is empty).

---

## PanelVersion

A normalised, comparable representation of a panel's self-reported version.

| Field | Type | Notes |
| --- | --- | --- |
| `raw` | string \| null | exactly what the panel returned, preserved for diagnostics |
| `major` | int \| null | |
| `minor` | int \| null | |
| `patch` | int \| null | absent patch normalises to `0` |
| `family` | tuple(int,int) \| null | `(major, minor)`; null when unparseable |
| `is_parsed` | bool | false ⇒ treat as unknown, never guess |

**Normalisation rules** (from research D2):

- Accepts an optional leading `v` / `V` (`v3.8.0` ≡ `3.8.0`).
- Accepts 2- or 3-component versions (`3.8` ≡ `3.8.0`).
- Trailing build suffixes (e.g. `3.8.0+build`) are stripped before parsing.
- Rejects: empty, `dev+`, commit hashes, `unknown`, any non-numeric component.
- **Never** compared as a string anywhere in the codebase.

**Validation invariants**:
- `is_parsed == false` ⇒ `family is None` ⇒ baseline profile + `panel_version_unknown`.
- `family` is derived from `(major, minor)` only; patch never influences the profile.

---

## PanelCompatibilityProfile

The named capability set that governs behaviour for a family.

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | `baseline_v3` \| `xui_3_7` \| `xui_3_8` |
| `certified` | bool | whether EVE claims verified support for this family |
| `scoped_tokens` | bool | panel may issue scoped API tokens |
| `expiring_tokens` | bool | panel may issue expiring API tokens |
| `bearer_rejection_is_401` | bool | a rejected Bearer yields 401 without extra headers |
| `bearer_hint_header_required` | bool | 401 only distinguishable when `X-Requested-With: XMLHttpRequest` is sent |
| `client_limit_hwid` | bool | panel's client record carries a per-device limit |
| `panel_lifecycle_automation` | bool | panel may auto-renew / auto-reset a client |
| `random_subscription_paths` | bool | fresh installs randomise the subscription path |
| `amneziawg` | bool | `amneziawg` protocol supported |
| `tuic` | bool | `tuic` protocol supported |

**Populated values — every flag traced to exact-tag evidence**

| Flag | baseline_v3 | xui_3_7 | xui_3_8 | Evidence |
| --- | --- | --- | --- | --- |
| `certified` | true (pre-existing support) | true | true | this feature |
| `scoped_tokens` | false | true | true | `api.go enforceTokenScope` + `ApiToken.Scope` in both tags |
| `expiring_tokens` | false | true | true | `ApiToken.ExpiresAt` in both tags |
| `bearer_rejection_is_401` | false | **false** | **true** | `checkAPIAuth` differs between tags |
| `bearer_hint_header_required` | false | **true** | **false** | idem |
| `client_limit_hwid` | false | true | true | `ClientRecord.limitHwid` in both OpenAPI specs |
| `panel_lifecycle_automation` | false | true | true | `ResetDay`/`ResetMax`/`TrafficReset`/`TrafficResetDay` in both `model.Client` |
| `random_subscription_paths` | false | **false** | **true** | `setting.go:356` (3.8) vs `:104` (3.7) |
| `amneziawg` | false | true | true | protocol constant in both tags |
| `tuic` | false | **false** | **true** | protocol constant only in 3.8.0 |

> `persistent_keepalive` is **deliberately absent** from the profile table. The
> mission's example listed it as 3.8-only; verification showed `KeepAlive` exists
> in `model.Client` in **both** 3.7.0 and 3.8.0, and 3.8.0 only changed the admin
> UI. Encoding it would have been an unverified claim (FR-010).

**Certification states**:
`supported` (baseline_v3, xui_3_7, xui_3_8) |
`certification_required` (parsed version outside the whitelist) |
`unverified` (version unknown / unparseable).

---

## PanelCompatibility

The per-panel resolution result handed to callers and to the doctor surface.

| Field | Type | Notes |
| --- | --- | --- |
| `server_id` | int | cache key |
| `detected_version` | string \| null | raw as reported |
| `version` | PanelVersion | normalised |
| `profile` | PanelCompatibilityProfile | never null; defaults to baseline |
| `detection_source` | string | `server_status` \| `panel_update_info` \| `none` |
| `confidence` | string | `authoritative` \| `corroborated` \| `unknown` |
| `certification` | string | see above |
| `warnings` | list[string] | named degraded conditions |
| `resolved_at` | float | monotonic timestamp for TTL |

**State transitions**:

```text
(unresolved)
  ├─ /server/status ok, panelVersion parseable
  │     ├─ family in {(3,7)} -> profile xui_3_7, confidence authoritative
  │     ├─ family in {(3,8)} -> profile xui_3_8, confidence authoritative
  │     └─ otherwise        -> profile baseline_v3 + certification_required
  ├─ /server/status ok, panelVersion missing/unparseable
  │     └─ try getPanelUpdateInfo -> if parseable, confidence corroborated
  │           else -> profile baseline_v3 + panel_version_unknown (unverified)
  └─ /server/status unreachable
        └─ profile baseline_v3 + panel_version_unknown; no version-specific
           behaviour is attempted
```

**Cache entry lifecycle**: created on first successful resolution; expires after a
bounded TTL; explicitly invalidated on server configuration change, credential or
token change, explicit connection test, detected authentication failure, and
manual refresh. Authentication failures must **never** write a
`capability = unsupported` verdict (FR-012/FR-013).

---

## PanelClientSettingSnapshot

The authoritative view EVE consults before mutating a client, so that settings its
normal read path cannot see are preserved.

| Field | Type | Notes |
| --- | --- | --- |
| `email` | string | identity used by the mutation |
| `limit_hwid` | int \| null | null ⇒ the panel does not expose it ⇒ EVE MUST NOT send it |
| `source` | string | which panel read produced the snapshot |
| `read_at` | float | freshness |

**Invariants**:
- `limit_hwid is None` ⇒ the field is omitted from the payload (never defaulted to 0).
- `limit_hwid == 0` is a legitimate operator value ("no limit") and MUST be
  preserved as `0`, not treated as "unset" (spec Edge Case).
- If the snapshot cannot be read, the mutation MUST NOT proceed in a way that
  clears the value (FR-021).
- An explicit operator request to change the device limit bypasses preservation
  for that field only (FR-018 / US1 AS3).

---

## LifecycleAutomationFinding

| Field | Type | Notes |
| --- | --- | --- |
| `server_id`, `email` | identity of the affected client | |
| `fields` | dict | the panel-side settings observed (`resetDay`, `resetMax`, `trafficReset`, `trafficResetDay`) |
| `severity` | string | `warning` — EVE is no longer the sole lifecycle authority |
| `operator_action` | string | human-readable resolution guidance |
| `managed_state` | string | `fully_managed` \| `partially_managed` |

**Invariant**: EVE never writes these fields and never emits EVE lifecycle events
from them in this feature (FR-025, FR-027).
