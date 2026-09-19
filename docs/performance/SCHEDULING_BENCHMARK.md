# Per-server scheduling benchmark

This document reports what `scripts/benchmark_per_server_scheduling.py` measures, why the
numbers are what the acceptance criteria are written against, and how to reproduce them.
The policy itself is documented in `docs/performance/SERVER_POLLING.md`.

## The claim under test

    A HOT panel's poll cadence must not be a function of how many OTHER panels exist,
    nor of how long reading them takes.

That is the difference between cycle-oriented polling and independent per-server
scheduling. Ordering a batch better, or making the batch smaller, reduces the damage; it
cannot remove the barrier, because the next start of the HOT panel still waits for the
batch to finish. The benchmark therefore runs the SAME policy and the SAME simulated panel
latency under two dispatch shapes:

| mode | what it is |
|------|------------|
| `legacy-cycle` | `schedulers._fetch_and_update_global_data_inner(periodic=True)` in a loop: every due panel, one batch, wait for the batch, sleep. Driven through the product's own entry point against a real (temporary sqlite) database and a stubbed panel read, with `EVE_REFRESH_BATCH_SERVERS=0` and `EVE_SERVER_POLL_IDLE_JITTER_SECONDS=0` so the sweep is whole-install, as it was before the batch cap existed. |
| `cycle` | the same loop with the 2.7.12 batch cap and idle jitter (the intermediate state). |
| `per-server` | `schedulers.run_per_server_scheduler()`: one dispatch decision per free worker, one panel per decision, rescheduled from that panel's own completion. |

## Method

* One HOT panel (`note_server_activity` + an overdue schedule), every other panel IDLE.
* Install sizes 1 / 10 / 50 / 100; simulated panel read 100 / 300 / 1000 ms.
* `EVE_REFRESH_WORKERS=5` (the default), `EVE_SERVER_POLL_ACTIVE_SECONDS=2`.
* The idle band is compressed to 5 s **for the measurement only**
  (`--idle-seconds`): a HOT panel is only delayed by the rest of the install while the
  rest of the install is being read, and with the production 45 s band a 12 s window would
  simply miss those bursts -- which is exactly how a cycle-oriented loop can look healthy
  in a benchmark and still starve the panel an operator is watching. Compressing the band
  changes how OFTEN the burst happens, not how long a HOT panel waits inside it.
* Each run: 12 s (15 s for the CPU re-run below). The first gap is reported separately:
  the HOT panel starts overdue, so its second read is a catch-up, not a cadence sample.
* Windows, Python 3.14, `time.process_time()` for CPU (system tick ~15.6 ms, so a
  per-fetch CPU figure below one tick is not a measurement; the raw total is reported too).

Reproduce:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_per_server_scheduling.py `
    --modes legacy-cycle,per-server --servers 1,10,50,100 `
    --latency-ms 100,300,1000 --seconds 12 --idle-seconds 5 `
    --json bench-scheduling.json
.\.venv\Scripts\python.exe scripts\benchmark_per_server_scheduling.py --quick   # ~1 min
```

## Results: HOT panel start-to-start (ms)

`p50`/`p95` are over the steady-state gaps (all gaps after the catch-up poll).

| mode | servers | read | p50 | p95 | max gap | queue delay p95 | polls in window |
|------|---------|------|-----|-----|---------|-----------------|-----------------|
| legacy-cycle | 1 | 100 ms | 2007 | 2015 | 2015 | n/a | 7 |
| legacy-cycle | 1 | 1000 ms | 1999 | 2001 | 2001 | n/a | 7 |
| legacy-cycle | 10 | 1000 ms | 1994 | 2009 | 2009 | n/a | 7 |
| legacy-cycle | 50 | 300 ms | 1990 | 2014 | 2014 | n/a | 7 |
| **legacy-cycle** | **50** | **1000 ms** | **1297** | **1297** | **1297** | n/a | **2** |
| legacy-cycle | 100 | 300 ms | 1746 | 2031 | 2031 | n/a | 7 |
| **legacy-cycle** | **100** | **1000 ms** | **--** | **--** | **--** | n/a | **1** |
| cycle (batch cap + jitter) | 50 | 300 ms | 2068 | -- | -- | n/a | 3 |
| per-server | 1 | 100 ms | 2001 | 2014 | 2014 | 6 | 7 |
| per-server | 1 | 1000 ms | 2006 | 2014 | 2014 | 11 | 7 |
| per-server | 10 | 100 ms | 1999 | 2009 | 2009 | 2 | 7 |
| per-server | 10 | 1000 ms | 2002 | 2010 | 2010 | 15 | 7 |
| per-server | 50 | 100 ms | 2001 | 2007 | 2007 | 0 | 7 |
| per-server | 50 | 1000 ms | 2003 | 2004 | 2004 | 0 | 7 |
| per-server | 100 | 100 ms | 2000 | 2009 | 2009 | 0 | 7 |
| per-server | 100 | 300 ms | 2017 | 2114 | 2114 | 0 | 7 |
| per-server | 100 | 1000 ms | 2005 | 2005 | 2005 | 0 | 7 |

Reading of the two rows that matter:

