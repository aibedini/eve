# Memory: attribution before optimization

## Why this document exists first

"3.45 GB of 3.78 GB is used" is not yet a finding. On a Linux host that number includes
Redis, PostgreSQL, nginx, Xray children and the page cache, and it says nothing about which
part is Eve, which part is reclaimable, or whether anything is growing. Two specific traps
made the earlier reading unreliable:

* **RSS is not additive.** Summing RSS across Eve's processes counts the Python
  interpreter, libc and every shared wheel once per process, which turns a normal
  multi-process install into a fake memory problem. `panel/core/memory_report.py` therefore
  aggregates on **PSS** (shared pages divided by the processes that map them).
* **Page cache is not application memory.** Linux deliberately fills free RAM with cache.
  `used` is computed as `total - MemAvailable`, and the cache is reported separately, so
  reclaimable memory is never presented as pressure.

The order is deliberate: measure, then rank, then change representation. Nothing in this
document proposes a fix that has not been measured on the install it applies to.

## What now measures it

| Surface | What it answers |
|---|---|
| `panel/core/memory_report.py` | host totals/pressure, per-process RSS/PSS/USS/threads/peak/uptime, Eve's roles, snapshot duplication, Redis snapshot bytes, caches, bounded trend |
| `GET /api/system/memory` (superadmin) | the same payload Settings → Overview renders |
| `POST /api/system/memory/analyze` (superadmin + step-up) | explicit, bounded deep Python sample: largest allocations as file/line/size only |
| Settings → Overview → Memory | the human-readable version: host, Eve PSS, roles table, snapshot, caches, trend, health notes |
| `scripts/measure_snapshot_footprint.py` | how many bytes a row, `raw_client` and the `*_formatted` strings actually cost, by building production rows and re-measuring after deleting each key |
| `health_watchdog` | one sample a minute into a Redis ring (`LPUSH` + `LTRIM`, capped at 1440 entries) |

The trend is what separates the two very different explanations of a high number:

```
600 MB -> 700 MB -> 850 MB -> 1.1 GB -> 1.5 GB     a leak
restart -> 400 MB -> full dashboard -> 950 MB, flat  retained snapshot architecture
```

Sources: `/proc/meminfo`, `/proc/pressure/memory`, `/proc/<pid>/smaps_rollup` (RSS, PSS,
PSS_Anon/File/Shmem, Private_*), `/proc/<pid>/status` (VmHWM peak, Threads, VmSwap),
`/proc/<pid>/cmdline` (role). No new dependency: `psutil` is not required and was not added.
Where `/proc` is absent every section reports `available: false` with a reason instead of
inventing zeros, which is what a Windows/dev checkout shows.

Nothing in the payload is a credential, a command, an environment value or customer data -
only counts, byte sizes, pids, roles and ages - and a unit test asserts that
(`tests/test_memory_report.py::ReportContractTests`). A process command line is reduced to
the executable basename for the same reason.

Collect it on the host:

```bash
curl -s -b "session=<admin-cookie>" http://127.0.0.1:<app_port>/api/system/memory | python -m json.tool
```

## The architecture as it stands (code-level, before any measurement)

Roles, from the systemd units the installer writes (`setup.sh:1351-1422`) and the
auto-sizing it applies to the web worker (`setup.sh:1324-1333`: 3 workers from 12 GB RAM
up, 2 from 6 GB, **1 below 6 GB - so 1 on a 4 GB host**; `GUNICORN_WORKERS` overrides it):

```
eve-manager                 gunicorn (1 worker on 4 GB) + N threads
eve-manager-background      background_worker.py -> fetcher, scheduler, jobs
eve-manager-telegram-egress telegram_egress_worker.py
eve-manager-telegram-bot    telegram_bot_worker.py
Redis, PostgreSQL, nginx, managed Xray children
```

The snapshot is the big object, and it plausibly lives in **three** Python processes:

```
Redis (compressed, one key per server)
   |
   +-- background  process_inbounds -> GLOBAL_SERVER_DATA   (the owner; publishes)
   +-- web         snapshot_reader_worker -> load_snapshot_from_redis()
   |               "Pulls the shared snapshot from Redis into local memory so requests are
   |                served fast & in-process" (panel/jobs/schedulers.py:713)
   +-- telegram bot  imports GLOBAL_SERVER_DATA (telegram_bot_worker.py:28) and calls
                     load_snapshot_from_redis() on updates (telegram_bot_worker.py:5406)
```

And a single row is heavier than it needs to be:

```
processed client
  email, id, up, down, up_formatted, down_formatted, totalGB, totalGB_formatted,
  remaining, remaining_formatted, expiry..., service_state*, comment,
  raw_client  <-- the same configuration again, nested
```

plus, on v3, the same client appearing once per assigned inbound. That is what
`snapshot.client_rows` vs `snapshot.unique_clients` (and `duplication_ratio`,
`rows_with_raw_client`, `rows_with_formatted_strings`) are for.

## Suspects, and how each one is measured

