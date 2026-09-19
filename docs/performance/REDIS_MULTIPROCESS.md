# Real-Redis multi-process proof for the refresh/watch pipeline

`scripts/integration_redis_multiprocess.py` starts **real operating-system child
processes**, points them at a **real Redis** through the application's own
`panel.core.redis_client.get_redis()`, and measures three facts that no single-process
test can observe.

This document tells an operator how to run it and what each PASS line means.

---

## 1. Why this exists next to the fake-Redis test

| | `tests/test_watch_propagation_crossprocess.py` | this harness |
|---|---|---|
| Backend | a small file-backed FAKE Redis written for the test | a real Redis server |
| Runs in CI | always, no service needed | only when a Redis is reachable |
| Proves | the watch mark and fetch ticket cross a process boundary, and that the same code WITHOUT the shared backend does not | the app speaks Redis correctly end to end: real `SET`/`GET`, real TTLs, real pub/sub, real compressed snapshot round-trip |
| Fails when | the cross-process mechanism regresses | the backend contract regresses (wrong key, wrong type, dropped TTL, wrong channel, undecodable payload) |

The fake is the **always-on guard**. It stays the thing that fails in CI. This harness
is the **real-backend proof**: it catches the class of bug a hand-written stub cannot,
because the stub is only as good as the assumptions of whoever wrote it. A dropped
`ex=` argument, a value stored as a type the reader cannot decode, or a channel name
no listener subscribes to would all pass against a fake and fail here.

Neither replaces the other. Run the fake test on every change; run this harness when
you touch `panel/core/redis_client.py`, `panel/core/refresh_policy.py`,
`panel/jobs/schedulers.py` (snapshot reader / fetcher publish paths), when you change a
Redis key name or a TTL, or before a release that ships those files.

---

## 2. What you need

* A reachable Redis 6+ server. The workspace default database is fine.
* `redis` (redis-py) installed in the same interpreter you run the app with
  (`requirements.txt` pins `redis>=5.0.0`). If it is missing, the app reports "no
  Redis" and the harness skips - that is a skip, not a failure.
* The panel's own configuration, or one environment variable:

| Variable | Meaning |
|---|---|
| `REDIS_URL` | the app's own variable; `get_redis()` uses it first |
| `EVE_INTEGRATION_REDIS_URL` | tried only when the app's configuration does not reach a server, so a CI job can point this run at a service without editing app config |
| `EVE_SERVER_POLL_ACTIVE_SECONDS` | optional; if you override it in the panel, pass the same value to the harness with `--active-interval` |
| `EVE_SERVER_POLL_ACTIVE_TTL_SECONDS` | optional; if you raise it above 120, raise `--mark-ttl-max` to match |

**Throwaway Redis (recommended).** This starts a disposable server with no
persistence and removes it on exit:

```
docker run --rm -p 6379:6379 redis:7-alpine
```

If port 6379 is already taken by something else, publish another port and point the
harness at it (`-p 6380:6379` plus `REDIS_URL=redis://127.0.0.1:6380/0`).

**Do not point this at a shared production Redis.** The refresh-policy keys (watch
marks, the wake channel, client fences) belong to the product and cannot be renamed,
so the run removes the keys it wrote for its own synthetic server ids. It never
touches production-looking snapshot keys, because every snapshot key it writes carries
a per-run `eve:it:<random>:` prefix. See "Isolation" below.

---

## 3. Run it

PowerShell (Windows):

```powershell
docker run --rm -p 6379:6379 redis:7-alpine     # in another terminal, or -d

$env:REDIS_URL = 'redis://127.0.0.1:6379/0'
$env:EVE_SKIP_IMPORT_MIGRATIONS = '1'
$env:DISABLE_BACKGROUND_THREADS = '1'
.\.venv\Scripts\python.exe scripts\integration_redis_multiprocess.py --json .\_redis_it.json
```

Plain shell (Linux/macOS, or Git Bash):

```sh
docker run --rm -p 6379:6379 redis:7-alpine &   # or -d

export REDIS_URL='redis://127.0.0.1:6379/0'
export EVE_SKIP_IMPORT_MIGRATIONS=1
export DISABLE_BACKGROUND_THREADS=1
.venv/bin/python scripts/integration_redis_multiprocess.py --json ./_redis_it.json
```

Point it at a service explicitly instead of the app config:

```powershell
$env:EVE_INTEGRATION_REDIS_URL = 'redis://127.0.0.1:6380/0'
```

Useful options:

| Option | Default | Meaning |
|---|---|---|
| `--json PATH` | none | where to write the machine-readable summary (written even on skip/failure) |
| `--rounds N` | `5` | watch rounds behind the latency sample |
| `--fetch-latency-ms MS` | `250` | simulated panel read latency in step 2 |
| `--active-interval S` | `2.0` | expected HOT interval |
| `--mark-ttl-max S` | `125.0` | longest acceptable watch-mark TTL |
| `--timeout S` | `300` | per-child timeout |

