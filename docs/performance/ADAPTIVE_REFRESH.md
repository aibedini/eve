# Adaptive refresh cadence

## Problem

The background fetcher polled every panel every 30 seconds, unconditionally:
2,880 fan-outs per day, and with 12 panels that is ~34,560 panel requests per day,
almost all of them while nobody is looking at the dashboard. The cadence was also
the only knob: no way to slow down at night, and no notion of "someone is here, be
fresher".

## Change

New module `panel/core/refresh_policy.py`:

* activity levels derived from dashboard traffic - **active** (activity within
  `EVE_REFRESH_ACTIVE_WINDOW_SECONDS`, default 120 s), **recent** (within the recent
  window, default 600 s) and **idle**;
* a target interval per level (30 / 90 / 300 s by default) and a hard
  `EVE_REFRESH_MAX_STALENESS_SECONDS` (900 s) that clamps the sleep so the snapshot
  never ages past it;
* `record_activity()` sets a per-process Event (so a local fetcher reacts
  immediately) and publishes a timestamp to Redis (`eve:refresh:last_activity`) so
  split web/background deployments share the signal;
* `should_fetch_now(snapshot_age)` decides whether the fetcher needs to run.

Wiring:

* `background_data_fetcher` fetches when the snapshot is stale for the current
  level, otherwise sleeps in bounded slices (`EVE_REFRESH_ACTIVITY_POLL_SECONDS`,
  default 60 s) so remote activity is noticed promptly.
* `GET /api/refresh` records activity (throttled to once per second) - the client
  keeps polling; the server simply decides how often it needs to refetch.
* A connected SSE viewer counts as activity, throttled to once per minute so a
  forgotten tab cannot pin the fetcher to the fast cadence.
* `GET /api/doctor` exposes `checks.refresh_policy` (level, ages, intervals, next
  sleep).

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_REFRESH_INTERVAL_SECONDS | 30 | active target |
| EVE_REFRESH_RECENT_SECONDS | 90 | recently-active target |
| EVE_REFRESH_IDLE_SECONDS | 300 | idle target |
| EVE_REFRESH_ACTIVE_WINDOW_SECONDS | 120 | activity younger than this is active |
| EVE_REFRESH_RECENT_WINDOW_SECONDS | 600 | activity younger than this is recent |
| EVE_REFRESH_MAX_STALENESS_SECONDS | 900 | never let the snapshot get older |
| EVE_REFRESH_ACTIVITY_POLL_SECONDS | 60 | idle sleep slice, i.e. how quickly shared activity is noticed |

## Measured

`scripts/benchmark_refresh_policy.py` (`--json docs/performance/refresh-policy.json`)
simulates one day: 8 hours with an operator watching (a dashboard request every
30 s), 16 hours idle, 12 panels.

| Policy | fan-outs/day | panel requests/day |
|--------|--------------|--------------------|
| fixed 30 s (before) | 2,880 | 34,560 |
| adaptive | **1,159** | **13,908** |

**59.8% fewer panel fan-outs** over that day (time by level: active 8.0 h,
recent 8 min, idle 15.8 h). A returning operator does not wait for the idle
cadence: their request records activity, which wakes the local fetcher immediately
(or is seen within the next 60 s slice in a split deployment) and the max staleness
bounds how old the data can be even if that signal is lost.

## Limits

* The wake Event is per process; split web/background roles rely on the shared
  Redis timestamp and the poll slice, so the reaction is bounded by
  `EVE_REFRESH_ACTIVITY_POLL_SECONDS` (60 s), not instant.
* Activity is a proxy for "someone is watching": background jobs and bots do not
  record it, so the idle cadence applies unless a dashboard is open.
* Per-panel backoff still applies inside a cycle, and a cycle is skipped when
  another fetch holds the fetch guard (phase 15).

## Tests

`tests/test_refresh_policy.py`: level windows, configurable targets, throttling,
shared Redis activity, timestamp parsing, interval clamping (staleness and the
5 s floor), the `should_fetch_now` matrix, the sleeper being woken by activity,
the status payload, the fetcher loop (fresh snapshot does not fetch and sleeps a
bounded slice; stale and missing snapshots fetch immediately) and the simulation
script.