| # | Suspect | How it is measured now | What would confirm it |
|---|---|---|---|
| A | background holds the full snapshot | `eve.roles.background.pss_bytes`; its own `snapshot.*` when the endpoint is called in that process | role PSS in the hundreds of MB, flat after a full fetch |
| B | the web worker also hydrates the whole snapshot | `eve.roles.web.pss_bytes` + `snapshot.*` from a web process | web PSS close to background's |
| C | the Telegram bot hydrates it too | `eve.roles.telegram-bot.pss_bytes` | a third copy of similar size |
| D | `raw_client` duplicates config per row | `snapshot.rows_with_raw_client / client_rows` | a large share of rows carry it |
| E | v3 clients repeat per assigned inbound | `snapshot.duplication_ratio` | ratio well above 1.0 |
| F | formatted strings are cached | `snapshot.rows_with_formatted_strings` | near `client_rows` |
| G | a big `/api/refresh` spikes RSS | `eve.roles.web.peak_rss_bytes` (VmHWM) before/after a full dashboard load | peak ≫ steady after a load |

The instrumentation deliberately does not compute the true retained size of nested objects
on every request; that is the on-demand deep analysis, which is bounded, admin-only and
returns file/line/size only.

Suspects D, E and F no longer need the live host to be quantified: they were measured in
bytes by building production rows and deleting one key at a time (next section). A, B, C
and G still need the host, because they are about how many copies exist and how large each
process is.

## What was measured (this checkout, 2026-09-19)

`scripts/measure_snapshot_footprint.py` builds fleets through the **production** builder
(`app.process_inbounds`) from synthetic 3x-ui payloads and measures the result four ways:
a bounded deep size, JSON, gzip, and - for attribution - the **deletion delta**, i.e. the
same snapshot with `raw_client` or the `*_formatted` keys removed. It touches no network,
no database and no Redis, and it never sleeps. Default run: 3 servers x 4 inbounds, 50
accounts per server.

| Measurement | Result |
|---|---|
| one processed row, measured in isolation | 4,933 bytes |
| of that, **shared with the rest of the snapshot** (dict keys, interned constants) | 2,568 bytes, **52.1%** |
| **marginal cost of one more row** (two fleet sizes, fixed part cancels) | **2,230 bytes** |
| `raw_client` inside one row, in isolation | 1,596 bytes |
| JSON of the snapshot | 0.163 MB per 150 rows |
| gzip of that JSON (the Redis copy) | 9.4% of JSON |
| deleting `raw_client` | -27.5% deep, -26.7% JSON, **-31.1% gzip** |
| deleting the `*_formatted` strings | -8.4% deep, -10.4% JSON, **-17.4% gzip** |
| deleting both | -35.9% deep, -37.0% JSON, **-47.7% gzip** |
| the v3 mirror, same account fleet | 150 unique rows -> 600 rows (4.00x), 0.338 -> 1.294 MB (3.82x) |
| collapsing the mirror to one entity per account | removes 0.956 MB, **73.9%** of the mirrored snapshot |

Two of these numbers are corrections to the arithmetic this document was written with:

* **A row measured in isolation is not what a row costs.** 4,933 bytes isolated against
  2,230 bytes marginal - and the isolated figure says exactly where the difference is:
  52.1% of a row is objects shared with the rest of the snapshot (the dict keys such as
  `'email'`, interned short strings such as `'xtls-rprx-vision'`, small ints), which an
  isolated measurement counts once per row. Scaling with the isolated figure overstates the
  snapshot 2.2x, so the slope between two fleet sizes is the number to use.
* **Attribution has to be a deletion, not a sum.** The isolated `raw_client` subtree is
  1,596 bytes/row, but deleting it saves 27.5% of the snapshot rather than 32% of it,
  because part of that subtree (the same email, id and literal values) is already
  referenced by the row itself. The plan below quotes the deletion delta.

What this measurement is not: the fleet is synthetic (the *field set* is production, since
`process_inbounds` built every row, but the values are short and uniform, so a real install
with longer emails and comments is slightly larger), and the absolute byte sizes belong to
the interpreter that produced them. The table above was **not** reproduced on this
checkout: it states CPython 3.14 on x86-64, while this checkout runs CPython 3.11.6 (3.11
and 3.7 are the only interpreters installed here). Re-running the same documented command
on 3.11.6 gives 5,447 bytes for an isolated row and 2,329 bytes marginal per row, with the
deletion deltas inside one percentage point of the table (-27.1% deep / -30.4% gzip for
`raw_client`, -9.3% / -17.0% for the formatted strings, -36.3% / -47.3% for both, and
73.8% removed by collapsing the v3 mirror). Read the **percentages and the slope** as the
transferable result and the absolutes as "on the interpreter that produced them", then
read the live `snapshot.client_rows` from the host and multiply, rather than trusting a
fleet size assumed here.

Scaling from the measured slope (2,230 bytes/row retained; extrapolation, not a
measurement), per snapshot copy:

