# Reseller refresh projection

## Problem

`GET /api/refresh` derives a private view for a reseller: the shared snapshot is
filtered down to the inbounds and clients that reseller owns. The handler started
that derivation with `copy.deepcopy(GLOBAL_SERVER_DATA)` — a full deep copy of the
shared snapshot before any filtering. On the benchmark dataset (12 servers x 30
inbounds x 50 clients = 18,000 clients, roughly 8 MB serialised) that copy alone
cost about 1.5 s of the 1.76 s response, and the reseller saw it on every poll that
carried a change.

Measured baseline: `api_refresh_reseller` mean 1,755.8 ms, p95 2,600.8 ms
(`docs/performance/serialization-before.json`, commit `23d1ee0`).

## Change

`panel/routes/dashboard.py` no longer deep-copies. The loop that filters the
inbounds already copies nothing shared: it only annotates the inbounds it is going
to return (`total_up`/`total_down` set to `"---"`, `clients` replaced by the
filtered list, `client_count` updated). It now takes a **shallow copy per visible
inbound** (`inbound = dict(source_inbound)`) instead of a deep copy of the whole
snapshot:

* the shared snapshot keeps its own inbound dicts and client lists, so a per-user
  view cannot leak into the cache or another user;
* client and server dicts are shared with the response because they are read-only;
* the enrichment the superadmin path performs in place is untouched;
* the `?debug_timing=1` block now reports `projection` instead of `deepcopy` (it
  had no other consumer).

## Result

Measured with the same harness and dataset (`docs/performance/serialization-after.json`):

| scenario | before mean | after mean | before p95 | after p95 | bytes |
|----------|-------------|------------|------------|-----------|-------|
| `api_refresh_reseller` | 1,755.8 ms | **289.6 ms** | 2,600.8 ms | 432.5 ms | 162,607 (same) |
| `api_refresh_superadmin` | 404.0 ms | 405.7 ms | 424.0 ms | 427.5 ms | 8,231,586 (same) |
| `api_refresh_superadmin_unchanged` | 5.1 ms | 4.7 ms | 7.5 ms | 7.7 ms | 140 (same) |
| `api_refresh_superadmin_delta` | 104.2 ms | 86.6 ms | 141.1 ms | 120.9 ms | 27,181 (same) |

6.1x faster on the reseller path, with an identical response size, which is the
external evidence that the projection produces the same payload.

## Residual risk

* The response now shares client and server dicts with the shared snapshot. Nothing
  in the request path mutates them; the regression test asserts the snapshot (and
  its inbound client lists) is byte-identical after both a reseller and a
  superadmin refresh, which is what would catch a future mutation.
* The remaining 290 ms is the per-reseller ownership query plus the client loop;
  it still scales with the number of inbounds the reseller can see.

## Verification

`tests/test_reseller_projection.py` (2 tests): the reseller view filters clients by
ownership, zeroes the inbound totals, counts only owned clients and sums only their
traffic, and leaves `GLOBAL_SERVER_DATA["inbounds"]` exactly as it was (including
the original `total_up` and the full client list); the superadmin view still
returns every client and is idempotent on a second call.
