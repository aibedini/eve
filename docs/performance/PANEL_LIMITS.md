# Bounded, coalesced panel access

## Problem

Nothing bounded how many X-UI sessions Eve could open at once, and nothing
deduplicated identical work: N concurrent callers asking for the same server
triggered N panel fetches. The concurrency was implicitly limited only by the
gunicorn worker threads, so a burst of dashboard/client operations could hammer a
panel with parallel logins and reads, and a slow panel would multiply that load.

## Change

New module `panel/core/panel_limits.py`:

* `coalesce(key)` - single flight per key. The first caller becomes the leader and
  runs the work, everyone else waits for the leader's result and skips the work
  (assign the value to `slot.result`). A leader error is re-raised for followers.
* `panel_slot()` - a process-wide concurrency cap (bounded semaphore) that the
  leader of every flight and every background fan-out worker shares.
* Counters (started/coalesced/completed/rejected/timed_out/in_flight/max_in_flight)
  exposed on `GET /api/doctor` as `checks.panel_limits`.

Wiring:

* `fetch_and_update_server_data(server_id)` is now single-flight per server
  (`xui-fetch:<id>`): duplicate callers reuse the running fetch instead of hitting
  the panel again.
* `app.fetch_worker` (the background fan-out worker) holds a `panel_slot()` while
  it authenticates and reads the panel, so the fan-out and request-triggered
  fetches share one cap.
* The fan-out executor size is configurable (`EVE_REFRESH_WORKERS`) instead of a
  hardcoded 5.
* The single-server fetch now performs its whole snapshot read-modify-write
  (replace the server block, update statuses/stats) under `GLOBAL_REFRESH_LOCK`,
  so two fetches for different servers cannot lose each other's block.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_PANEL_CONCURRENCY | 12 | maximum simultaneous panel fetches per process |
| EVE_PANEL_FETCH_WAIT_SECONDS | 10 | how long a caller waits for its turn |
| EVE_REFRESH_WORKERS | 5 | worker threads for the background fan-out |

When the cap is reached the caller raises `PanelBusy` (HTTP callers can surface it
as "panel busy, retry"); when a follower cannot get the leader's result in time it
raises the same error instead of starting a second fetch.

## Measured

`scripts/benchmark_panel_limits.py` (`--json docs/performance/panel-limits.json`),
20 callers asking for the same panel with a 50 ms fetch:

| | panel fetches | wall time |
|---|---|---|
| without single flight | 20 | 59.0 ms |
| with single flight | **1** (19 coalesced followers) | 53.4 ms |
| concurrency cap 4 over 16 distinct tasks | - | max 4 simultaneous |

## Limitations

* The cap covers the fetch paths that go through `fetch_worker` and
  `fetch_and_update_server_data`. Route handlers that build their own panel
  session for a one-off read are not individually throttled yet; they still share
  the session cache and are bounded by worker threads. Extending the cap to those
  call sites (or adding a per-host circuit breaker) is a later phase.
* Coalescing keys are per server id; two different servers still fetch in parallel,
  up to the cap.

## Tests

`tests/test_panel_limits.py`: duplicate callers run the work once and share the
result; a follower times out with `PanelBusy`; a leader error is re-raised for
followers and the follower never runs the work; the global cap bounds simultaneous
fetches; `panel_slot` rejects once the cap is held; configuration comes from the
environment and invalid values fall back; `fetch_and_update_server_data` is
single-flight at the function level; and the measurement script produces a valid
result.
