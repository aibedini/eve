# Implementation Plan: 3x-ui 3.7.x / 3.8.x Version-Gated Compatibility

**Branch**: `feat/3xui-37-38-compat` | **Date**: 2026-09-15 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-3xui-37-38-compat/spec.md`

## Summary

Replace EVE's single boolean "is this panel v3?" signal with a central,
version-gated compatibility layer, and close a confirmed P0 data-corruption
defect in which any EVE client mutation silently resets a panel-side per-device
(HWID) limit to `0` on 3x-ui 3.7.x and 3.8.x.

The technical approach is deliberately narrow:

1. **One new module** (`panel/services/xui_compat.py`) owns version
   normalisation, family resolution, profile selection, the per-server
   compatibility cache, and the profile capability table. Nothing else in the
   codebase compares versions.
2. **Version detection is local-first.** `GET /panel/api/server/status` →
   `obj.panelVersion` is authoritative and needs no outbound internet from the
   panel; `getPanelUpdateInfo` is corroboration only. Both were verified present
   and identical in shape on the exact `v3.7.0` and `v3.8.0` tags.
3. **Capability probing keeps its job** (does the first-class v3 client API
   exist?) but stops being treated as a version. It returns a typed result that
   separates *route missing* from *auth invalid* from *scope insufficient*.
4. **Preservation is evidence-driven, not version-guessed.** EVE's mutation read
   path (`/inbounds/list`) provably cannot see `limitHwid`; EVE reads the
   authoritative value from `/clients/get/{email}` and echoes it on unrelated
   mutations. Because the trigger is *the panel exposing the field*, no future
   version is silently enrolled into 3.8 semantics.
5. **Nothing else changes.** No endpoint rewrites, no schema migration, no new
   panel calls on public paths.

## Technical Context

**Language/Version**: Python 3.11 (EVE is Flask + SQLAlchemy + Alembic, modular
`panel/` package)

**Primary Dependencies**: Flask, requests, SQLAlchemy/PostgreSQL, Redis (optional,
graceful degradation), existing `panel/adapters/xui.py` panel client

**Storage**: No new persistent storage. Compatibility metadata is a per-server
in-process cache with TTL, mirroring the existing `XUI_CAPABILITY_CACHE` pattern.
No Alembic revision is required.

**Testing**: pytest (`testpaths = ["tests"]`, `pythonpath = ["."]`), interpreter
`.venv-test\\Scripts\\python.exe`. Existing suite
`tests/test_3xui_compat.py` is extended; new contract fixtures derived from the
exact upstream tags; plus controlled acceptance against real disposable panels.

**Target Platform**: EVE server (Linux/Docker in production, Windows dev checkout)

**Project Type**: Web application (server-rendered Flask panel + background workers)

**Performance Goals**: Zero additional panel requests per page render or client
operation once the compatibility cache is warm. Version resolution must not
appear on the public subscription path.

**Constraints**: Must preserve byte-identical request shapes for legacy and
pre-3.7 panels. Must not weaken TLS verification, `allow_insecure`, credential
storage or token logging.

**Scale/Scope**: 1 new service module; targeted extensions to the X-UI adapter,
the doctor surface, the client mutation paths and the subscription path resolver.
Approximately 6 existing files touched.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment |
| --- | --- |
| I. Canonical Service Identity | Not affected — no identity or key construction changes. |
| II. Lifecycle Generation | **Directly relevant.** Panel-side lifecycle automation (FR-024–027) could bypass EVE's generation barrier. Plan: detect and surface only; never write, never translate into EVE events. No generation semantics change. |
| III. Telemetry State | Not affected — state classification is untouched. |
| IV. Fetch Ordering | Satisfied — compatibility resolution is a per-server cached read, not part of the fetch ordering chain, and does not reorder or supersede fetch tickets. |
| V. Cross-Process Correctness | Satisfied by scoping: the compatibility cache is a **hint**, never a durable fact, and the one behaviour that must be correct (device-limit preservation) is read from the panel at mutation time, not from the cache. Per-worker cache divergence is bounded by TTL and is documented. No new process-local lock guards a cross-process invariant. |
| VI. Notification Delivery | Not affected. |
| VII. Renewal Safety | Satisfied — the renewal chokepoint (`panel/jobs/messaging.py:2995`) and generation/invalidation flow are untouched. The renewal *client mutation* gains preservation only. |
| VIII. Security | Satisfied — no weakening of `enforce_panel_transport`, `allow_insecure`, TLS verification or credential storage. Doctor output must not carry tokens (FR-035). Token scope is never escalated (FR-014). |
| IX. Public Request Paths | Satisfied — FR-040 forbids new synchronous panel calls on public subscription paths; path resolution reuses the already-cached panel settings read. |
| X. Database Evolution | Satisfied — **no schema change**, therefore no Alembic revision. Deliberate: compatibility metadata is derived, not stored. |
| XI. Observability | Satisfied — FR-032–034 require named degraded states and forbid reporting an unusable server as healthy. |
| XII. Testing | Satisfied — contract fixtures derived from the exact tagged upstream source/OpenAPI, integration tests through the real wiring, and preservation tests that assert *persisted* panel state, not just payload shape. |
| XIII. Production Acceptance | Satisfied — controlled acceptance against real disposable 3.7.x and 3.8.x panels; no production customer accounts touched. |

**Gate result: PASS.** No violations requiring justification.

Post-Phase-1 re-check: **PASS** — the design introduces no new persistence, no new
cross-process invariant, and no new public-path panel call.

## Project Structure

### Documentation (this feature)

```text
specs/001-3xui-37-38-compat/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 output — decisions and evidence
├── data-model.md        # Phase 1 output — entities and state
├── quickstart.md        # Phase 1 output — validation guide
├── contracts/           # Phase 1 output — compatibility contract
│   ├── compatibility-profiles.md
│   └── panel-version-detection.md
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (/speckit-tasks)
```

### Source Code (repository root)

```text
panel/
├── services/
│   └── xui_compat.py          # NEW — the single compatibility authority
├── adapters/
│   └── xui.py                 # version read, typed probe result, limit preservation
├── routes/
│   ├── doctor.py              # compatibility/doctor surface
│   ├── clients.py             # mutation paths (renew/edit/toggle/reset/rotate/add)
│   └── admin.py               # invalidation on server config / credential change
├── jobs/
│   └── refresh.py             # client cache; no new panel calls
└── models/
    └── core.py                # unchanged (no schema change)

app.py                         # re-export surface only
3XUI_V3_API.md                 # documentation correction
tests/
├── test_3xui_compat.py        # EXTENDED — the compatibility matrix
└── fixtures/xui/              # NEW — contract fixtures from exact upstream tags
```

**Structure Decision**: Single project. The compatibility logic lives in one new
service module under the existing `panel/services/` layer, because the dependency
direction is one-way (`core` <- `models` <- `services`/`adapters` <-
`routes`/`jobs`); `adapters/xui.py` may import the service, and routes/jobs may
import both. `panel/adapters/xui.py` keeps its existing public surface so no
caller is rewritten unnecessarily (FR-043).

## Complexity Tracking

> No constitution violations. This section is intentionally empty.
