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

## Two different questions, measured separately

| | A) INSTALL SIZE SCALING | B) HOT CONCURRENCY CAPACITY |
|---|---|---|
| setup | **1 HOT panel** + N-1 IDLE | **M panels HOT at the same time** |
| question | does the HOT panel's cadence depend on the install size? | when does the worker pool stop serving them at their cadence, and how does it degrade? |
| harness | `benchmark_per_server_scheduling.py` (wall clock, this document) | `benchmark_hot_capacity.py` (virtual time, below) |

The earlier version of this document reported a capacity number from a measurement that
only ever ran **1 HOT panel among N-1 IDLE panels**. That is experiment A, and it says
nothing about how many panels can be HOT at once — reading "100 panels held the 2 s
cadence" as "100 HOT panels are served every 2 s" is exactly the misreading this section
exists to prevent.

## Results: A) install size scaling (1 HOT, rest IDLE)

HOT panel start-to-start, wall clock, `EVE_REFRESH_WORKERS=5`, cadence 2 s. From
`benchmark_per_server_scheduling.py`:

| mode | servers | read | p50 | p95 | queue delay p95 | polls in window |
|------|---------|------|-----|-----|-----------------|-----------------|
| legacy-cycle | 1 | 1000 ms | 1999 | 2001 | n/a | 7 |
| **legacy-cycle** | **50** | **1000 ms** | **1297** | **1297** | n/a | **2** |
| **legacy-cycle** | **100** | **1000 ms** | **—** | **—** | n/a | **1** |
| per-server | 1 | 1000 ms | 2006 | 2014 | 11 ms | 7 |
| per-server | 50 | 1000 ms | 2003 | 2004 | 0 ms | 7 |
| per-server | 100 | 1000 ms | 2005 | 2005 | 0 ms | 7 |
| per-server | 100 | 300 ms | 2017 | 2114 | 0 ms | 7 |

* **per-server: 2.00–2.02 s p50 at every install size and read time** — the cadence is the
  configured one and the install does not enter it.
* **legacy-cycle at 100 panels x 1000 ms: ONE poll in a 12 s window** (the HOT panel never
  got a second read), and at 50 x 1000 ms the effective cadence was ~6 s. That starvation
  is what the architecture change removes.

The virtual-time harness reproduces A deterministically and instantly (Tier 3 script),
including the 0 → 99 idle-panel sweep:

| idle panels | HOT p50 | HOT p95 | queue delay p95 |
|---|---|---|---|
| 0 | 2000 ms | 2000 ms | 0 ms |
| 9 | 2000 ms | 2000 ms | 0 ms |
| 49 | 2000 ms | 2000 ms | 0 ms |
| 99 | 2000 ms | 2000 ms | 0 ms |

## Results: B) HOT concurrency capacity (M HOT at once)

Theory first: a HOT panel of cadence `c` whose read takes `r` occupies `r/c` of a worker,
so the pool of `w` workers can sustain

    max_hot = w * c / r

With `w=5`, `c=2 s`: **100 ms → 100 panels, 300 ms → 33, 1000 ms → 10**. Measured with
`scripts/benchmark_hot_capacity.py` (virtual time, real policy decisions, 5 workers,
cadence 2 s, 3-minute horizon; the dashboard's watch marks are renewed every 30 s as a
real tab does):

| HOT panels | read | p50 | p95 | queue delay p95 | missed deadlines | saturation ticks |
|---|---|---|---|---|---|---|
| 1 | 300 ms | 2000 ms | 2000 ms | 0 ms | 0 % | 0 |
| 10 | 300 ms | 2000 ms | 2000 ms | 50 ms | 0 % | 5 |
| 20 | 300 ms | 2000 ms | 2000 ms | 100 ms | 0 % | 16 |
| 30 | 300 ms | 2100 ms | 2100 ms | **1750 ms** | 0 % | 918 |
| 40 | 300 ms | **2800 ms** | 2800 ms | **2450 ms** | 0 % | 552 |
| 5 | 1000 ms | 2000 ms | 2000 ms | 0 ms | 0 % | 6 |
| 10 | 1000 ms | 2100 ms | 2100 ms | **1050 ms** | 0 % | 176 |
| 15 | 1000 ms | **3150 ms** | 3150 ms | **2100 ms** | **58 %** | 176 |