The script exits `0` when all three steps pass **and** when it skips for lack of
Redis; it exits `1` when a step fails.

---

## 4. Expected output

Healthy run:

```
Redis: redis://127.0.0.1:6379/0 (discovered via get_redis())
Synthetic server ids: 9000001..9000501, namespace=mp-1a2b3c4d5e6f
[PASS] watch         publish_to_wake_ms_p95=4.812ms
       samples=[1.905, 4.812, 2.377, 3.04, 2.115] p50=2.377 max=4.812
[PASS] fetch_publish fetch_to_revision_visible_ms=412.77ms
[PASS] mutation      fence_roundtrip_ms=903.114ms
cleanup: removed 15 key(s)
OVERALL: PASSED
JSON: C:\...\_redis_it.json
```

No Redis reachable (the normal case on a developer box):

```
SKIPPED: no Redis available (get_redis() returned None (no REDIS_URL configured, or redis-py not installed, or the server is unreachable))
  This harness needs a REAL Redis; it is not a substitute for the always-on fake-Redis guard in tests/test_watch_propagation_crossprocess.py.
  Start one with: docker run --rm -p 6379:6379 redis:7-alpine
  Then set REDIS_URL (or EVE_INTEGRATION_REDIS_URL) and re-run.
```

Exit code `0`, `status: "skipped"` and a `reason` in the JSON.

---

## 5. How to read the three PASS lines

Every step asserts its facts internally; the printed line is the metric an operator
watches, and any failed assertion is listed underneath as `- <reason>`.

### `[PASS] watch` - `publish_to_wake_ms`

What runs: one child process ("web") calls
`refresh_policy.note_watch(<server_id>)`; a second, long-lived child
("fetcher") has `start_wake_listener()` running and reports what IT sees.

What it would catch:

* the wake published on a channel no listener subscribes to (or under a renamed
  channel) - `wake payload did not name server N`;
* a watch mark written under a key the reader never reads, or with a value it cannot
  decode - `server N is not watched in the fetcher process`;
* a mark that is visible but does not shorten the cadence (the original bug: the panel
  on screen in one process, polled on the idle interval in another) -
  `server N interval=45.0, expected HOT 2.0`;
* a mark written without an expiry, which would pin a closed tab's panel hot forever -
  the reader checks the TTL the server reports;
* a wake that only "arrives" because the harness slept long enough - the handshake
  reports `listening` before the writer runs, and a sample whose wake arrives before
  the publish on the shared clock is a failure.

`publish_to_wake_ms` is measured between the writer's own timestamp taken **after**
`publish()` returned and the moment the listener thread received the message. It is
therefore an **upper bound** on delivery latency, never an optimistically small
number. `publish_to_wake_ms_p95` is the nearest-rank 95th percentile of the samples;
the full sample list and the p50 are printed next to it, so with `--rounds 3` the p95
is simply the largest of the three samples.