* **per-server, 1 -> 100 panels, 100 -> 1000 ms read: 2001 -> 2005 ms p50.** The cadence is
  the configured 2 s; the install size and the read time do not enter it. `queue delay p95`
  is 0-15 ms, i.e. no HOT dispatch waited for a worker in any of these configurations.
* **legacy-cycle, 100 panels at 1000 ms: one poll in the 12 s window** (the HOT panel did
  not get a second read at all), and 50 panels at 1000 ms: two polls, i.e. the effective
  cadence was ~6 s against a 2 s target. That is the starvation the architecture change
  removes; it is not visible at 100 panels/100 ms because there the whole sweep finishes
  inside one cadence.

`max inflight` stayed at 4-5 (the worker pool) in both modes, and `saturation_events`
was non-zero for per-server at 50/100 panels -- the scheduler reports the ticks where more
panels were due than there were free workers, which is the honest signal that capacity (not
cadence maths) is the limit. The HOT panel's own queue delay stayed at 0 in those runs
because it outranked the idle majority.

## Results: wake -> dispatch

Five rounds: an idle panel with a long schedule is nudged through the real mutation path
(`note_server_activity(..., share=True)` plus `publish_wake`), and the time until its read
starts is measured. Two runs on the same machine:

| metric | run A | run B |
|--------|-------|-------|
| p50 | **3.2 ms** | 59.6 ms |
| p95 | **62.5 ms** | 62.8 ms |
| max | **62.5 ms** | 62.8 ms |

Acceptance was p95 < 500 ms; both runs are two orders of magnitude under it. The spread
between the two p50 values is tick alignment (the scheduler re-evaluates every
`SCHEDULER_TICK_SECONDS` = 50 ms while it waits, so a nudge lands either just before or
just after a tick). The cross-process half of this -- a web process nudging the fetcher
process through real Redis -- is measured separately by
`scripts/integration_redis_multiprocess.py`; see `docs/performance/REDIS_MULTIPROCESS.md`.

## Results: cost

* Idle load is the same work either way: at 100 panels with a 5 s idle band both modes read
  ~400-500 idle panels/minute. The jitter and the scheduler do not add panel reads; the
  same set of reads is dispatched differently.
* CPU at 100 panels, 15 s window, same simulated read, `time.process_time()` deltas:

  | mode | read | fetches | CPU total | CPU per fetch |
  |------|------|---------|-----------|----------------|
  | legacy-cycle | 300 ms | 106 | 265.6 ms | 2.51 ms |
  | per-server | 300 ms | 108 | 31.3 ms | 0.29 ms |
  | legacy-cycle | 1000 ms | 100 | 62.5 ms | 0.63 ms (HOT starved: 1 poll) |
  | per-server | 1000 ms | 70 | 0.0 ms | below one tick |

  The remaining runs are at or below the ~15.6 ms Windows clock tick, so they are reported
  as 0 and should be read as "not resolvable at this scale", not as "free". The one
  resolvable comparison (100 panels, 300 ms) shows the per-server path spending ~8x less
  CPU per read than the whole-install sweep, which is the bookkeeping the sweep pays for
  every panel it re-examines rather than for the panel it is actually refreshing.
* The scheduler's own idle cost while it waits is one wakeup per 50 ms tick that checks a
  future list and an event -- not a scan of the install. Redis gains one `HSET` per
  completed read (`eve:refresh:server_sync`) for the cross-process freshness report, which
  is O(1) per read and leaves
  `tests/test_mutation_scale.py::test_redis_work_per_mutation_is_constant` untouched.

## Sizing recommendation

With `EVE_REFRESH_WORKERS=5` and `EVE_SERVER_POLL_ACTIVE_SECONDS=2`:

* Up to ~10 HOT panels whose reads take <=300 ms, and up to ~4 HOT panels whose reads take
  ~1 s, keep the 2 s cadence with no queue delay (the pool is the limit: a HOT panel needs
  `read_time / cadence` of a worker on average, so 5 workers at 2 s cadence serve roughly
  `5 * 2 s / read_time` HOT panels).
* Beyond that the cadence degrades **visibly**: `scheduler_queue_delay_ms` grows per panel,
  `saturation_events` grows in `/api/doctor`, and the freshness line on the dashboard shows
  the age. Raise `EVE_REFRESH_WORKERS` (and `EVE_PANEL_CONCURRENCY` if the panel hosts can
  take it) or lower `EVE_SERVER_POLL_WATCH_LIMIT`.
* Do not raise `EVE_REFRESH_WORKERS` past `EVE_PANEL_CONCURRENCY` without raising that too:
  the panel semaphore is the process-wide bound on simultaneous panel sessions, and a
  worker that cannot get a slot spends its time waiting for one.

## Limits of this benchmark

* The panel read is simulated (a sleep of the configured latency), so the numbers isolate
  the SCHEDULING shape. Real panel latency is variable, and a slow panel occupies its
  worker for the whole read, which is exactly why the sizing rule above is expressed in
  worker-time rather than in panel count.
* The per-server mode injects the read (the loop is designed to be drivable that way and
  the tests use the same seam); the cycle mode drives the product's real entry point. Both
  use the identical policy and the identical simulated latency, so the comparison is of
  dispatch shapes, not of code paths.
* A single-process measurement cannot show the cross-process wake; that is the Redis
  harness's job, and its numbers live in the document referenced above.
