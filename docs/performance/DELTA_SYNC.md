# Delta sync for /api/refresh

## Problem

Every dashboard poll re-serialized and re-transferred the whole snapshot. The
phase 10 baseline measured 540 ms and 8.2 MB per poll at 18k clients
(`baseline-2.5.120.json`), and a reseller poll additionally paid ~2 s for the
per-user deepcopy and filter. Almost every poll carries no new information.

## Protocol

The endpoint now accepts a snapshot revision and answers with only what changed:

| Request | Answer |
|---------|--------|
| no `since` | `sync.mode = full` - the complete snapshot (unchanged behaviour) |
| `?since=<rev>` (or the `X-Eve-Snapshot` header) with the current revision | `sync.mode = unchanged` - a ~140 byte envelope, no `inbounds` |
| `since` one of the retained revisions | `sync.mode = delta` - `inbounds` holds only the changed inbounds, `removed` the deleted `[server_id, inbound_id]` keys, plus fresh `stats`/`servers` |
| unknown, future or pruned revision | `sync.mode = full` with `sync.reason` (`no_revision`, `unknown_revision`, `history_gap`, `too_many_changes`) |

Every answer carries `sync.revision` and `sync.last_update`. A client that never
sends `since` keeps the old behaviour, so the change is backward compatible.

The dashboard sends `since` on silent polls and merges the delta in place
(replace changed inbounds, drop removed ones, append new ones); an explicit
Refresh click always requests a full snapshot.

## How a revision is detected

`panel/core/snapshot_delta.py` keeps a bounded history (30 revisions) of the
canonical per-inbound digests:

- Writers bump `GLOBAL_SERVER_DATA['last_update']` on every mutation, so a changed
  timestamp marks the snapshot dirty; `mark_dirty()` lets a writer say so
  explicitly.
- The fetcher and the cached-client helpers pass the server ids they replaced
  (`mark_dirty(server_ids=[...])`), so a change re-hashes only that server's
  block. The full pass over 18k clients costs ~0.6 s, the hinted pass ~55 ms;
  an unchanged poll is an in-memory comparison and costs ~0.01 ms.
- A digest of `stats`/`servers_status`/`is_updating` is tracked too, so a
  server-status-only change still advances the revision and reaches clients as a
  delta with an empty `inbounds` list.
- With several gunicorn workers the revision counter and the history are shared
  through Redis (best effort); a revision a worker does not know falls back to a
  full snapshot. Without Redis each worker keeps its own counter (safe, just
  fewer deltas when a client moves between workers).

## Measured result

Same process, same dataset as the phase 10 baseline (18k clients), 20 samples per
scenario, phase 11 commit:

| Scenario | before | after |
|----------|--------|-------|
| full poll (`/api/refresh`) | 548 ms, 8.2 MB | 541 ms, 8.2 MB (unchanged path) |
| poll with no changes | - (always full) | **7.2 ms, 140 B** |
| poll with one changed inbound | 541 ms, 8.2 MB | **135.7 ms, 27.2 KB** |
| reseller poll with no changes | 2.09 s, 163 KB | **7 ms, ~140 B** (deepcopy + filter skipped) |
| fingerprint pass, one server (30 inbounds) | - | 55 ms |
| fingerprint pass, whole snapshot | - | 597 ms (only for an unhinted writer or a Redis merge) |

The harness scenarios `api_refresh_superadmin_unchanged`,
`api_refresh_superadmin_delta`, `snapshot_delta_sync_hinted` and
`snapshot_delta_sync_unhinted` track these going forward; the raw report is
`after-2.5.121.json`. The full path, the reseller path, payload bytes and SQL
counts show no regression in the comparison against `baseline-2.5.120.json`.

## Residual costs and limits

- The first request after a change pays the fingerprint pass: ~55 ms for a hinted
  server, ~600 ms when a writer (or a cross-worker Redis snapshot merge) cannot
  name the changed servers. The benefit is that this cost is paid once per change
  per worker instead of by every poll from every client.
- Hints are per server, so a single changed inbound re-hashes its server's whole
  block; inbound-level hints are a possible later refinement.
- The reseller full path is still a deepcopy plus per-client filter when something
  changed; only the no-change poll is free. Per-server snapshots and lock removal
  are the later phases that target it.
- A client that never sends `since` (older dashboards, third-party callers) keeps
  the old cost.

## Tests

`tests/test_snapshot_delta.py`: revision/full/delta/unchanged decisions, removed
keys, hint-limited rehashing, hinted removals, metadata-only changes, history gaps,
too-many-changes fallback, Redis-shared revisions across two simulated workers,
and the route behaviour for a superadmin and a reseller (full, unchanged, delta,
header-carried revision, unknown revision).