So the measured comfort zone is ~20 HOT panels at 300 ms (queue delay ≤ 100 ms) and ~5 at
1 s, with the theoretical ceiling (33 / 10) reached as a *hard* edge where queue delay
explodes. This replaces the earlier "≈10 at 300 ms / ≈4 at 1 s" claim in this document,
which understated the pool by roughly 3x — the formula above is the one to size with, and
the measured rows are what it looks like when the pool is actually exhausted.

## Results: idle load and the WARM band

The idle feed rate is the model, not an accident. For `n` idle panels on band `i` with
jitter span `j`, the rate is approximately `n * 60 / (i + j/2)`:

| panels | band | measured | model |
|---|---|---|---|
| 100 (99 idle + 1 HOT) | 45 s | **126.6 /min** | ~119 /min |
| 100, compressed to 5 s (the wall-clock benchmark's `--idle-seconds 5`) | 5 s | ~400–500 /min | ~800–1200 /min |

The earlier report's "~400–500 fetch/min" came from that **compressed 5 s band**, not from
production's 45 s; at the production band the same install is ~127 feeds/min. (The
wall-clock figure also sits below its own model because that run had no Redis and pays a
connection-retry stall periodically, and because a 12 s window is short; the virtual-time
number is the one to quote.)

Per-mode accounting for the production band (virtual harness, 100 panels, 1 HOT):

| install | feeds/min by mode | final modes |
|---|---|---|
| calm (idle polls report no change) | hot 30.2, warm 0, idle 126.6 | hot 1, warm 0, idle 99 |
| **busy** (every idle poll reports new traffic) | hot 30.2, warm 0, idle 126.6 | hot 1, warm 0, idle 99 |

The busy row is the important one: it is identical to the calm row, i.e. a busy install does
**not** promote itself into the WARM band. Before this was fixed, "the poll returned new
data" extended WARM on every panel, so a busy install settled onto the 10 s band for the
WARM TTL and paid several times the panel load with nothing on screen to justify it. WARM is
now a hand-off from attention: only a panel that was HOT (or still settling from one) can
extend it. `tests/test_server_polling.py::test_a_busy_idle_panel_is_not_promoted_into_the_warm_band`
guards it.

## Reproducing everything here

```powershell
# A) install size scaling + a wall-clock cross-check of B (Tier 3)
.\.venv\Scripts\python.exe scripts\benchmark_per_server_scheduling.py `
    --modes legacy-cycle,per-server --servers 1,10,50,100 `
    --latency-ms 100,300,1000 --seconds 12 --idle-seconds 5 --json bench-scheduling.json

# A + B + idle/WARM in virtual time, ~10 s of wall clock, no sleeps (Tier 3)
.\.venv\Scripts\python.exe scripts\benchmark_hot_capacity.py --json hot-capacity.json
```

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

With `EVE_REFRESH_WORKERS=5` and `EVE_SERVER_POLL_ACTIVE_SECONDS=2`, the pool sustains
`workers * cadence / read` HOT panels: 100 at 100 ms, ~33 at 300 ms, ~10 at 1 s. Measured,
the comfortable zone is about two thirds of that (queue delay stays ≤ 100 ms), and the
theoretical number is a hard edge rather than a target:

* ~20 HOT panels at 300 ms reads, ~5 at 1 s reads: full cadence, no queue delay.
* 30 HOT at 300 ms, 10 at 1 s: still ~2.1 s p50, but `queue_delay_ms` climbs to ~1-1.8 s.
* 40 HOT at 300 ms, 15 at 1 s: the cadence itself degrades (2.8-3.2 s p50) and deadlines
  are missed. This is capacity exhaustion, not a scheduling bug.

Beyond the pool, the cadence degrades **visibly**: `scheduler_queue_delay_ms` grows per
panel, `saturation_events` and `capacity_rejections` grow in `/api/doctor`, and the
freshness line on the dashboard shows the age. Raise `EVE_REFRESH_WORKERS` (and
`EVE_PANEL_CONCURRENCY` with it), or lower `EVE_SERVER_POLL_WATCH_LIMIT` so fewer panels
are HOT. Do not raise the worker count past the panel semaphore: a worker that cannot get
a slot spends its time waiting for one.

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
