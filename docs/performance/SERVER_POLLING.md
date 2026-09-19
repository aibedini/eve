# Per-server adaptive polling

## Problem

The adaptive cadence in `docs/performance/ADAPTIVE_REFRESH.md` decides **how often the
fetcher wakes**. Inside a cycle, every enabled panel was still fetched, so freshness was
all-or-nothing: the whole install was polled on the cycle cadence. Two consequences:

* an external change made directly in X-UI (a new client, a usage jump, a disabled
  account) reached the UI only on the next full cycle - up to 30 s while the operator is
  watching, up to 300 s when idle;
* the operator's attention could not buy freshness for one panel without paying the
  fan-out cost for all of them, which is what makes "poll the panel I am looking at every
  two seconds" impractical on a large install.

A per-server cadence alone did not fix the second half. Three things kept the two-second
target unreachable, and each of them is addressed below:

1. the fan-out read **every** due panel in one sweep, so the cycle length (and therefore
   the effective interval of the panel the operator was watching) was set by the idle
   majority;
2. every idle panel was scheduled `idle_seconds` after the same cycle, so the whole
   install came due in the same second and re-created that sweep forever;
3. watch state and mutation state **did not cross the process boundary** in time: the
   browser talks to a web process, the polling loop runs in the background process, the
   mark was read through a five-second cache, and an EVE mutation was not published at
   all - so a panel was made hot in a process that does not poll.

## Change

The automatic fetch path is a **per-server scheduler**, not a cycle:
`schedulers.run_per_server_scheduler()` keeps a bounded worker pool and, on every tick,
dispatches whatever is due to a free worker -- one read per panel, rescheduled from that
panel's own completion. There is no batch and no barrier, so the number of other panels
changes how long a panel waits for a FREE worker (measured, see below), never when it is
scheduled. `panel/core/refresh_policy.py` owns the schedule the scheduler dispatches from.

The global sweep survives for what it is actually good at: the first fill after a restart
(discovery + one coherent snapshot), an operator's refresh, recovery, and the staleness
ceiling that `server_due()` enforces on its own.

* Each panel has a runtime state: `next_due`, `failures`, `active_until`, `warm_until`,
  `inflight`, `wake_pending`, the last fetch/success/error/publish timestamps, the
  dispatch/queue delay, the outcomes, the two revisions and the mode.
  `server_sync_state()` and `sync_summary()` expose it (ids, modes, ages and counters
  only - never a credential, address or email).
* **One read in flight per server**, enforced twice: the scheduler skips a panel whose
  `inflight` flag is set, and `panel_limits.coalesce` makes any other caller of the same
  panel wait for that read instead of starting a second one.
* A nudge that arrives while a panel is being read is **held** (`wake_pending`) and the
  panel is rescheduled for immediately when the read returns, so a mutation landing
  mid-poll is not invisible for a whole cadence.
* The next poll is **period-preserving**: it is measured from the schedule the completed
  read was serving, so a HOT panel whose read takes 300 ms is polled every 2 s
  start-to-start rather than every 2.3 s. A read that overran its interval leaves the
  schedule in the past, which means "due now" - the panel catches up instead of silently
  skipping a slot.
* **HOT** panels - the ones on screen (`?servers=`/`?server_id=` on `/api/refresh`, the
  same declaration on the SSE stream) or touched by an Eve mutation - are polled every
  `EVE_SERVER_POLL_ACTIVE_SECONDS` (2 s). The mark is a TTL: nothing renews it once the
  tab stops polling, so the fast cadence dies on its own.
* **WARM** panels - watched or mutated recently, nobody looking right now, or a panel
  that actually changed on its last poll - are polled every
  `EVE_SERVER_POLL_WARM_SECONDS` (10 s) until `EVE_SERVER_WARM_TTL_SECONDS` (600 s)
  after the HOT window ends. WARM exists so the hand-off from 2 s to 45 s is a band
  rather than a cliff, which is what makes a renewal or a just-closed tab settle.
* **IDLE** panels are polled every `EVE_SERVER_POLL_IDLE_SECONDS` (45 s) plus a stable
  per-server offset inside `EVE_SERVER_POLL_IDLE_JITTER_SECONDS` (10 s), so the install
  does not come due in one second. The offset is a checksum of the server id, identical
  in every process and after a restart (`hash()` is salted per interpreter and would
  re-align the very panels the jitter separates).
* A failing panel backs off exponentially (5 s, 10 s, 20 s ... capped at 300 s) and the
  window is mirrored into the schedule, so the loop never wakes for a panel it would only
  skip again. A backoff keeps its exact interval - jitter is a property of the idle band,
  not of a retry ladder.
