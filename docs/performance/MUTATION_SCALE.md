# Mutation scale: O(1) in the number of panels

## Problem

The mutation path used to be proportional to the install. Every panel write touched the
whole snapshot: `bump_server_revision`, a full Redis load (decode *every* panel's block),
a full re-serialize and publish, and the browser received a full snapshot because the
delta machinery had no per-panel revision to work with. At 12 panels a single client
mutation measured 441 ms of snapshot work; at 100 panels it would be seconds - and the
operator's edit would land in a UI that froze while it happened. (The per-server cache
that fixed the load half is `docs/performance/PER_SERVER_CACHE.md`; the delta half is
`docs/performance/DELTA_SYNC.md`.)

## Claim

The cost of **one client mutation** - commit to cache, what the browser receives, what
Redis is asked to do, and how often the fetcher polls a watched panel - does not grow
with the number of panels. Only the total snapshot size does, and that is the thing the
delta path exists to avoid sending.

## Measured

`python scripts/benchmark_mutation_scale.py --json docs/performance/mutation-scale.json`
(25 cached clients per panel, mutation p95 over 80 iterations per scale, cache reads
through the real Flask route):

| panels | clients | mutation p95 | mutation CPU p95 | delta bytes | full snapshot | cache read (full) | cache read (delta) | Redis ops/mutation | X-UI polls/min¹ |
|--------|---------|--------------|------------------|-------------|---------------|-------------------|--------------------|--------------------|-----------------|
| 10 | 250 | 11.9 ms | 15.6 ms | 17,707 | 175 KB | 41.4 ms | 11.9 ms | 26 | 300 |
| 50 | 1,250 | 9.6 ms | 15.6 ms | 17,708 | 876 KB | 94.0 ms | 9.9 ms | 26 | 630 |
| 100 | 2,500 | 10.6 ms | 15.6 ms | 17,708 | 1.75 MB | 158.9 ms | 10.8 ms | 26 | 680 |

¹ polls/minute simulated with the real per-server policy: the panels on screen every
`EVE_SERVER_POLL_ACTIVE_SECONDS` (2 s, up to `EVE_SERVER_POLL_WATCH_LIMIT` = 20), the
rest every `EVE_SERVER_POLL_IDLE_SECONDS` (45 s). The naive "poll everything every 2 s"
figure for comparison is 300 / 1,500 / 3,000. See `docs/performance/SERVER_POLLING.md`.

The verdicts the benchmark enforces (non-zero exit when one breaks) - the key in
`--json` is in parentheses:

| Verdict (`verdicts` key) | Measured | Bound |
|--------------------------|----------|-------|
| mutation p95 ratio, 100 panels / 10 panels (`mutation_is_flat`) | 0.89x | <= 2x |
| delta bytes ratio, 100 / 10 (`delta_is_one_block`) | 1.00x | <= 2x |
| full snapshot ratio, 100 / 10 (`full_snapshot_grows_with_the_install`) | 10.0x | >= 4x (it must grow) |
| Redis ops per mutation (`redis_ops_are_constant`) | 26 at every scale | identical |
| outbound panel calls per cache read (`cache_read_makes_no_panel_call`) | 0 | 0 |
| polls/min while 20 panels are watched (`per_server_polling_is_bounded`) | 680 at 100 panels | < 3,000 (naive) |

The full cache read does grow (41.4 ms -> 158.9 ms for 10x the clients): that is the
snapshot's own size, and it is why the browser asks with `?since=` - the delta read stays
at ~10-12 ms and ~17.7 KB, the size of one panel's block, because a mutation only moves
one panel's revision. Run-to-run noise on a shared machine moves the p95 by a few ms; the
committed `docs/performance/mutation-scale.json` is the record for a given run.

## Why it is O(1)

* `patch_cached_client` patches the matching rows of **one** panel block, recomputes that
  panel's stats and publishes that panel's block (`publish_snapshot_to_redis([server_id])`).
  Nothing iterates the other panels.
* A Redis write lock (`eve:server_snapshot_write:<id>`) plus a targeted
  `_load_snapshot_from_redis_unlocked(force=True, server_ids=[id])` means the write path
  decodes one block, not the install.
* `snapshot_delta` records the `(server_id, inbound_id)` pair that changed, and
  `build_sync`/`select_inbounds` return exactly those blocks, so the payload the browser
  merges is one block regardless of install size.
* The read path is served from the in-process snapshot + Redis and makes **zero** panel
  calls; the fetcher, not the request, owns X-UI traffic (`docs/performance/SERVER_POLLING.md`).

## Limits

* The delta stays one block only while fewer than `MAX_DELTA_KEYS` inbounds changed since
  the client's revision; a mass change (a bulk edit, a full refresh) falls back to a full
  snapshot by design.
* Memory is not bounded by this work: the snapshot itself is O(clients) in the process
  (`python_peak_kb` / `process_peak_rss_kb` are reported by the benchmark for that
  reason), which is what `docs/performance/SERIALIZATION.md` and the panel limits address.
* Redis ops are counted through the real call sites with a counting fake client; a real
  deployment additionally pays the network round trip per op (26 ops/mutation), which is
  the per-server cache's whole point.
* sqlite, in-process, no other traffic: the ratios are the claim, not the absolute ms.

## Tests

`tests/test_mutation_scale.py` runs the benchmark in quick mode and fails when the
mutation p95, the delta size, the Redis op count, the zero-panel-call guarantee or the
poll bound regresses, when the artifact loses a field, or when the CI wiring disappears.
