# Background workers

## Problem

The panel runs background work in threads: the data fetcher, the snapshot reader,
the backup scheduler, the health watchdog, the usage rollup, the messaging bots,
the pulse scheduler and the BNQO link engine. With more than one gunicorn worker
each process would start its own copy, so exclusivity is claimed with a non-blocking
fcntl lock per singleton name. Three gaps remained:

1. **No visibility.** Nothing reported which process owned which singleton, or
   whether a worker had started at all. Diagnosing "backups stopped" meant reading
   logs across workers.
2. **A broken lock file silently disabled work.** The claim caught every
   `OSError` and returned False, which means "another worker owns it". If the lock
   directory was read-only or wrongly owned, *every* worker returned False and no
   process ran the scheduler, watchdog or bots — with no error anywhere.
3. **Fifteen near-identical start blocks** each repeated the claim, the thread
   creation, the try/except and the skip log, so a change to the pattern had to be
   made fifteen times.

## Change

`panel/jobs/schedulers.py`:

* `_start_worker(name, target, singleton=False)` is the one way a background
  thread is started. It claims the singleton (when asked), starts a daemon thread
  named `eve-<name>`, and records the outcome.
* `_WORKER_REGISTRY` records per worker: `state` (`started`/`skipped`/`failed`),
  `singleton`, the thread name and the ISO start time (or the error).
* `worker_inventory()` returns `pid`, `process_role`, `threads_started`,
  `singletons_owned`, `singleton_errors` and the per-worker records with a live
  `alive` flag computed from `threading.enumerate()`.
* `_claim_singleton` now distinguishes contention from a lock file it could not
  create: `EAGAIN`/`EACCES` means another worker owns it (False); any other
  `OSError` is a deployment fault, so the worker **fails open** (runs anyway) and
  the reason is recorded in `_SINGLETON_ERRORS` and logged as a warning.
* `ensure_background_threads_started` is now a list of `_start_worker` calls with
  the role and the singleton flag, which is what the tests assert against.

`panel/routes/doctor.py` exposes the inventory as `checks.workers`, and marks it
`warning` when a worker failed to start or a singleton lock was unavailable.

## Process roles

| PROCESS_ROLE | Workers |
|--------------|---------|
| `web` | `snapshot_reader` only (reads the shared Redis snapshot; no panel fan-out) |
| `worker` | scheduler, watchdog, rollups, bots, pulse and BNQO schedulers (each singleton) |
| `combined` | everything a worker runs, plus the data fetcher; the non-owner also reads the snapshot |

With Redis enabled the data fetcher is a singleton (`data_fetcher`) and only the
winner also runs `refresh_queue_worker`; the losers read the shared snapshot. With
Redis disabled every worker fetches into its own memory cache.

## Verification

`tests/test_worker_inventory.py` (8 tests, 1 POSIX-only): a started worker is
recorded and reported alive; a thread that cannot start is recorded as `failed`
with its error; the inventory shape and empty error map; a second claim of the same
singleton fails (POSIX); a lock-file failure fails open and is recorded; the `web`
role starts exactly the snapshot reader; the background role starts the singleton
set; and the bootstrap runs once per process. `tests/test_pg_hardening.py`-style
doctor coverage: `/api/doctor` reports `checks.workers` with the current pid.

## Residual risk

* **Fail-open can duplicate work.** When the lock directory is unusable every
  worker runs the scheduler, so an auto-backup could run more than once. That is
  preferred over silently never running it, and the reason is visible in the
  doctor payload. Fix the reported path to restore exclusivity.
* **Windows development** has no fcntl, so every process fails open by design; that
  is the same behaviour as before.
* A singleton owner that dies releases the lock, so the next process to call
  `ensure_background_threads_started` claims it. There is no rebalancing while the
  owner is alive, which matches the previous design.