* Becoming active **pulls the next poll in** (a panel appearing on screen must not sit out
  a remaining idle window), except while it is backing off.
* `retain_servers()` forgets panels that left the enabled set, so a deleted panel cannot
  keep the schedule permanently "due".

### Saturation is measured, not hidden

`scheduler_metrics()` (also in `/api/doctor` under `scheduler`) reports `dispatched`,
`completed`, `errors`, `queue_delay_ms_avg/max` (how late a due panel actually started
against its own schedule), `saturation_events` (ticks where more panels were due than
there were free workers), `max_due_i_wait`, `workers`, `wake_consumed` and the panel
concurrency counters. `server_sync_state()` carries the same evidence per panel
(`scheduler_queue_delay_ms`, `last_start_gap_ms`, `inflight`, `wake_pending`).

This exists because capacity is finite: the cadence is only promised up to the worker
pool. When the pool is saturated the queue delay rises visibly instead of the system
quietly calling a 9-second poll "2-second polling".

### Batch ordering is still used by the sweep

`prioritize_fetch_batch()` orders the *sweep's* due panels by band (HOT, WARM, backoff,
idle) and then by how long each has been due, and bounds it to
`EVE_REFRESH_BATCH_SERVERS` (default: twice the refresh worker limit). A panel that just
became HOT is therefore read in the sweep's first worker slot instead of behind
everything else that happens to be due; the panels left out stay due and are picked up by
the next sweep. This is the recovery/bootstrap path - the per-server scheduler does not
need it.

Only the sweep is bounded. A manual refresh, a targeted repair and the
messaging warm-up name a set and get all of it: the bound is a scheduling device, not a
budget, and a silently partial "Refresh" would be worse than a slow one.

### Watch state crosses the process boundary

* The mark itself is one Redis key per server (`eve:refresh:watch:<server_id>`, TTL = the
  active window, value = the reason) **plus a sorted-set index** (`eve:refresh:hot_servers`,
  scored by expiry, with the reason in `eve:refresh:watch_reason`). The index is what the
  fetcher reads: `ZREMRANGEBYSCORE` + `ZRANGEBYSCORE` + `HMGET` is O(log n + live marks),
  where the previous `SCAN eve:refresh:watch:*` grew with the size of the keyspace.
  Watch keys written by a build that predates the index are folded into it at most once
  per `LEGACY_SCAN_SECONDS` (60), so a rolling upgrade costs one bounded scan rather than
  one scan per read.
* `publish_wake()` carries a nudge on `eve:refresh:wake` (Redis Pub/Sub) so the fetch
  loop breaks its sleep instead of waiting out its slice; `start_wake_listener()` runs one
  subscriber thread in the process that owns the loop. The channel is **not** a source of
  truth: a dropped message costs one sleep slice, because the durable key and index are
  what the policy reads. With no Redis the nudge degrades to the in-process wake event.
* `note_watch()` publishes on a new mark and throttles renewals
  (`WATCH_WAKE_THROTTLE_SECONDS`); `note_server_activity(..., share=True)` - the mutation
  path - publishes the mark **and** a nudge, which is how an Eve write made in a web
  process now makes the panel hot in the fetcher process.

### A verified mutation is fenced against a slower read

The panel's client-level endpoint reflects an EVE write immediately; its aggregate
inbound list can lag by a poll. Without a guard the next background poll read the
aggregate, saw the pre-mutation counters and wrote them over the verified ones - the
renewal appearing to undo itself a minute later.

* After a verified mutation, `patch_cached_client()` records the verified client state
  (`record_client_fence()`, TTL `EVE_CLIENT_FENCE_SECONDS`, 30 s) in Redis **and** in
  process, and the write-through now prefers the verified telemetry over the cached row
  (previously the row won, so the mutation response reported pre-mutation counters).
* `_apply_client_fences()` in `panel/jobs/schedulers.py` holds the verified counters
  against a lagging aggregate read, and clears the fence as soon as a read comes back at
  or above them - it can never pin a value the panel genuinely changed.
* A renewal's **baseline** (the pre-mutation traffic state its new cap and its "previous"
  ledger figures are derived from) may only come from the cache while that row's traffic
  view is younger than `EVE_RENEW_BASELINE_MAX_AGE_SECONDS` (30 s). The row's own
  per-layer stamp is the precise answer, and the snapshot's `last_update` is the bound
  when the row predates those stamps. A row that says it is too old is read from the
  panel first, because an old row is a wrong baseline and not merely a slow one; a row
  with no usable stamp is accepted, since the cache is then the only state there is. The
  decision travels in the renew response's `timing` (`baseline_source`,
  `cache_baseline_age_seconds`, `cache_baseline_rejected`).

