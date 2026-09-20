# Implementation Plan: V3 Client Normalization

**Branch**: `main` | **Date**: 2026-09-20 | **Spec**: [spec.md](./spec.md)

**Input**: `specs/002-v3-client-normalization/spec.md`

## Summary

Correct compressed-snapshot telemetry, add a bounded background retention probe, and
replace duplicated v3 client rows with one server-scoped entity plus small inbound
memberships. Legacy blocks remain compatible; per-server Redis blocks remain independent;
expanded views exist only for requested responses or deltas.

## Technical Context

**Language/Version**: Python 3.11 production; repository-supported Python range

**Primary Dependencies**: Flask, SQLAlchemy, redis-py, stdlib gzip/json, Linux `/proc`

**Storage**: Existing PostgreSQL and Redis per-server blocks; no database schema change

**Testing**: pytest/unittest via `.venv-test`; Tier 1 per edit and Tier 2 once before handoff

**Target Platform**: Linux systemd, separate Web and Background processes

**Project Type**: Flask web service with Redis-backed cross-process snapshots

**Performance Goals**: One retained v3 entity per unique client; mutation O(memberships); no publish/hydrate peak regression; measured saving at 52,372 memberships / 11,766 clients

**Constraints**: Preserve public response shape; no expanded fleet cache; no raw_client/formatted cleanup; bounded PII-free diagnostics; safe old-format reads

**Scale/Scope**: 7 servers, 92 inbounds, two process copies, about 10.02 MiB compressed

## Constitution Check

*GATE: Passed before and after design.*

- **I**: UUID stays primary; email is a scoped fallback and never overrides UUID identity.
- **III**: Canonical service-state calculation is unchanged.
- **IV**: Issue-order tickets and server revision CAS remain the publication barrier.
- **V**: Redis remains the shared boundary; local structures guard local memory only.
- **VIII**: Diagnostics expose counts/bytes only, never payload or customer values.
- **IX**: Bounded materialization adds no live panel calls.
- **XI**: Telemetry is fixed first and unavailable measurements remain explicit.
- **XII**: Format, mutation, delta, and legacy behavior receive regression coverage.
- **Versioning**: One `APP_VERSION` patch bump and matching changelog entry.
- **Budget**: No Tier 3/full-suite run locally.

## Project Structure

```text
specs/002-v3-client-normalization/       # feature artifacts
app.py                                   # inbound construction/compat exports
panel/core/memory_report.py              # telemetry integration
panel/core/redis_client.py               # block publication/hydration
panel/core/snapshot_delta.py             # dependency-aware deltas
panel/core/snapshot_model.py             # entities/memberships/materializers
panel/core/memory_probe.py                # bounded lifecycle samples
panel/jobs/schedulers.py                  # checkpoints and normalized commit
panel/jobs/refresh.py                     # mutations/server replacement
panel/routes/dashboard.py                 # requested-view materialization
scripts/measure_snapshot_footprint.py     # old/new measurement
tests/                                    # focused unit/integration coverage
```

**Structure Decision**: Extend `panel/core`. Existing jobs/routes/services consume the
shared-reference compatibility graph, so a broad reader rewrite is unnecessary; the HTTP
dashboard boundary materializes requested rows. No new module in `panel/` imports `app` at
module load time. `app.py` keeps compatibility exports.

## Complexity Tracking

No constitution violations require justification.
