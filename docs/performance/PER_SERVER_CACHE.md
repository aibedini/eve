# Per-server snapshot cache

## Problem

The dashboard snapshot is published to Redis as per-server blocks, and readers
merge only the blocks whose published version changed. The one gap was the
read-modify-write cycle for a single server: `serialized_server_snapshot_write`
(the path behind every client add, renew, remove and cached patch) force-loaded
the snapshot with

    load_snapshot_from_redis(force=True)

which ignores the per-server versions and therefore **downloads and decompresses
every server block** on every client mutation. Measured (baseline before this
phase, 12 servers / 18k clients): a forced full load decoded all 12 blocks in
441 ms and pulled ~1 MB from Redis; the decode step alone was 441 ms versus 30.6 ms
for a single block.

## Change

- `load_snapshot_from_redis(force=False, server_ids=None)` (and the unlocked
  helper) accept the servers a caller actually needs. Blocks for other servers keep
  the local copy instead of being re-downloaded; a server that has no local copy is
  still fetched, so a targeted load never drops data.
- `serialized_server_snapshot_write(server_id)` passes `server_ids=[server_id]`,
  so the per-server cycle only refreshes its own block.
- A targeted load deliberately does **not** mark the global snapshot version as
  loaded: the next full load still merges servers changed by other workers, using
  the per-server versions it tracks.
- Per-server version bookkeeping is updated only for the servers that were actually
  refreshed.
- `snapshot_metrics()` / `reset_snapshot_metrics()` count publishes, encoded and
  decoded blocks, bytes and full vs targeted loads. They are exposed read-only on
  `GET /api/doctor` as `checks.snapshot_cache`.

Nothing about publication changed: blocks are still written under a WATCH/CAS on
the manifest and the server revision keys, and the per-server distributed lock in
`serialized_server_snapshot_write` still serializes writers.

## Measured result

Reproduce with:

    python scripts/benchmark_cache.py --json docs/performance/per-server-cache.json

`per-server-cache.json` (12 servers, 30 inbounds per server, 50 clients per
inbound, in-memory Redis stand-in, no app import):

| Load | mean | blocks decoded | bytes from Redis |
|------|------|----------------|------------------|
| forced full (before) | 329.3 ms | 12 | 353 KB |
| targeted per-server (after) | 26.6 ms | 1 | 29 KB |

**~12x faster and 12x less Redis traffic per client mutation.** The absolute
numbers depend on the machine; the block count and the bytes ratio do not.

## Tests

`tests/test_per_server_redis.py`: a targeted load reads only the requested
server's key, keeps the other local blocks and updates only their own version
bookkeeping; a server without a local copy is still fetched; a full load decodes
only servers whose version changed; an unchanged version short-circuits; the
wrapper forwards `server_ids`; the write cycle calls the load with its own server;
publish metrics count zero blocks for a metadata-only publish and one for a single
changed server; and the measurement script produces a valid result.