Wiring:

* `background_data_fetcher()` runs ONE bootstrap sweep (discovery and a coherent first
  snapshot), then `run_per_server_scheduler()` for the rest of the process's life. The
  sweep is the recovery/bootstrap path; it is not the timing authority for HOT polling.
* A sweep in which every enabled panel is deferred returns without publishing: bumping
  `last_update` there would fake a fresh snapshot and hide the panel whose turn it is.
* A deferred panel is **not** a skipped panel: it keeps its cached block and its
  reachability instead of being reported as unreachable/"Backoff".
* The scheduler waits on its worker futures and the wake event in `SCHEDULER_TICK_SECONDS`
  (0.05 s) steps, so a cross-process nudge is acted on within one tick and a freed worker
  is reused immediately - it never sleeps "for the cycle".
* `server_due()` enforces `EVE_REFRESH_MAX_STALENESS_SECONDS` itself, so the safety net
  that a periodic sweep used to provide survives without one.
* Every completed read mirrors a compact scheduling row to Redis
  (`eve:refresh:server_sync`, TTL 15 min, one HSET per read). A web process reads it to
  answer "is what I am rendering live?" without pretending it knows the fetcher's memory;
  a missing row means unknown. This is a report, never an input to a scheduling decision.
* The dashboard sends the panels it renders with every cache poll (`?servers=1,2,3`) and
  on the live-update stream, and caps that list at the server's own limit (rendered into
  the page from `EVE_SERVER_POLL_WATCH_LIMIT`) so the two cannot disagree about which
  panels are being watched.
* `/api/refresh` returns `servers_sync`: per server, `mode`, `watched`, `watch_reason`,
  `inflight`, `wake_pending`, `sync_health`, the ages and the queue delay. It is a side map
  rather than a field on each inbound block, because those blocks are the shared snapshot
  and a per-request age written into them would change the delta fingerprint on every poll.
