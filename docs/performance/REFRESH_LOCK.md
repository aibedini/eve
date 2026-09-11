# Scoped refresh locks

## Problem

`GLOBAL_REFRESH_LOCK` protects the in-memory dashboard snapshot, but the fetch
callers held it for the **whole panel fan-out**:

```python
with GLOBAL_REFRESH_LOCK:                       # background_data_fetcher,
    fetch_and_update_global_data(force=False)   # _run_refresh_job, usage rollup
```

Fetching every enabled panel over the network takes seconds to minutes, so any
request or worker that merely needed to read or mutate the snapshot queue behind
it. Measured baseline (6 servers, a 0.2 s stub per panel): a concurrent
acquisition of the lock waited **435 ms** - the entire fetch - while the fan-out
ran.

## Change

- `GLOBAL_FETCH_LOCK` (new, in `panel/core/redis_client.py`) serializes panel
  fan-outs. `fetch_guard(wait_seconds)` is the context manager both fetch paths
  use; the lock is released as soon as the fetch returns and is **never** held by
  a reader.
- `fetch_and_update_global_data(..., wait_seconds=0)` acquires the guard itself,
  then runs `_fetch_and_update_global_data_inner`. It returns `False` when
  another fetch holds the slot (background cycle) instead of queueing.
- The fan-out body now takes `GLOBAL_REFRESH_LOCK` only for its short in-memory
  commits: the seed read, `_commit_snapshot()`, `_publish_dirty()` (Redis) and
  the `is_updating` flips. The working maps it builds while fetching are local.
- The callers no longer wrap the fetch in the snapshot lock:
  `background_data_fetcher`, the manual refresh job (`_run_refresh_job`) and the
  usage-rollup cold start.
- The manual job passes `wait_seconds=900` (a user asked for it, so it may wait
  for a background cycle) and reports "Another refresh is already running" when
  the slot stays busy; its single-server path and the reachability refresh take
  the same guard, so no two fan-outs overlap.
- `_update_reachability_status` now takes the lock only for its write-back.

## Measured result

Same synthetic setup (6 servers x 0.2 s), fetch in one thread, a reader acquiring
the lock in a loop:

| | before | after |
|---|---|---|
| reader worst block during a fan-out | 435.3 ms | **0.05 ms** |
| fetch duration | 440 ms | 445 ms |
| lock sections inside the fetch | 1 x whole fetch | 14, max 0.10 ms, total 0.52 ms |

The numbers are reproducible with `scripts/benchmark_locks.py`
(`--json docs/performance/refresh-lock.json`).

## Tests

`tests/test_refresh_lock_scoping.py`: a reader acquires the snapshot lock in
under 200 ms while a stubbed 10 s fan-out is in flight; a second fetch while one
runs returns False immediately without contacting any panel; `fetch_guard` reports
a busy slot and releases it; a completed fetch still updates
`servers_status`/`last_update` and clears `is_updating`; and
`background_data_fetcher` no longer references `GLOBAL_REFRESH_LOCK`.