The step also runs a **control**: the same child code started with no Redis configured
(`--no-redis`, which takes the product's own single-process fallback). It must report
`backend=process`, `watched=false` and the idle interval. If the control saw the mark,
the positive result would be measuring the harness rather than the pipeline.

### `[PASS] fetch_publish` - `fetch_to_revision_visible_ms`

What runs: a child ("fetcher") reads a synthetic panel with a configurable latency
(`--fetch-latency-ms`), re-checks the shared server revision, commits the block into
`GLOBAL_SERVER_DATA` and calls the real `publish_snapshot_to_redis()`. A separate
child ("dashboard") primes its cache, then polls `load_snapshot_from_redis()` on the
same cadence `snapshot_reader_worker` uses and reports when the revision it was told
about is what it holds.

What it would catch:

* a publish that returns `True` while writing nothing a reader can find;
* a manifest the loader cannot decode (the failure mode of the rejected pickle
  format) - the dashboard would never see the revision;
* a version key that never changes, or the loader's unchanged-version short-circuit
  firing when it should not;
* a wrong key name or a missing/renamed server block - the dashboard would see no
  inbounds;
* a `last_update` that does not round-trip;
* a published key with no TTL (the run reads the TTLs back from the server), which
  would leak keyspace and keep serving stale data after the fetcher dies.

`fetch_to_revision_visible_ms` is measured from the start of the synthetic panel read
to the instant the dashboard process observed the revision, so it includes the
simulated panel latency by construction; the harness fails if the measured value is
below the configured `--fetch-latency-ms`. The JSON also records
`publish_to_revision_visible_ms` and `read_finished_to_revision_visible_ms` if you
want the pipeline-only portion.

### `[PASS] mutation` - `fence_roundtrip_ms`

What runs: child A performs a verified write-through
(`refresh_policy.record_client_fence(server_id, email, state)`); child B calls
`refresh_policy.client_fences(server_id)` and reports the fence it can see.

What it would catch:

* a fence recorded only in the writing process's dict - the exact bug the fence
  exists for, where the background poll later reverts the verified numbers;
* a hash field written with a type the reader cannot parse;
* a fence with no expiry, which would pin a value the panel genuinely changed.

The printed round trip spans both process spawns and is therefore the honest cost of a
fence crossing a real boundary; a same-process call would be microseconds and would
prove nothing. The JSON records `fence_write_ms` and `fence_read_ms` separately.

---

## 6. What the JSON summary contains

`--json <path>` always writes a summary (skip, failure and success alike):

```json
{
  "status": "passed",
  "redis_url": "redis://127.0.0.1:6379/0",
  "redis_source": "get_redis()",
  "namespace": "mp-1a2b3c4d5e6f",
  "steps": {
    "watch":         {"passed": true, "publish_to_wake_ms_p95": 4.812, "samples": []},
    "fetch_publish": {"passed": true, "fetch_to_revision_visible_ms": 412.77},
    "mutation":      {"passed": true, "fence_roundtrip_ms": 903.114}
  },
  "cleanup": {"count": 15, "removed": []},
  "children": [{"op": "watch-write", "exit_code": 0, "stderr_tail": ""}]
}
```

Each step carries `failures` (the same strings printed under the FAIL line) and every
child process invocation is listed under `children` with its exit code and the tail of
its stderr, which is where a product-side traceback appears.

---

## 7. Isolation, and what the run leaves behind

* **Snapshot keys are namespaced.** The child prefixes the real module constants
  (`REDIS_SNAPSHOT_MANIFEST_KEY`, `REDIS_SERVER_SNAPSHOT_PREFIX`,
  `REDIS_SNAPSHOT_VERSION_KEY`, `REDIS_SERVER_REVISION_PREFIX`) with
  `eve:it:<random>:`, so a key of the shape `eve:server_data_version` is never read or
  written. The harness also asserts that the publish used a namespaced key.
* **Synthetic server ids.** The run uses `--server-id-base` (default
  `9000000 + pid`) and above. Real panel rows are small integers, so the watch marks
  and fences it writes cannot collide with a live server. Override it if it ever does.
* **Cleanup always runs**, in a `finally` block, through a child process that uses the
  same key builders: it deletes the run's namespaced keys, the watch keys and fences
  for its own server ids, its entries in the shared hot-server index and watch-reason
  hash, and the shared activity key
  (`eve:refresh:last_activity`, which the policy writes on every dashboard request).
  The removed keys are listed in the JSON summary.
* **The revision keys it bumps** (`eve:server_revision:<synthetic id>`) expire with
  `REDIS_SNAPSHOT_TTL` (600 s) on their own.

If a run is killed before cleanup, the marks expire by themselves within
`EVE_SERVER_POLL_ACTIVE_TTL_SECONDS` (120 s default); a fetcher would only see them
for a non-existent server id.

---

## 8. Honest limitations

* **The fake-Redis unit test remains the always-on guard in CI.**
  `tests/test_redis_multiprocess_integration.py` skips itself when no Redis is
  reachable, so on a CI image without a Redis service this harness contributes
  nothing. Do not delete or weaken the fake-Redis test on the strength of this one.
* **The panel read in step 2 is synthetic** (a sleep of `--fetch-latency-ms`). The
  admission logic (revision re-check), the commit and `publish_snapshot_to_redis()`
  are the real product code; the HTTP read and `process_inbounds()` are not.
* **The panel network-failure / backoff path is not exercised.** Reproducing it
  offline would mean faking the app-level error recording this harness deliberately
  does not touch (it would have to import `app` and a database), and a panel that is
  merely unreachable would prove the backoff ladder instead of the publish path.
  A failure path recorded end to end is covered by the app-level tests, not here.
* **Latency numbers are not a capacity benchmark.** Child-process start-up is excluded
  from the watch and publish measurements (the reader primes first, the listener
  subscribes first), but the mutation round trip includes two process spawns. For
  throughput work use `scripts/benchmark_cache.py`, which measures the snapshot cache
  in-process.
* **The wake latency is an upper bound.** It starts after the publisher's `publish()`
  returned. The true broker-to-subscriber delay is smaller by the return-trip cost of
  the publish call.

---

## 9. Interpreting a failure

1. Read the `- ` lines under the FAIL step; they name the assertion that broke.
2. Look at `children` in the JSON for the offending child's `exit_code` and
   `stderr_tail`. A non-zero exit means the child raised, and its traceback is there -
   the harness refuses to treat a crashed child as a measurement.
3. Re-run with `--rounds 5` if the failure is latency-shaped.
4. To see what the listener thread actually received, run the child by hand with
   `EVE_INTEGRATION_DEBUG=1` set, which prints every wake payload the product's
   handler was given (distinguishing "Redis never delivered" from "the handler
   refused the payload").

A failure here is a product finding, not a harness finding: every step drives the real
`panel/core/` code. Report it with the failing step, the JSON summary and the child's
`stderr_tail`.