* `GET /api/doctor` -> `checks.refresh_policy` exposes `servers`, `servers_tracked`,
  `servers_due`, the aggregate `sync` summary, `scheduler` (throughput, queue delay,
  saturation), the full per-server state under `servers_sync` (including
  `config_age_seconds`/`telemetry_age_seconds` from the snapshot's own stamps), the
  non-live subset under `servers_attention`, and `server_intervals`.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_SERVER_POLL_ACTIVE_SECONDS | 2 | poll interval for a watched/mutated panel |
| EVE_SERVER_POLL_WARM_SECONDS | 10 | poll interval for a recently watched/mutated panel |
| EVE_SERVER_POLL_IDLE_SECONDS | 45 | poll interval for an unwatched panel |
| EVE_SERVER_POLL_ACTIVE_TTL_SECONDS | 120 | how long a watch mark lasts without renewal |
| EVE_SERVER_WARM_TTL_SECONDS | 600 | how long the warm band lasts after the hot window |
| EVE_SERVER_POLL_IDLE_JITTER_SECONDS | 10 | spread over which idle panels come due |
| EVE_SERVER_POLL_BACKOFF_BASE_SECONDS | 5 | first backoff step after a failure |
| EVE_SERVER_POLL_BACKOFF_MAX_SECONDS | 300 | backoff ceiling |
| EVE_SERVER_POLL_WATCH_LIMIT | 20 | panels one dashboard may keep on the fast cadence |
| EVE_REFRESH_WORKERS | 5 | worker threads the scheduler may use at once |
| EVE_PANEL_CONCURRENCY | 12 | process-wide simultaneous panel fetches |
| EVE_REFRESH_BATCH_SERVERS | 0 (auto) | panels one SWEEP may read; 0 = every due panel |
| EVE_CLIENT_FENCE_SECONDS | 30 | read-your-writes fence lifetime |
| EVE_RENEW_BASELINE_MAX_AGE_SECONDS | 30 | oldest cached traffic view a renewal may use |

`EVE_REFRESH_BATCH_SERVERS=0` makes the recovery sweep whole-install, and
`EVE_SERVER_POLL_IDLE_JITTER_SECONDS=0` restores the synchronized idle schedule; both are
diagnostics, not settings an install should keep.

### Production sizing (measured)

`scripts/benchmark_per_server_scheduling.py` measures the same policy under the previous
dispatch shape and the per-server one; `docs/performance/SCHEDULING_BENCHMARK.md` carries
the numbers and the sizing recommendation. The short version: with the default
`EVE_REFRESH_WORKERS=5` and `EVE_SERVER_POLL_ACTIVE_SECONDS=2`, one HOT panel keeps its
2 s cadence up to a 100-panel install as long as the panel read is comfortably under
`workers x cadence / hot_panels`. Panels that are HOT beyond the pool's capacity are
served with a visible `scheduler_queue_delay_ms` (and `saturation_events` in the doctor
payload) rather than a silent promise of 2 s.

**Degradation policy when more panels are HOT than the pool can serve:** the scheduler
keeps strict fairness (band, then how long each panel has been waiting), so no panel
starves and every HOT panel is served in due order; what grows is queue delay, which is
reported per panel and in aggregate. A `saturation_events` counter that keeps rising with
`queue_delay_ms_max` well above the cadence is the signal to raise `EVE_REFRESH_WORKERS`
(and, if the panel hosts can take it, `EVE_PANEL_CONCURRENCY`), or to lower the watch
limit so fewer panels are HOT.

## Cost

The fast cadence is bounded on purpose: one open tab can hold at most
`EVE_SERVER_POLL_WATCH_LIMIT` panels at 2 s (20 panels ≈ 600 panel requests/minute while
the tab is actively polling), everything else stays on the idle cadence. An install with
more panels than the limit is still bounded, and an operator who wants the whole install
live can raise the limit knowingly or click Refresh, which fetches everything.

Bounding a sweep's batch trades one long cycle for several short ones: the same panel
reads per minute, spread so a HOT panel is read at the head of every sweep instead of once
per whole install. The jitter does not add reads; it moves idle panels off each other's
due time. In the steady state the scheduler does not need either: each panel is read on
its own clock.

## Limits

* The schedule itself is per process: only the fetcher role owns the loop, so the timings
  are not shared through Redis (unlike the watch marks, the fences and the activity
  timestamp) - the published `eve:refresh:server_sync` rows are a read-only report. A
  second fetcher would keep its own `next_due` values.
* The wake channel is best effort. A missed nudge costs one scheduler tick (0.05 s) plus
  the pub/sub round trip, and a read already running is not interrupted - it is remembered
  as `wake_pending` and the panel is rescheduled the moment that read returns.
* The cadence is a promise UP TO the worker pool. Beyond it the delay is real, reported
  (`scheduler_queue_delay_ms`, `saturation_events`) and fair, but it is a delay: an install
  that wants N panels HOT at once needs the workers to serve them.
* The fence is read-your-writes, not a lock: it holds the verified state (counters, cap,
  expiry) for its lifetime and is released the moment the panel's own read agrees. A value
  the panel genuinely changed is never pinned beyond the fence's TTL.
* A panel is "watched" because the browser says so; a bot or API-only client that never
  calls `/api/refresh` does not create watch marks.
* With `EVE_SSE_ENABLED=1` a stream-driven tab renews its marks from the stream itself,
  which is what keeps the panels on screen fresh without a poll loop.
* `sync_health()` answers freshness, not reachability: a panel can be online and still
  `stale`. "live" is reserved for a successful read inside the HOT cadence, so the badge
  never claims real time for data the loop has not refreshed.

## Tests

`tests/test_server_polling.py` (67 tests): cadence and TTL, the WARM band and its
hand-off, idle jitter, the active pull-in, backoff and its interaction with a watch mark,
defer never pulling a poll earlier, watch capping and deduplication, `retain_servers`, the
recovery sweep (a second sweep defers and does not publish, a manual sweep ignores the
schedule, only the elapsed panel is fetched, a deferred panel keeps its reachability, a
backoff skip reschedules, a disabled panel is forgotten), batch ordering and its limit,
the **per-server scheduler** (a HOT panel is not paced by the rest of the install, the
cadence does not grow with the server count, one read per panel, bounded concurrency,
saturation is measured, a nudge during an in-flight read reschedules the panel, the
bootstrap sweep is not the timing authority), the cross-process wake payload and listener,
the client fence lifecycle including the cap/expiry hold, the sync state/summary/health
vocabulary, `sync_event` rendering, and the route-level declaration (`?servers=`,
`?server_id=`, the cap, and the SSE stream renewing its marks).

`tests/test_renew_consistency.py` drives the real renew route and the real single-server
read path for the five renewal scenarios (stale cache baseline, fresh cache baseline,
verified counters winning, a pre-mutation background read, a lagging aggregate endpoint).

`tests/test_watch_propagation_crossprocess.py` proves the shared watch mark against real
child processes, `scripts/integration_redis_multiprocess.py` does the same against a real
Redis (see `docs/performance/REDIS_MULTIPROCESS.md`), and `tests/test_mutation_scale.py` /
`scripts/benchmark_mutation_scale.py` keep the mutation path bounded on a large install.