| Rows | Retained | Redis copy (gzip ~9.3%) |
|---|---|---|
| 10,000 | 21 MB | 2.0 MB |
| 30,000 | 64 MB | 6.0 MB |
| 60,000 | 128 MB | 12 MB |

That per-copy figure is what matters, because the snapshot exists in up to three Python
processes (background, web, Telegram bot, see above). What the host actually holds is
`snapshot.client_rows` on the live instance - read it from `/api/system/memory` and
multiply, rather than assuming the fleet size.

## Optimization plan (ranked, not implemented)

Measured impact can only come from the numbers above; the ordering below is by expected
value and risk, and each item states what to measure before and after.

| # | Change | Measured or expected saving | Complexity | Risk | Migration | Performance |
|---|---|---|---|---|---|---|
| 1 | Telegram bot stops holding the full snapshot; read one service state from a small Redis index (`eve:service_state:<server>:<client>`) | its whole snapshot copy (C): ~64 MB retained at 30k rows, plus ~6 MB of Redis reads per hydrate | medium | low-medium (bot paths must be enumerated: 4 read sites today, `telegram_bot_worker.py:521,1111,1702,3513`) | none (additive index, backfilled by the fetcher) | bot latency improves (one key instead of a full hydrate) |
| 2 | Normalise v3 client entities: one entity per server, inbound membership by reference | **measured** -73.9% of the snapshot on a mirrored fleet (150 unique rows cost 0.338 MB; the 4.00x mirror costs 1.294 MB) | high | high (touches every consumer of the snapshot shape and the delta fingerprint) | snapshot format change (versioned payload) | JSON/delta shrink with the row count; gzip too |
| 3 | Drop `raw_client` from the hot snapshot where an authoritative client read already exists | **measured** -27.5% deep, -26.7% JSON, -31.1% gzip (1,596 B/row isolated) | high | medium-high (renew/edit/rotate depend on it; the targeted read path must cover legacy too) | none | mutation latency unchanged (it already re-reads on v3); dashboard read unchanged |
| 4 | Stop storing `*_formatted` strings in the canonical cache; format at render | **measured** -8.4% deep, -10.4% JSON, -17.4% gzip | medium | low-medium (templates and the API consumers must format) | API shape annotation | smaller payloads, slightly more CPU in the browser |
| 5 | Web hydration made lazy/per-server instead of forever-whole-fleet | B (unmeasured until a live `snapshot.*` from a web process exists) | high | medium-high (global search and the overview need an index or a server-side query) | none | first paint equal or better; search needs the index |
| 6 | Server-side search index so search does not need every client in RAM | enables 5 | medium | medium | none | search stays fast with less memory |
| 7 | Split bootstrap so background/telegram workers do not import the whole web app | interpreter + route + cache overhead per worker | medium | medium (blueprint/cache imports move behind factories) | none | faster worker start-up |

The measured order is worth stating plainly, because it is not the order the suspects were
listed in: on a v3 install the duplication (2) is an order of magnitude larger than
`raw_client` (3), and the two together are larger than everything else on the list. Items 3
and 4 are the cheap ones - together they cut roughly half the gzipped Redis payload without
touching the snapshot's shape - which makes them the place to start if the risk budget is
small, and the mirror (2) the place to go if it is not.

Two things deliberately **not** in the plan: calling `gc.collect()` on hot paths (it trades
CPU for nothing when the retention is architectural), and solving pressure with swap or
zram (that hides the OOM instead of preventing it). A small swap file remains reasonable as
an OOM safety net, not as a fix.

## Runtime configuration (Phase 8 findings)

* **Web workers are already sized to the host**: 1 on a 4 GB machine
  (`setup.sh:1324-1333`). There is no evidence of worker-count bloat, and the measurement
  above will show it directly (`eve.roles.web.processes`).
* **No `max_requests` / `max_requests_jitter` is configured anywhere** (`gunicorn_config.py`
  sets worker class, threads, timeouts and logging only). Adding them is a safety net
  against slow fragmentation, not a fix for retained snapshot: a worker restart drops that
  worker's hydrated snapshot and warms it again, so on a small host it should be enabled
  *after* the trend shows growth that a restart actually reclaims. If enabled, keep the
  value high enough that restarts do not become the steady state (e.g. 800 with jitter 100)
  and watch `trend` across restarts.
* No `MemoryHigh`/`MemoryMax` should be set before the attribution exists; a limit chosen
  without numbers turns a memory problem into a restart loop.

## Acceptance

Settings → Overview → Memory is expected to answer, for the reported host:

* how much RAM the host has, how much is **available**, how much is cache, and how much is
  swap;
* Eve's own total as **PSS**, and which role uses the most;
* how many full snapshot copies exist (three roles with a snapshot section of comparable
  size = three copies) and how large each is;
* how much of a snapshot row is duplication (`duplication_ratio`,
  `rows_with_raw_client`, `rows_with_formatted_strings`);
* how much Redis holds for the compressed snapshot, and how big the out-of-snapshot caches
  are;
* whether memory is stable or growing, from a bounded trend.

Only then is a representation change worth making, and the ranking above says which one to
start with.
