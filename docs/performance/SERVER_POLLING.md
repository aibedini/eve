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

## Change

`panel/core/refresh_policy.py` gains a per-server schedule, and the periodic cycle
honours it.

* Each panel has a state: `next_due`, `failures`, `active_until`.
* **Active** panels - the ones on screen (`?servers=`/`?server_id=` on `/api/refresh`, the
  same declaration on the SSE stream) or touched by an Eve mutation - are polled every
  `EVE_SERVER_POLL_ACTIVE_SECONDS` (2 s). The mark is a TTL: nothing renews it once the
  tab stops polling, so the fast cadence dies on its own.
* **Idle** panels are polled every `EVE_SERVER_POLL_IDLE_SECONDS` (45 s).
* A failing panel backs off exponentially (5 s, 10 s, 20 s ... capped at 300 s) and the
  window is mirrored into the schedule, so the loop never wakes for a cycle that would
  only skip that panel again.
* Becoming active **pulls the next poll in** (a panel appearing on screen must not sit out
  a remaining idle window), except while it is backing off.
* `retain_servers()` forgets panels that left the enabled set, so a deleted panel cannot
  keep the schedule permanently "due".

* The watch marks are **shared, not process-local**: the browser's declaration
  arrives in a web process while the loop that must speed up runs in the background
  process, so each mark is written to Redis (`eve:refresh:watch:<server_id>`, TTL =
  the active window, value = the reason) and the schedule consults the shared set as
  well as its own. Without Redis the mark is process-local, which is correct for a
  single-process install and is exactly the old behaviour. See
  `docs/TELEMETRY_STATE_TRANSITIONS.md` and
  `tests/test_watch_propagation_crossprocess.py`.

Wiring:

* `fetch_and_update_global_data(..., periodic=True)` is the automatic loop's cycle and the
  only path that honours the schedule; a manual refresh, a targeted repair, and the
  usage-rollup warm-up always fetch what they ask for (operator intent outranks the poll
  schedule).
* A cycle in which every enabled panel is deferred returns without publishing: bumping
  `last_update` there would fake a fresh snapshot and hide the panel whose turn it is.
* A deferred panel is **not** a skipped panel: it keeps its cached block and its
  reachability instead of being reported as unreachable/"Backoff".
* `background_data_fetcher` wakes for `min(cycle interval, activity slice, next due
  panel)`, and treats the per-server schedule as the authority once panels are tracked
  (the cycle-level staleness is then only the bootstrap path).
* The dashboard sends the panels it renders with every cache poll (`?servers=1,2,3`) and
  on the live-update stream, so the fetcher follows the operator's screen.
* `GET /api/doctor` → `checks.refresh_policy` exposes `servers`, `servers_tracked`,
  `servers_due`, and `server_intervals`.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_SERVER_POLL_ACTIVE_SECONDS | 2 | poll interval for a watched/mutated panel |
| EVE_SERVER_POLL_IDLE_SECONDS | 45 | poll interval for an unwatched panel |
| EVE_SERVER_POLL_ACTIVE_TTL_SECONDS | 120 | how long a watch mark lasts without renewal |
| EVE_SERVER_POLL_BACKOFF_BASE_SECONDS | 5 | first backoff step after a failure |
| EVE_SERVER_POLL_BACKOFF_MAX_SECONDS | 300 | backoff ceiling |
| EVE_SERVER_POLL_WATCH_LIMIT | 20 | panels one dashboard may keep on the fast cadence |

## Cost

The fast cadence is bounded on purpose: one open tab can hold at most
`EVE_SERVER_POLL_WATCH_LIMIT` panels at 2 s (20 panels ≈ 600 panel requests/minute while
the tab is actively polling), everything else stays on the idle cadence. An install with
more panels than the limit is still bounded, and an operator who wants the whole install
live can raise the limit knowingly or click Refresh, which fetches everything.

## Limits

* The schedule is per process: only the fetcher role owns the loop, so this state is not
  shared through Redis (unlike the activity timestamp).
* A panel is "watched" because the browser says so; a bot or API-only client that never
  calls `/api/refresh` does not create watch marks.
* With `EVE_SSE_ENABLED=1` a stream-driven tab renews its marks from the stream itself,
  which is what keeps the panels on screen fresh without a poll loop.

## Tests

`tests/test_server_polling.py`: cadence and TTL, the active pull-in, backoff and its
interaction with a watch mark, defer never pulling a poll earlier, watch capping and
deduplication, `retain_servers`, the periodic cycle (second cycle defers and does not
publish, manual cycle ignores the schedule, only the elapsed panel is fetched, a deferred
panel keeps its reachability, a backoff skip reschedules, a disabled panel is forgotten),
the loop's wake bound and the periodic-path flag, and the route-level declaration
(`?servers=`, `?server_id=`, the cap, and the SSE stream renewing its marks).
